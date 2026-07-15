import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from flow.pipeline import STOP_TIMEOUT_S, Pipeline, _build_initial_prompt


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
    insert_mode = "paste"
    sounds = True


class _FakeStore:
    def __init__(self, words=None):
        self._words = words or []
        self.bumped = []

    def dictionary_words(self):
        return list(self._words)

    def add_dictation(self, *args, **kwargs):
        pass

    def record_candidates(self, terms):
        return []

    def bump_uses(self, words):
        self.bumped.append(list(words))


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


class SoundHookTests(unittest.TestCase):
    def test_start_sound_plays_after_successful_recorder_start(self):
        pipeline = _build_pipeline()
        pipeline.engine = True
        with patch("flow.pipeline.sound.play_start") as play_start:
            pipeline.on_hold_start()
        play_start.assert_called_once_with()

    def test_start_sound_not_played_when_recorder_start_fails(self):
        pipeline = _build_pipeline()
        pipeline.engine = True
        pipeline.recorder.start = lambda: (_ for _ in ()).throw(RuntimeError("mic busy"))
        with patch("flow.pipeline.sound.play_start") as play_start:
            pipeline.on_hold_start()
        play_start.assert_not_called()

    def test_start_sound_not_played_when_sounds_disabled(self):
        pipeline = _build_pipeline()
        pipeline.engine = True
        pipeline.cfg.sounds = False
        with patch("flow.pipeline.sound.play_start") as play_start:
            pipeline.on_hold_start()
        play_start.assert_not_called()

    def test_stop_sound_plays_after_recorder_stops(self):
        pipeline = _build_pipeline()
        pipeline.recorder._recording = True
        with patch("flow.pipeline.sound.play_stop") as play_stop:
            pipeline._finish_recording()
        play_stop.assert_called_once_with()

    def test_stop_sound_not_played_when_sounds_disabled(self):
        pipeline = _build_pipeline()
        pipeline.recorder._recording = True
        pipeline.cfg.sounds = False
        with patch("flow.pipeline.sound.play_stop") as play_stop:
            pipeline._finish_recording()
        play_stop.assert_not_called()

    def test_stop_sound_plays_even_for_discarded_short_tap(self):
        """The stop blip is 'you released the key' feedback, independent of
        whether the tap turns out too short to process."""
        pipeline = _build_pipeline()
        pipeline.recorder._recording = True
        pipeline._press_time = time.monotonic()  # ~0s elapsed < min_duration_s
        with patch("flow.pipeline.sound.play_stop") as play_stop:
            pipeline._finish_recording()
        play_stop.assert_called_once_with()


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


class LockNotHeldAcrossStopTests(unittest.TestCase):
    def test_lock_is_free_while_stop_blocks(self):
        pipeline = _build_pipeline()
        pipeline.recorder._recording = True
        enter = threading.Event()
        release = threading.Event()

        def hanging_stop():
            enter.set()
            release.wait(5.0)
            return np.zeros(100, dtype=np.float32)

        pipeline.recorder.stop = hanging_stop

        t = threading.Thread(target=pipeline.on_hold_end)
        t.start()
        self.assertTrue(enter.wait(1.0), "stop() was never entered")

        self.assertTrue(pipeline._lock.acquire(timeout=0.2))
        pipeline._lock.release()
        self.assertTrue(pipeline._stopping)

        release.set()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())
        self.assertFalse(pipeline._stopping)

    def test_start_refused_while_stop_in_flight(self):
        pipeline = _build_pipeline()
        pipeline.engine = True
        pipeline.recorder._recording = True
        enter = threading.Event()
        release = threading.Event()

        def hanging_stop():
            enter.set()
            release.wait(5.0)
            return np.zeros(100, dtype=np.float32)

        pipeline.recorder.stop = hanging_stop
        start_calls = []
        pipeline.recorder.start = lambda: start_calls.append(1)

        # Pin the timeout above every wait below so the stop is guaranteed
        # to still be in flight while we probe, whatever the real constant.
        with patch("flow.pipeline.STOP_TIMEOUT_S", 5.0):
            t = threading.Thread(target=pipeline.on_hold_end)
            t.start()
            self.assertTrue(enter.wait(1.0), "stop() was never entered")

            start = threading.Thread(target=pipeline.on_hold_start)
            start.start()
            start.join(timeout=1.0)
            self.assertFalse(start.is_alive(), "on_hold_start blocked")
            self.assertEqual(start_calls, [])

            release.set()
            t.join(timeout=5.0)

    def test_start_refused_while_tail_still_running(self):
        """_stopping must stay True through the post-stop tail (overlay.hide,
        state changes) so a fast re-press cannot race the old press's
        cleanup."""
        pipeline = _build_pipeline()
        pipeline.engine = True
        pipeline.recorder._recording = True
        hide_entered = threading.Event()
        hide_release = threading.Event()

        def hanging_hide():
            hide_entered.set()
            hide_release.wait(5.0)

        pipeline.overlay.hide = hanging_hide
        start_calls = []
        pipeline.recorder.start = lambda: start_calls.append(1)

        t = threading.Thread(target=pipeline.on_hold_end)
        t.start()
        self.assertTrue(hide_entered.wait(1.0), "overlay.hide was never entered")

        # stop() has returned; the tail is still running.
        self.assertEqual(pipeline.recorder.stops, 1)
        self.assertTrue(pipeline._stopping)
        pipeline.on_hold_start()
        self.assertEqual(start_calls, [])

        hide_release.set()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())
        self.assertFalse(pipeline._stopping)


