import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from flow.pipeline import STOP_TIMEOUT_S, Pipeline


class _Overlay:
    def __init__(self):
        self.prewarmed = False

    def prewarm(self):
        self.prewarmed = True

    def show(self):
        pass

    def hide(self):
        pass


class _Recorder:
    def __init__(self):
        self._recording = False
        self.stops = 0
        self.abandoned = 0

    @property
    def recording(self):
        return self._recording

    def start(self):
        self._recording = True

    def stop(self):
        self.stops += 1
        self._recording = False
        return np.zeros(100, dtype=np.float32)

    def abandon(self):
        self.abandoned += 1

    @property
    def level(self):
        return 0.0


class _Config:
    min_duration_s = 0.3
    max_duration_s = 120.0
    engine = "parakeet"
    parakeet_model = "test"
    whisper_model = "test"
    cleanup_enabled = False
    cleanup_provider = "openrouter"
    cleanup_model = "test"
    cleanup_timeout_s = 5.0
    cleanup_min_words = 4


def _build_pipeline():
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.cfg = _Config()
    pipeline.set_state = lambda s: None
    pipeline.engine = None  # Will be set in tests
    pipeline.recorder = _Recorder()
    pipeline.overlay = _Overlay()
    pipeline.cleanup_enabled = False
    pipeline._queue = type("_Q", (), {"put": lambda s, item: None})()
    pipeline._press_time = 0.0
    pipeline._max_timer = None
    pipeline._lock = threading.Lock()
    pipeline._stopping = False
    return pipeline


class PipelineStartupTests(unittest.TestCase):
    def test_start_prewarms_overlay_before_worker_thread(self):
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.overlay = _Overlay()

        with patch("flow.pipeline.threading.Thread") as thread:
            pipeline.start()

        self.assertTrue(pipeline.overlay.prewarmed)
        thread.assert_called_once()


class HoldStartGuardTests(unittest.TestCase):
    def test_skip_when_already_recording(self):
        pipeline = _build_pipeline()
        pipeline.engine = True
        pipeline.on_hold_start()
        pipeline.on_hold_start()  # second call should be skipped
        self.assertEqual(pipeline.recorder.stops, 0)

    def test_skip_when_stopping_in_progress(self):
        pipeline = _build_pipeline()
        pipeline.engine = True
        pipeline._stopping = True
        pipeline.on_hold_start()
        self.assertFalse(pipeline.recorder.recording)

    def test_skip_when_engine_not_ready(self):
        pipeline = _build_pipeline()
        pipeline.engine = None
        pipeline.on_hold_start()
        self.assertFalse(pipeline.recorder.recording)


class RecorderStopTimeoutTests(unittest.TestCase):
    def test_safe_stop_returns_zeros_on_timeout(self):
        pipeline = _build_pipeline()
        pipeline.recorder._recording = True
        # Make stop hang by replacing with a deadlock.
        pipeline.recorder.stop = lambda: time.sleep(999) and np.zeros(0, dtype=np.float32)
        result = pipeline._stop_recorder_safe()
        self.assertEqual(len(result), 0)

    def test_safe_stop_returns_audio_on_success(self):
        pipeline = _build_pipeline()
        pipeline.recorder._recording = True
        result = pipeline._stop_recorder_safe()
        self.assertEqual(len(result), 100)
        self.assertFalse(pipeline.recorder.recording)
        self.assertEqual(pipeline.recorder.stops, 1)

    def test_wedged_recorder_is_replaced_so_dictation_survives(self):
        """A stalled CoreAudio stop must not leave `recording` stuck at True —
        that would make every later hotkey press a no-op."""
        pipeline = _build_pipeline()
        wedged = pipeline.recorder
        wedged._recording = True
        wedged.stop = lambda: time.sleep(999)

        with patch("flow.pipeline.STOP_TIMEOUT_S", 0.05), \
                patch("flow.pipeline.Recorder", _Recorder):
            pipeline._stop_recorder_safe()

        self.assertIsNot(pipeline.recorder, wedged)
        self.assertFalse(pipeline.recorder.recording)
        self.assertEqual(wedged.abandoned, 1)

        pipeline.engine = True
        pipeline.on_hold_start()
        self.assertTrue(pipeline.recorder.recording)


if __name__ == "__main__":
    unittest.main()
