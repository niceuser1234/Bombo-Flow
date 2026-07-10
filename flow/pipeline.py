"""The dictation pipeline: hotkey -> record -> transcribe -> clean -> paste.

Runs a single worker thread so overlapping dictations are processed in order.
State changes are reported via a callback so the menu bar can reflect them.
"""

import logging
import queue
import threading
import time

from . import audio as audio_mod
from . import stt
from .audio import Recorder
from .cleanup import Cleaner
from .insert import insert_text

log = logging.getLogger(__name__)

# States: loading, idle, recording, processing, error


class Pipeline:
    def __init__(self, cfg, set_state):
        self.cfg = cfg
        self.set_state = set_state
        self.recorder = Recorder()
        self.cleaner = Cleaner(cfg)
        self.engine = None
        self.cleanup_enabled = cfg.cleanup_enabled and self.cleaner.available
        self._queue: queue.Queue = queue.Queue()
        self._press_time = 0.0
        self._max_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._load_model, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

    def _load_model(self):
        self.set_state("loading")
        try:
            self.engine = stt.create_engine(self.cfg)
            self.engine.warmup()
            self.set_state("idle")
            log.info("Engine ready")
        except Exception:
            log.exception("Failed to load STT engine")
            self.set_state("error")

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
        text = self.engine.transcribe(captured)
        t_stt = time.monotonic()
        log.info("STT (%.2fs): %r", t_stt - released_at, text)
        if not text:
            return
        if self.cleanup_enabled:
            text = self.cleaner.clean(text)
            log.info("Cleaned (%.2fs): %r", time.monotonic() - t_stt, text)
        insert_text(text, mode=self.cfg.insert_mode)
        log.info("Inserted after %.2fs total", time.monotonic() - released_at)
