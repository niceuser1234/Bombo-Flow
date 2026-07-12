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

    def start(self):
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
            if self.engine is None or self.recorder.recording:
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
        with self._lock:
            self._finish_recording()

    def _force_stop(self):
        log.warning("Max recording duration reached — stopping")
        with self._lock:
            self._finish_recording()

    def _finish_recording(self):
        if not self.recorder.recording:
            return
        if self._max_timer:
            self._max_timer.cancel()
            self._max_timer = None
        duration = time.monotonic() - self._press_time
        captured = self.recorder.stop()
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
