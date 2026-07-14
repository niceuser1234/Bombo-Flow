"""The dictation pipeline: hotkey -> record -> transcribe -> clean -> paste.

Runs a single worker thread so overlapping dictations are processed in order.
State changes are reported via a callback so the menu bar can reflect them.
"""

import difflib
import logging
import queue
import threading
import time

from . import audio as audio_mod
from . import stt
from .audio import Recorder
from .cleanup import Cleaner
from .insert import insert_text
from .learn import filter_candidates
from .overlay import Overlay
from .store import get_store

log = logging.getLogger(__name__)

# States: loading, idle, recording, processing, error

STOP_TIMEOUT_S = 2.0  # max seconds to wait for recorder.stop() before giving up.


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


def _dict_fixes(vocabulary: list[str], raw: str, cleaned: str) -> int:
    """Vocabulary terms that appear in the cleaned text but not in the raw
    transcript — i.e. corrections the dictionary is responsible for."""
    raw_l = raw.lower()
    return sum(
        1 for t in vocabulary if t in cleaned and t.lower() not in raw_l
    )


class Pipeline:
    def __init__(self, cfg, set_state):
        self.cfg = cfg
        self.set_state = set_state
        self.recorder = Recorder()
        self.cleaner = Cleaner(cfg)
        self.overlay = Overlay(level_source=lambda: self.recorder.level)
        self.engine = None
        self.cleanup_enabled = cfg.cleanup_enabled and self.cleaner.available
        self._queue: queue.Queue = queue.Queue()
        self._press_time = 0.0
        self._max_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._stopping = False  # guard against concurrent finish+start

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

    def on_hold_start(self):
        with self._lock:
            if self.engine is None:
                return
            if self.recorder.recording:
                log.debug("on_hold_start skipped: already recording")
                return
            if self._stopping:
                log.debug("on_hold_start skipped: _finish_recording in progress")
                return
            self._press_time = time.monotonic()
            try:
                self.recorder.start()
            except Exception:
                log.exception("Could not start recording (mic in use?)")
                self.set_state("error")
                return
            self.set_state("recording")
            self.overlay.show()
            self._max_timer = threading.Timer(
                self.cfg.max_duration_s, self._force_stop
            )
            self._max_timer.daemon = True
            self._max_timer.start()

    def on_hold_end(self):
        self._finish_recording()

    def _force_stop(self):
        log.warning("Max recording duration reached — stopping")
        self._finish_recording()

    def _finish_recording(self):
        with self._lock:
            if not self.recorder.recording or self._stopping:
                return
            self._stopping = True
            if self._max_timer:
                self._max_timer.cancel()
                self._max_timer = None
            duration = time.monotonic() - self._press_time
        # _stopping stays True until the tail below finishes, so a fast
        # re-press cannot start a recording whose state/overlay this old
        # press would then stomp. The blocking stop still runs lock-free.
        try:
            captured = self._stop_recorder_safe()

            self.overlay.hide()
            if duration < self.cfg.min_duration_s:
                log.info("Discarding %.2fs tap", duration)
                self.set_state("idle")
                return
            if audio_mod.is_silent(captured):
                log.warning(
                    "Recording is silent — check Microphone permission for the "
                    "host app in System Settings > Privacy & Security > Microphone"
                )
                self.set_state("error")
                threading.Timer(2.0, lambda: self.set_state("idle")).start()
                return
            self.set_state("processing")
            self._queue.put((captured, time.monotonic()))
        finally:
            with self._lock:
                self._stopping = False

    def _stop_recorder_safe(self) -> "np.ndarray":
        """Stop the recorder off the calling thread, with a hard timeout.

        CoreAudio can wedge inside AudioDeviceStop (its IO thread and the
        stopping thread deadlock over the HAL locks). The stop therefore runs
        on a throwaway thread: if it does not return in time we abandon that
        thread along with its stream and swap in a fresh Recorder, so the next
        dictation still works instead of finding `recording` stuck at True.

        Returns the captured audio array, or zeros when the stop timed out.
        """
        import numpy as np

        result: list[np.ndarray] = []
        done = threading.Event()
        stuck = self.recorder

        def _stop():
            try:
                result.append(stuck.stop())
            except Exception:
                log.exception("recorder.stop() raised")
            finally:
                done.set()

        stopper = threading.Thread(target=_stop, daemon=True, name="recorder-stop")
        stopper.start()
        if not done.wait(STOP_TIMEOUT_S):
            log.warning(
                "recorder.stop() did not return within %.1fs — CoreAudio is "
                "stalled; abandoning the stream and rebuilding the recorder",
                STOP_TIMEOUT_S,
            )
            stuck.abandon()
            # The overlay's level_source closes over `self`, so it follows the
            # swap without further wiring.
            self.recorder = Recorder()
            return np.zeros(0, dtype=np.float32)
        if result:
            return result[0]
        return np.zeros(0, dtype=np.float32)

    # -- worker --

    def _worker(self):
        if not self._load_model():
            return
        while True:
            captured, released_at = self._queue.get()
            try:
                self._process(captured, released_at)
            except Exception:
                log.exception("Pipeline processing failed")
                self.set_state("error")
                time.sleep(2.0)
            finally:
                if self._queue.empty() and not self.recorder.recording:
                    self.set_state("idle")

    def _process(self, captured, released_at):
        raw = self.engine.transcribe(captured)
        t_stt = time.monotonic()
        log.info("STT (%.2fs): %r", t_stt - released_at, raw)
        if not raw:
            return
        store = get_store()
        text, new_terms, vocabulary = raw, [], []
        if self.cleanup_enabled:
            vocabulary = store.dictionary_words()
            result = self.cleaner.clean(raw, vocabulary=vocabulary)
            text, new_terms = result.text, result.new_terms
            log.info("Cleaned (%.2fs): %r", time.monotonic() - t_stt, text)
        app_name = _frontmost_app()
        insert_text(text, mode=self.cfg.insert_mode)
        log.info("Inserted after %.2fs total", time.monotonic() - released_at)
        # Metrics and auto-learning must never break the dictation path.
        try:
            store.add_dictation(
                raw, text,
                duration_s=len(captured) / 16000,
                app=app_name,
                corrected_words=_words_changed(raw, text),
                dict_fixes=_dict_fixes(vocabulary, raw, text),
            )
            if new_terms:
                accepted = filter_candidates(new_terms, vocabulary)
                promoted = store.record_candidates(accepted)
                if promoted:
                    log.info("Dictionary auto-learned: %s", promoted)
        except Exception:
            log.exception("Storing dictation/metrics failed")
