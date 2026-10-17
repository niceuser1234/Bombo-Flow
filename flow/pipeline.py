"""The dictation pipeline: hotkey -> record -> transcribe -> clean -> paste.

Runs a single worker thread so overlapping dictations are processed in order.
State changes are reported via a callback so the menu bar can reflect them.
"""

import difflib
import functools
import logging
import queue
import re
import threading
import time

from . import audio as audio_mod
from . import lifecycle
from . import sound
from . import stt
from . import transform
from .audio import Recorder
from .cleanup import Cleaner
from .insert import insert_text
from .learn import filter_candidates
from .overlay import Overlay
from .store import get_store

log = logging.getLogger(__name__)

# States: loading, idle, recording, processing, error

STOP_TIMEOUT_S = 2.0  # max seconds to wait for recorder.stop() before giving up.
START_TIMEOUT_S = 2.0  # max seconds to wait for recorder.start() before giving up.
RESET_TIMEOUT_S = 3.0  # max seconds to wait for reset_portaudio() before giving up.
# Added to the chime's own length when muting the capture: the sound travels
# from the speakers back to the microphone, and playback starts a moment after
# play() returns.
CHIME_GUARD_S = 0.05
# A tap released this recently turns the next press into hands-free.
DOUBLE_TAP_S = 0.4


def _frontmost_app() -> str | None:
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app else None
    except Exception:
        return None


def _words_changed(raw: str, cleaned: str) -> int:
    """Word-level diff size between raw and cleaned transcript."""
    a = [w.lower() for w in raw.split()]
    b = [w.lower() for w in cleaned.split()]
    changed = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b).get_opcodes():
        if op != "equal":
            changed += max(i2 - i1, j2 - j1)
    return changed


@functools.lru_cache(maxsize=2048)
def _term_pattern(term: str) -> "re.Pattern":
    # Lookarounds rather than \b so terms starting or ending in punctuation
    # (".NET", "C++") still anchor correctly.
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def _terms_in(vocabulary: list[str], text: str) -> list[str]:
    """Vocabulary terms that appear in text, matched on word boundaries.

    A plain substring test made "AI" match "again" and "Go" match "Google".
    That inflated the usage counters which order the initial_prompt and drive
    the least-used eviction, so two-letter terms pinned themselves to the top
    while real multi-word terms sat at zero and were evicted first — and the
    same phantom hits were reported as dictionary fixes in the insights panel.
    """
    return [t for t in vocabulary if _term_pattern(t).search(text)]


def _dict_fixes(vocabulary: list[str], raw: str, cleaned: str) -> int:
    """Vocabulary terms that appear in the cleaned text but not in the raw
    transcript — i.e. corrections the dictionary is responsible for."""
    return sum(
        1 for t in _terms_in(vocabulary, cleaned)
        if not _term_pattern(t).search(raw)
    )


INITIAL_PROMPT_TERM_LIMIT = 30
INITIAL_PROMPT_MAX_CHARS = 800


def _build_initial_prompt(vocabulary: list[str]) -> str | None:
    """Bias STT towards learned dictionary terms (foreign words, proper
    nouns) via Whisper's initial_prompt. Top terms only (vocabulary is
    usage-ordered) and capped in length — initial_prompt is not meant for
    long text and an oversized prompt can hurt more than it helps.

    No language prefix (dictations mix German/English/other languages, and a
    German "Vokabular:" label would itself bias the model). If the joined
    terms exceed the cap, truncate at the last full term rather than
    mid-term — a half-written term is more likely to confuse the model than
    to help it."""
    if not vocabulary:
        return None
    terms = vocabulary[:INITIAL_PROMPT_TERM_LIMIT]
    prompt = ", ".join(terms)
    if len(prompt) <= INITIAL_PROMPT_MAX_CHARS:
        return prompt
    truncated = prompt[:INITIAL_PROMPT_MAX_CHARS]
    cut = truncated.rfind(", ")
    return truncated[:cut] if cut != -1 else truncated