class _RecordingEngine:
    def __init__(self, text="hallo"):
        self.text = text
        self.calls = []

    def transcribe(self, audio, initial_prompt=None):
        self.calls.append(initial_prompt)
        return self.text


class BuildInitialPromptTests(unittest.TestCase):
    def test_empty_vocabulary_yields_no_prompt(self):
        self.assertIsNone(_build_initial_prompt([]))

    def test_prompt_uses_only_top_terms(self):
        many_terms = [f"Begriff{i}xyz" for i in range(50)]
        prompt = _build_initial_prompt(many_terms)
        self.assertIsNotNone(prompt)
        self.assertIn("Begriff0xyz", prompt)
        # Only the top ~30 terms are used, so a term far down the
        # usage-ordered list must not appear.
        self.assertNotIn("Begriff49xyz", prompt)

    def test_prompt_has_no_language_prefix(self):
        prompt = _build_initial_prompt(["Kubernetes", "München"])
        self.assertEqual(prompt, "Kubernetes, München")

    def test_prompt_is_truncated_at_last_full_term(self):
        # 30 terms x 40 chars comfortably exceeds the 800-char cap, so
        # truncation must actually fire — and it must land on a term
        # boundary rather than slicing a term in half.
        long_terms = [f"Term{i:02d}" + "x" * 34 for i in range(30)]
        full = ", ".join(long_terms)
        self.assertGreater(len(full), 800)

        prompt = _build_initial_prompt(long_terms)
        self.assertLessEqual(len(prompt), 800)
        # 19 full terms (42 chars each incl. separator, minus the trailing
        # separator) fit under the cap; the 20th would push it over.
        expected = ", ".join(long_terms[:19])
        self.assertEqual(prompt, expected)
        self.assertGreater(len(", ".join(long_terms[:20])), 800)
        # No term is cut mid-way: every comma-separated piece is a full term.
        self.assertTrue(all(piece in long_terms for piece in prompt.split(", ")))


class InitialPromptTests(unittest.TestCase):
    def _run(self, words):
        pipeline = _build_pipeline()
        pipeline.engine = _RecordingEngine()
        with patch("flow.pipeline.get_store", return_value=_FakeStore(words)), \
                patch("flow.pipeline.insert_text"), \
                patch("flow.pipeline._frontmost_app", return_value="TestApp"):
            pipeline._process(np.zeros(100, dtype=np.float32), time.monotonic())
        return pipeline.engine.calls[0]

    def test_transcribe_receives_prompt_with_dictionary_terms(self):
        prompt = self._run(["Kubernetes", "München"])
        self.assertIsNotNone(prompt)
        self.assertIn("Kubernetes", prompt)
        self.assertIn("München", prompt)

    def test_transcribe_receives_no_prompt_when_dictionary_empty(self):
        prompt = self._run([])
        self.assertIsNone(prompt)


class UsageBumpTests(unittest.TestCase):
    def _run(self, words, spoken):
        pipeline = _build_pipeline()
        pipeline.engine = _RecordingEngine(text=spoken)
        store = _FakeStore(words)
        with patch("flow.pipeline.get_store", return_value=store), \
                patch("flow.pipeline.insert_text"), \
                patch("flow.pipeline._frontmost_app", return_value="TestApp"):
            pipeline._process(np.zeros(100, dtype=np.float32), time.monotonic())
        return store

    def test_terms_in_final_text_get_their_uses_bumped(self):
        store = self._run(["Kubernetes", "München"],
                          spoken="wir deployen auf kubernetes heute")
        self.assertEqual(store.bumped, [["Kubernetes"]])

    def test_no_bump_when_no_term_appears(self):
        store = self._run(["Kubernetes"], spoken="hallo welt wie geht es")
        self.assertEqual(store.bumped, [])


if __name__ == "__main__":
    unittest.main()