class Pipeline:
    def __init__(self, cfg, set_state):
        self.cfg = cfg
        self.set_state = set_state
        self.recorder = self._new_recorder()
        self.cleaner = Cleaner(cfg)
        self.overlay = Overlay(level_source=lambda: self.recorder.level)
        self.engine = None
        self.cleanup_enabled = cfg.cleanup_enabled and self.cleaner.available
        self._queue: queue.Queue = queue.Queue()
        self._press_time = 0.0
        self._max_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._stopping = False  # guard against concurrent finish+start
        self._recover_timer: threading.Timer | None = None
        # Held for the duration of an audio recovery. Acquired non-blocking,
        # so it doubles as the "recovery in progress" flag for on_hold_start.
        self._recovery_lock = threading.Lock()
        # Set once CoreAudio has wedged: no new recordings, and the worker
        # relaunches the app after finishing whatever is still queued.
        self._relaunch_pending = False
        self._relaunch = lifecycle.relaunch
        # The running recording's mode: "dictation", "command" or
        # "handsfree"; None while idle.
        self._mode: str | None = None
        # Pending after a short tap: a second press inside the window turns
        # the still-open recording into hands-free, otherwise it is dropped.
        self._tap_timer: threading.Timer | None = None

    def _new_recorder(self) -> Recorder:
        """Every Recorder — the initial one and each replacement built during
        recovery — has to carry the configured microphone."""
        return Recorder(device_name=self.cfg.input_device)

    def start(self):
        # Build the native overlay before the first hotkey press so recording
        # feedback appears immediately rather than paying AppKit setup cost.
        self.overlay.prewarm()
        # Load, warm up, and run the model all on the single worker thread.
        # MLX streams are thread-local, so the model must be used on the same
        # thread it was created on — otherwise generate() raises
        # "There is no Stream(cpu, N) in current thread".
        threading.Thread(target=self._worker, daemon=True).start()

    def _load_model(self) -> bool:
        self.set_state("loading")
        if self.cfg.engine == "whisper":
            log.info(
                "Loading Whisper engine (%s) — first run downloads the model "
                "(~0.5 GB) from Hugging Face, which can take a few minutes",
                self.cfg.whisper_model,
            )
        try:
            self.engine = stt.create_engine(self.cfg)
            self.engine.warmup()
            self.set_state("idle")
            log.info("Engine ready")
            return True
        except Exception:
            log.exception("Failed to load STT engine")
            self.set_state("error")
            return False

    # -- hotkey callbacks (run on the pynput listener thread) --

    def on_mode_change(self, mode):
        """Control pressed during an fn hold: the dictation becomes a
        rewrite-selection command. Hands-free never changes mode."""
        if mode != "command":
            return
        with self._lock:
            if (not self.recorder.recording or self._stopping
                    or self._tap_timer is not None
                    or self._mode != "dictation"):
                return
            self._mode = "command"
            self.overlay.set_style("command")

    def on_hold_start(self, mode="dictation"):
        with self._lock:
            if self.engine is None:
                return
            if (self._tap_timer is not None and self.recorder.recording
                    and not self._stopping):
                if mode == "dictation":
                    self._enter_handsfree()
                    return
                then = "discard-tap"
            elif (self.recorder.recording and not self._stopping
                    and self._mode == "handsfree"):
                then = "finish"
            else:
                self._start_recording(mode)
                return
        # _finish_recording takes the lock itself and blocks on the stop, so
        # both follow-ups run after it is released.
        if then == "finish":
            self._finish_recording()
            return
        # A command press inside the tap window: drop the pending tap first,
        # then record the command on a fresh stream. If the grace timer won
        # the race and is already finishing, the start below is refused like
        # any press during a stop.
        self._finish_recording(discard=True)
        with self._lock:
            self._start_recording(mode)

    def _enter_handsfree(self):
        """Second press of a double tap: keep the open stream, drop the tap's
        audio, and record until the next press. Caller holds self._lock."""
        self._tap_timer.cancel()
        self._tap_timer = None
        self._mode = "handsfree"
        self.recorder.clear()
        self._press_time = time.monotonic()
        if self.cfg.sounds:
            self.recorder.mute_for(sound.handsfree_duration_s() + CHIME_GUARD_S)
            sound.play_handsfree()
        self.overlay.set_style("handsfree")
        self.set_state("handsfree")
        # The dictation limit must not cut a hands-free recording short.
        if self._max_timer:
            self._max_timer.cancel()
        self._max_timer = threading.Timer(
            self.cfg.handsfree_max_s, self._force_stop
        )
        self._max_timer.daemon = True
        self._max_timer.start()

    def _start_recording(self, mode):
        """Open the stream for a new press. Caller holds self._lock."""
        if self._relaunch_pending:
            log.debug("on_hold_start skipped: relaunch pending")
            return
        if self.recorder.recording:
            log.debug("on_hold_start skipped: already recording")
            return
        if self._stopping:
            log.debug("on_hold_start skipped: _finish_recording in progress")
            return
        if self._recovery_lock.locked():
            # Opening a stream while Pa_Terminate runs is the crash the
            # recovery is trying to avoid in the first place.
            log.debug("on_hold_start skipped: audio recovery in progress")
            return
        self._press_time = time.monotonic()
        finished, _, error = self._run_bounded(
            self.recorder.start, START_TIMEOUT_S, "recorder-start"
        )
        if not finished:
            log.warning(
                "recorder.start() did not return within %.1fs — CoreAudio "
                "is wedged", START_TIMEOUT_S,
            )
            self._request_relaunch("recorder.start() hung")
            return
        if error is not None:
            log.error(
                "Could not start recording — resetting audio stack",
                exc_info=error,
            )
            self._schedule_recovery()
            return
        self._mode = mode
        if self.cfg.sounds:
            # After the stream is open, never before: opening the input
            # device reconfigures CoreAudio and truncates a sound that is
            # already playing, which clips the chime's release tail. Mute
            # the capture for its duration instead, so it still stays out
            # of the recording. Also keeps the chime honest — it only
            # sounds once recording has actually started.
            self.recorder.mute_for(sound.start_duration_s() + CHIME_GUARD_S)
            sound.play_start()
        # A fresh recording supersedes any pending recovery reset, so
        # cancel the stale error->idle timer before it clobbers this
        # recording's menubar state.
        if self._recover_timer:
            self._recover_timer.cancel()
            self._recover_timer = None
        self.set_state("recording")
        self.overlay.show(style=mode)
        self._max_timer = threading.Timer(
            self.cfg.max_duration_s, self._force_stop
        )
        self._max_timer.daemon = True
        self._max_timer.start()

    def on_hold_end(self):
        with self._lock:
            if not self.recorder.recording or self._stopping:
                return
            # Hands-free ends on the next press, not on a release; and a
            # release while the tap window is open belongs to the tap.
            if self._mode == "handsfree" or self._tap_timer is not None:
                return
            held = time.monotonic() - self._press_time
            if self._mode == "dictation" and held < self.cfg.min_duration_s:
                # Too short to dictate: keep the stream open in case this is
                # the first half of a double tap.
                self._tap_timer = threading.Timer(
                    DOUBLE_TAP_S, self._tap_grace_expired
                )
                self._tap_timer.daemon = True
                self._tap_timer.start()
                return
        self._finish_recording()

    def _tap_grace_expired(self):
        # Hands the timer itself in, so a timer that fired just as a second
        # press switched to hands-free cannot discard that recording.
        self._finish_recording(discard=True, tap=threading.current_thread())

    def _force_stop(self):
        log.warning("Max recording duration reached — stopping")
        self._finish_recording()

    def _finish_recording(self, discard=False, tap=None):
        with self._lock:
            if tap is not None and tap is not self._tap_timer:
                return  # a superseded tap-grace timer
            if not self.recorder.recording or self._stopping:
                return
            self._stopping = True
            if self._tap_timer is not None:
                if self._tap_timer is not threading.current_thread():
                    self._tap_timer.cancel()
                self._tap_timer = None
            if self._max_timer:
                self._max_timer.cancel()
                self._max_timer = None
            duration = time.monotonic() - self._press_time
            mode = self._mode or "dictation"
        # _stopping stays True until the tail below finishes, so a fast
        # re-press cannot start a recording whose state/overlay this old
        # press would then stomp. The blocking stop still runs lock-free.
        try:
            captured, timed_out = self._stop_recorder_safe()
            if self.cfg.sounds:
                sound.play_stop()

            if timed_out:
                self.overlay.hide()
                if (not discard and duration >= self.cfg.min_duration_s
                        and len(captured)
                        and not audio_mod.is_silent(captured)):
                    self.set_state("processing")
                    self._queue.put((captured, time.monotonic(), mode))
                self._request_relaunch("recorder.stop() hung")
                return
            if self._relaunch_pending:
                # E.g. a start that returned after its timeout already asked
                # for the relaunch. Anything queued now would sit behind the
                # relaunch sentinel, and a recovery would race it. The state
                # stays "loading" until the app restarts.
                self.overlay.hide()
                log.info("Discarding recording: relaunch pending")
                return
            if discard or duration < self.cfg.min_duration_s:
                self.overlay.hide()
                log.info("Discarding %.2fs tap", duration)
                self.set_state("idle")
                return
            if len(captured) == 0:
                self.overlay.hide()
                log.warning("recorder.stop() returned no audio")
                self._enter_error_then_idle()
                return
            if audio_mod.is_silent(captured):
                self.overlay.hide()
                log.warning(
                    "Recording is silent — resetting audio stack (stale "
                    "input device?). If this repeats, check Microphone "
                    "permission in System Settings > Privacy & Security"
                )
                self._schedule_recovery()
                return
            if mode == "command":
                self.overlay.flash("Rewriting…", seconds=None)
            else:
                self.overlay.hide()
            self.set_state("processing")
            self._queue.put((captured, time.monotonic(), mode))
        finally:
            with self._lock:
                self._mode = None
                self._stopping = False

    def _schedule_recovery(self):
        """Run the audio recovery on its own thread.

        reset_portaudio() calls Pa_Terminate, which is precisely what blocks
        when CoreAudio is in the wedged state this recovery exists for. Both
        callers reach it from the SerialRunner's single hotkey-dispatch
        thread (on_hold_start holds self._lock while doing so), so running it
        inline would stall every further press and release behind it — the
        app would stop responding to the hotkey altogether.
        """
        if self._relaunch_pending:
            # A relaunch replaces the whole audio stack anyway; touching
            # PortAudio now would only race the wedged stream.
            log.debug("Audio recovery skipped: relaunch pending")
            return
        if not self._recovery_lock.acquire(blocking=False):
            log.debug("Audio recovery already in progress")
            return
        threading.Thread(
            target=self._run_recovery, daemon=True, name="audio-recovery"
        ).start()

    def _run_recovery(self):
        try:
            self._recover_audio()
        except Exception:
            log.exception("Audio recovery failed")
            self._enter_error_then_idle()
        finally:
            self._recovery_lock.release()

    def _recover_audio(self):
        """A capture came back dead (silent stream or failed start): refresh
        PortAudio's stale device snapshot and swap in a fresh Recorder so
        the next dictation works without restarting the app."""
        stale = self.recorder
        if stale.recording:
            # Same reasoning as the timed-out-stop path above: terminating
            # PortAudio underneath a live CoreAudio stream can crash. Drop the
            # stream and leave the device snapshot alone.
            log.warning(
                "Skipping PortAudio reset — the previous stream is still live"
            )
            stale.abandon()
        else:
            # Bounded like start/stop: a blocked Pa_Terminate would otherwise
            # hold _recovery_lock forever, refusing every later press.
            finished, _, error = self._run_bounded(
                audio_mod.reset_portaudio, RESET_TIMEOUT_S, "portaudio-reset"
            )
            if not finished:
                log.warning(
                    "reset_portaudio() did not return within %.1fs — "
                    "CoreAudio is wedged", RESET_TIMEOUT_S,
                )
                self._request_relaunch("reset_portaudio() hung")
                return
            if error is not None:
                raise error
        self.recorder = self._new_recorder()
        self._enter_error_then_idle()

    def _enter_error_then_idle(self):
        """Show the error state, then return to idle after 2s via a daemon
        timer. The timer is tracked so a new recording started within that
        window can cancel it (see on_hold_start) instead of having its state
        clobbered."""
        self.set_state("error")
        # Cancel any timer still pending from an earlier failure: an orphan
        # would fire later and force "idle" over a live recording.
        if self._recover_timer is not None:
            self._recover_timer.cancel()
        self._recover_timer = threading.Timer(2.0, lambda: self.set_state("idle"))
        self._recover_timer.daemon = True
        self._recover_timer.start()

    def _run_bounded(self, func, timeout, name):
        """Run func() on a throwaway thread. Returns (finished, result, error).
        A wedged CoreAudio call leaves its thread behind — daemon, harmless."""
        box: dict = {}
        done = threading.Event()

        def _run():
            try:
                box["result"] = func()
            except BaseException as e:  # re-raised by the caller
                box["error"] = e
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True, name=name).start()
        if not done.wait(timeout):
            return False, None, None
        return True, box.get("result"), box.get("error")

    def _stop_recorder_safe(self) -> "tuple[np.ndarray, bool]":
        """Stop the recorder off the calling thread, with a hard timeout.

        CoreAudio can wedge inside AudioDeviceStop (its IO thread and the
        stopping thread deadlock over the HAL locks). On timeout the audio
        captured so far is rescued, the stream abandoned, and a fresh
        Recorder swapped in. Returns (audio, timed_out).
        """
        import numpy as np

        stuck = self.recorder
        finished, result, error = self._run_bounded(
            stuck.stop, STOP_TIMEOUT_S, "recorder-stop"
        )
        if not finished:
            log.warning(
                "recorder.stop() did not return within %.1fs — CoreAudio is "
                "stalled; rescuing the captured audio and abandoning the stream",
                STOP_TIMEOUT_S,
            )
            rescued = stuck.rescue()
            stuck.abandon()
            # The overlay's level_source closes over `self`, so it follows the
            # swap without further wiring.
            self.recorder = self._new_recorder()
            return rescued, True
        if error is not None:
            log.error("recorder.stop() raised", exc_info=error)
            return np.zeros(0, dtype=np.float32), False
        return result, False

    def _request_relaunch(self, reason: str):
        """CoreAudio is wedged for the rest of this process. Stop taking new
        recordings; the worker relaunches once the queue ahead is drained."""
        if self._relaunch_pending:
            return
        self._relaunch_pending = True
        self.set_state("loading")
        self._queue.put(None)
        log.warning("Relaunch scheduled: %s", reason)

    # -- worker --

    def _worker(self):
        if not self._load_model():
            return
        while True:
            item = self._queue.get()
            if item is None:
                self._relaunch("CoreAudio wedged")
                return
            captured, released_at, mode = item
            try:
                self._process(captured, released_at, mode)
            except Exception:
                log.exception("Pipeline processing failed")
                self.set_state("error")
                time.sleep(2.0)
            finally:
                if self._queue.empty() and not self.recorder.recording:
                    self.set_state("idle")

    def _process_rewrite(self, captured, released_at):
        """Rewrite mode: the recording is an instruction for the selection.

        Never raises: the "Rewriting…" pill is persistent, so any failure must
        replace it with a message rather than leave it on screen.
        """
        try:
            self._rewrite(captured, released_at)
        except Exception:
            log.exception("Rewrite failed")
            self.overlay.flash("Rewrite failed", 2.0)

    def _rewrite(self, captured, released_at):
        instruction = self.engine.transcribe(captured).strip()
        log.info("Rewrite instruction (%.2fs): %d chars",
                 time.monotonic() - released_at, len(instruction))
        if len(instruction) < 2:
            self.overlay.flash("Didn't catch that", 2.0)
            return
        if not self.cleaner.available:
            self.overlay.flash("Rewrite needs an API key", 2.0)
            return
        selection = transform.grab_selection()
        if not selection:
            self.overlay.flash("Select some text first", 2.0)
            return
        result = self.cleaner.rewrite(selection, instruction)
        if not result:
            self.overlay.flash("Rewrite failed", 2.0)
            return
        self.overlay.hide()
        try:
            get_store().add_dictation(
                instruction, result,
                duration_s=len(captured) / self.recorder.samplerate,
                app=_frontmost_app(), corrected_words=0, dict_fixes=0,
                kind="rewrite",
            )
        except Exception:
            log.exception("Storing rewrite failed")
        insert_text(result, mode=self.cfg.insert_mode)
        log.info("Rewrote %d -> %d chars after %.2fs total",
                 len(selection), len(result), time.monotonic() - released_at)

    def _process(self, captured, released_at, mode="dictation"):
        if mode == "command":
            self._process_rewrite(captured, released_at)
            return
        store = get_store()
        # Two lists on purpose: the cleanup LLM sees the whole dictionary (it
        # has the surrounding text and is told not to insert unspoken terms),
        # while the STT prompt only gets terms that have been corroborated.
        vocabulary = store.dictionary_words()
        raw = self.engine.transcribe(
            captured, initial_prompt=_build_initial_prompt(store.stt_bias_words())
        )
        t_stt = time.monotonic()
        # Transcripts only at DEBUG: the log file outlives the dictation and
        # holds whatever the user happened to speak. INFO keeps the timings
        # that make the log useful for diagnosing slowness.
        log.info("STT (%.2fs): %d chars", t_stt - released_at, len(raw))
        log.debug("STT raw: %r", raw)
        if not raw:
            return
        text, new_terms = raw, []
        if self.cleanup_enabled:
            result = self.cleaner.clean(raw, vocabulary=vocabulary)
            text, new_terms = result.text, result.new_terms
            log.info("Cleaned (%.2fs): %d chars",
                     time.monotonic() - t_stt, len(text))
            log.debug("Cleaned text: %r", text)
        app_name = _frontmost_app()
        # Persist before inserting. Insertion is the step that can fail for
        # reasons outside our control (wedged main thread, refused pasteboard),
        # and a transcript that only exists in the history is recoverable —
        # one that was never written down is gone.
        # Metrics and auto-learning must never break the dictation path.
        try:
            store.add_dictation(
                raw, text,
                duration_s=len(captured) / self.recorder.samplerate,
                app=app_name,
                corrected_words=_words_changed(raw, text),
                dict_fixes=_dict_fixes(vocabulary, raw, text),
            )
            used = _terms_in(vocabulary, text)
            if used:
                store.bump_uses(used)
            if new_terms:
                accepted = filter_candidates(new_terms, vocabulary)
                promoted = store.record_candidates(accepted)
                if promoted:
                    log.info("Dictionary auto-learned: %s", promoted)
        except Exception:
            log.exception("Storing dictation/metrics failed")
        insert_text(text, mode=self.cfg.insert_mode)
        log.info("Inserted after %.2fs total", time.monotonic() - released_at)
