import threading
import time
import unittest
from unittest import mock
from unittest.mock import patch

import numpy as np

from flow.pipeline import (
    STOP_TIMEOUT_S,
    Pipeline,
    _build_initial_prompt,
    _dict_fixes,
    _terms_in,
)


class _Overlay:
    def __init__(self):
        self.prewarmed = False
        self.styles = []
        self.flashes = []

    def prewarm(self):
        self.prewarmed = True

    def show(self, style="dictation"):
        self.styles.append(style)

    def set_style(self, style):
        self.styles.append(style)

    def flash(self, message, seconds=2.0):
        self.flashes.append((message, seconds))

    def hide(self):
        pass


class _Recorder:
    samplerate = 16000

    def __init__(self, device_name=None):
        self._recording = False
        self.stops = 0
        self.clears = 0
        self.abandoned = 0
        self.muted_for = None
        self.device_name = device_name

    def mute_for(self, seconds):
        self.muted_for = seconds

    @property
    def recording(self):
        return self._recording

    def start(self):
        self._recording = True

    def stop(self):
        self.stops += 1
        self._recording = False
        return np.zeros(100, dtype=np.float32)

    def clear(self):
        self.clears += 1

    def abandon(self):
        self.abandoned += 1

    def rescue(self):
        return np.zeros(0, dtype=np.float32)

    @property
    def level(self):
        return 0.0


class _Config:
    min_duration_s = 0.3
    max_duration_s = 120.0
    input_device = ""
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
    handsfree_max_s = 600.0


class _FakeStore:
    def __init__(self, words=None):
        self._words = words or []
        self.bumped = []

    def dictionary_words(self):
        return list(self._words)

    def stt_bias_words(self):
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
    pipeline._recover_timer = None
    pipeline._recovery_lock = threading.Lock()
    pipeline._relaunch_pending = False
    pipeline._mode = None
    pipeline._tap_timer = None
    pipeline._handsfree_entry_held = False
    pipeline._relaunched = []
    pipeline._relaunch = lambda reason: pipeline._relaunched.append(reason)
    return pipeline


def _wait_for_recovery(pipeline, timeout=2.0):
    """Audio recovery runs on its own thread so a blocking Pa_Terminate cannot
    stall the hotkey dispatch thread. _schedule_recovery acquires the lock
    synchronously, so waiting for it to clear is race-free."""
    deadline = time.monotonic() + timeout
    while pipeline._recovery_lock.locked() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not pipeline._recovery_lock.locked(), "recovery did not finish"


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
    def test_start_sound_plays_after_the_stream_opens(self):
        """Opening the input device reconfigures CoreAudio and truncates a
        sound that is already playing, which audibly clips the chime's release
        tail. So the stream opens first and the chime follows."""
        pipeline = _build_pipeline()
        pipeline.engine = True
        order = []
        pipeline.recorder.start = lambda: order.append("recorder.start")
        with patch("flow.pipeline.sound.play_start",
                   side_effect=lambda: order.append("play_start")), \
                patch("flow.pipeline.sound.start_duration_s", return_value=0.23):
            pipeline.on_hold_start()
        self.assertEqual(order, ["recorder.start", "play_start"])

    def test_capture_is_muted_for_the_length_of_the_chime(self):
        """The chime plays into an open microphone, so it must be dropped
        from the capture rather than transcribed at the head of the
        recording."""
        pipeline = _build_pipeline()
        pipeline.engine = True
        with patch("flow.pipeline.sound.play_start"), \
                patch("flow.pipeline.sound.start_duration_s", return_value=0.23):
            pipeline.on_hold_start()
        self.assertAlmostEqual(
            pipeline.recorder.muted_for, 0.23 + 0.05, places=6
        )

    def test_no_mute_when_sounds_are_disabled(self):
        """Nothing is played, so nothing may be cut from the recording."""
        pipeline = _build_pipeline()
        pipeline.engine = True
        pipeline.cfg.sounds = False
        with patch("flow.pipeline.sound.play_start") as play_start:
            pipeline.on_hold_start()
        play_start.assert_not_called()
        self.assertIsNone(pipeline.recorder.muted_for)

    def test_start_sound_not_played_when_recorder_start_fails(self):
        """The chime says "recording has started" — it must not sound when it
        has not."""
        pipeline = _build_pipeline()
        pipeline.engine = True
        pipeline.recorder.start = lambda: (_ for _ in ()).throw(RuntimeError("mic busy"))
        with patch("flow.pipeline.sound.play_start") as play_start, \
                patch("flow.pipeline.audio_mod.reset_portaudio"), \
                patch("flow.pipeline.Recorder", _Recorder):
            pipeline.on_hold_start()
            _wait_for_recovery(pipeline)
        play_start.assert_not_called()

    def test_start_sound_is_skipped_when_recovery_is_running(self):
        pipeline = _build_pipeline()
        pipeline.engine = True
        pipeline._recovery_lock.acquire()
        try:
            with patch("flow.pipeline.sound.play_start") as play_start:
                pipeline.on_hold_start()
            play_start.assert_not_called()
            self.assertFalse(pipeline.recorder.recording)
        finally:
            pipeline._recovery_lock.release()

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
    def test_safe_stop_reports_timeout_with_rescued_audio(self):
        pipeline = _build_pipeline()
        pipeline.recorder._recording = True
        # Make stop hang by replacing with a deadlock.
        pipeline.recorder.stop = lambda: time.sleep(999) and np.zeros(0, dtype=np.float32)
        pipeline.recorder.rescue = lambda: np.full(7, 0.1, dtype=np.float32)
        result, timed_out = pipeline._stop_recorder_safe()
        self.assertTrue(timed_out)
        self.assertEqual(len(result), 7)

    def test_safe_stop_returns_audio_on_success(self):
        pipeline = _build_pipeline()
        pipeline.recorder._recording = True
        result, timed_out = pipeline._stop_recorder_safe()
        self.assertFalse(timed_out)
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

    def test_short_terms_do_not_match_inside_longer_words(self):
        """Substring matching made "AI" match "again" and "Go" match
        "Google", inflating the counters that order the initial_prompt and
        drive the least-used eviction."""
        store = self._run(["AI", "Go"], spoken="again i googled it")
        self.assertEqual(store.bumped, [])

    def test_terms_still_match_next_to_punctuation(self):
        store = self._run(["Kubernetes"], spoken="wir nutzen Kubernetes, oder?")
        self.assertEqual(store.bumped, [["Kubernetes"]])


class TermMatchingTests(unittest.TestCase):
    def test_word_boundaries_are_respected(self):
        self.assertEqual(_terms_in(["AI"], "again and again"), [])
        self.assertEqual(_terms_in(["Go"], "I googled it"), [])
        self.assertEqual(_terms_in(["AI"], "the AI model"), ["AI"])

    def test_matching_stays_case_insensitive(self):
        self.assertEqual(_terms_in(["Kubernetes"], "auf kubernetes"), ["Kubernetes"])

    def test_terms_with_punctuation_anchor_correctly(self):
        self.assertEqual(_terms_in([".NET"], "das .NET framework"), [".NET"])
        self.assertEqual(_terms_in(["C++"], "in C++ geschrieben"), ["C++"])

    def test_dict_fixes_counts_only_terms_the_cleanup_added(self):
        # "Kubernetes" was already in the raw transcript — not a fix.
        self.assertEqual(_dict_fixes(["Kubernetes"], "kubernetes läuft",
                                     "Kubernetes läuft"), 0)
        # "n8n" only appears after cleanup — that is a dictionary fix.
        self.assertEqual(_dict_fixes(["n8n"], "acht en acht läuft",
                                     "n8n läuft"), 1)


class AudioRecoveryTests(unittest.TestCase):
    def test_failed_start_resets_portaudio_and_swaps_recorder(self):
        pipeline = _build_pipeline()
        pipeline.engine = True
        broken = pipeline.recorder
        broken.start = lambda: (_ for _ in ()).throw(RuntimeError("stale device"))
        with patch("flow.pipeline.audio_mod.reset_portaudio") as reset, \
                patch("flow.pipeline.Recorder", _Recorder), \
                patch("flow.pipeline.sound.play_start"):
            pipeline.on_hold_start()
            _wait_for_recovery(pipeline)
        reset.assert_called_once_with()
        self.assertIsNot(pipeline.recorder, broken)
        self.assertFalse(pipeline.recorder.recording)

    def test_recovery_runs_off_the_hotkey_dispatch_thread(self):
        """reset_portaudio() blocks in Pa_Terminate exactly when CoreAudio is
        wedged. Running it inline under self._lock would stall the only thread
        servicing hotkey callbacks, wedging the hotkey permanently."""
        pipeline = _build_pipeline()
        pipeline.engine = True
        pipeline.recorder.start = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        caller = threading.current_thread().ident
        ran_on = []
        entered = threading.Event()
        release = threading.Event()

        def blocking_reset():
            ran_on.append(threading.current_thread().ident)
            entered.set()
            release.wait(2.0)  # stands in for a wedged Pa_Terminate

        with patch("flow.pipeline.audio_mod.reset_portaudio",
                   side_effect=blocking_reset), \
                patch("flow.pipeline.Recorder", _Recorder), \
                patch("flow.pipeline.sound.play_start"):
            pipeline.on_hold_start()  # must not block on the teardown
            self.assertTrue(entered.wait(2.0), "recovery never ran")
            self.assertFalse(release.is_set())
            release.set()
            _wait_for_recovery(pipeline)

        self.assertEqual(len(ran_on), 1)
        self.assertNotEqual(ran_on[0], caller)

    def test_live_stream_is_abandoned_instead_of_terminating_portaudio(self):
        """If the recorder still holds an open stream, Pa_Terminate can crash.
        Drop the stream and leave the device snapshot alone."""
        pipeline = _build_pipeline()
        pipeline.engine = True
        stuck = pipeline.recorder
        stuck._recording = True
        stuck.start = lambda: (_ for _ in ()).throw(RuntimeError("half open"))

        with patch("flow.pipeline.audio_mod.reset_portaudio") as reset, \
                patch("flow.pipeline.Recorder", _Recorder), \
                patch("flow.pipeline.sound.play_start"):
            pipeline._schedule_recovery()
            _wait_for_recovery(pipeline)

        reset.assert_not_called()
        self.assertEqual(stuck.abandoned, 1)
        self.assertIsNot(pipeline.recorder, stuck)

    def test_concurrent_recovery_requests_collapse_into_one(self):
        pipeline = _build_pipeline()
        calls = []
        entered = threading.Event()
        release = threading.Event()

        def blocking_reset():
            calls.append(1)
            entered.set()
            release.wait(2.0)

        with patch("flow.pipeline.audio_mod.reset_portaudio",
                   side_effect=blocking_reset), \
                patch("flow.pipeline.Recorder", _Recorder):
            pipeline._schedule_recovery()
            self.assertTrue(entered.wait(2.0))
            # Second request arrives while the first is still tearing down.
            pipeline._schedule_recovery()
            release.set()
            _wait_for_recovery(pipeline)

        self.assertEqual(len(calls), 1)

    def test_silent_capture_resets_portaudio_and_swaps_recorder(self):
        pipeline = _build_pipeline()
        pipeline.recorder._recording = True
        pipeline._press_time = time.monotonic() - 1.0  # > min_duration_s
        old = pipeline.recorder
        with patch("flow.pipeline.audio_mod.is_silent", return_value=True), \
                patch("flow.pipeline.audio_mod.reset_portaudio") as reset, \
                patch("flow.pipeline.Recorder", _Recorder), \
                patch("flow.pipeline.sound.play_stop"):
            pipeline._finish_recording()
            _wait_for_recovery(pipeline)
        reset.assert_called_once_with()
        self.assertIsNot(pipeline.recorder, old)

    def test_timed_out_capture_does_not_reset_portaudio_or_swap(self):
        """A timed-out stop means _stop_recorder_safe already swapped the
        recorder. _finish_recording must NOT reset PortAudio (would tear down
        the still-live abandoned stream) nor swap the recorder again; it asks
        for a relaunch instead of showing an error."""
        pipeline = _build_pipeline()
        pipeline.recorder._recording = True
        pipeline._press_time = time.monotonic() - 1.0  # > min_duration_s
        # (audio, timed_out): the timeout path.
        pipeline._stop_recorder_safe = lambda: (np.zeros(0, dtype=np.float32), True)
        swapped = pipeline.recorder
        with patch("flow.pipeline.audio_mod.reset_portaudio") as reset, \
                patch("flow.pipeline.Recorder", _Recorder), \
                patch("flow.pipeline.sound.play_stop"):
            pipeline._finish_recording()
        reset.assert_not_called()
        self.assertIs(pipeline.recorder, swapped)
        self.assertTrue(pipeline._relaunch_pending)

    def test_second_failure_cancels_the_previous_recovery_timer(self):
        """Two failures inside the 2s error window left the first timer
        orphaned; it fired later and forced 'idle' over whatever was running."""
        pipeline = _build_pipeline()
        first = mock.Mock()
        pipeline._recover_timer = first
        pipeline._enter_error_then_idle()
        try:
            first.cancel.assert_called_once_with()
            self.assertIsNot(pipeline._recover_timer, first)
        finally:
            pipeline._recover_timer.cancel()

    def test_new_recording_cancels_stale_recovery_timer(self):
        """A recording started within the 2s error->idle window must cancel the
        pending recovery timer so its set_state('idle') cannot clobber the
        recording's menubar state."""
        pipeline = _build_pipeline()
        pipeline.engine = True
        timer = mock.Mock()
        pipeline._recover_timer = timer
        pipeline.on_hold_start()
        timer.cancel.assert_called_once_with()
        self.assertIsNone(pipeline._recover_timer)


class PersistenceOrderTests(unittest.TestCase):
    def test_transcript_is_stored_even_when_inserting_fails(self):
        """Insertion is the step most likely to fail for reasons outside our
        control. A transcript that only lives in the history is recoverable;
        one that was never written down is gone for good."""
        pipeline = _build_pipeline()
        pipeline.engine = _RecordingEngine(text="hallo welt")
        store = _FakeStore()
        stored = []
        store.add_dictation = lambda raw, text, **kw: stored.append((raw, text, kw))

        with patch("flow.pipeline.get_store", return_value=store), \
                patch("flow.pipeline.insert_text",
                      side_effect=RuntimeError("no pasteboard")), \
                patch("flow.pipeline._frontmost_app", return_value="TestApp"):
            with self.assertRaises(RuntimeError):
                pipeline._process(np.zeros(100, dtype=np.float32), time.monotonic())

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0][1], "hallo welt")

    def test_duration_uses_the_recorder_sample_rate(self):
        pipeline = _build_pipeline()
        pipeline.engine = _RecordingEngine(text="hallo welt")
        store = _FakeStore()
        stored = []
        store.add_dictation = lambda raw, text, **kw: stored.append(kw)

        with patch("flow.pipeline.get_store", return_value=store), \
                patch("flow.pipeline.insert_text"), \
                patch("flow.pipeline._frontmost_app", return_value="TestApp"):
            pipeline._process(np.zeros(8000, dtype=np.float32), time.monotonic())

        self.assertAlmostEqual(stored[0]["duration_s"], 0.5)


if __name__ == "__main__":
    unittest.main()


class _HangingRecorder(_Recorder):
    """start()/stop() block like a wedged CoreAudio HAL."""

    def __init__(self, device_name=None, hang_start=False, hang_stop=False):
        super().__init__(device_name)
        self._gate = threading.Event()
        self.hang_start = hang_start
        self.hang_stop = hang_stop
        self.chunks_audio = np.full(16000, 0.1, dtype=np.float32)

    def start(self):
        if self.hang_start:
            self._gate.wait(10)
        super().start()

    def stop(self):
        if self.hang_stop:
            self._gate.wait(10)
        return super().stop()

    def rescue(self):
        return self.chunks_audio


class FreezeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.p = _build_pipeline()
        self.p.engine = object()
        self.queued = []
        self.p._queue = type("_Q", (), {"put": lambda s, item: self.queued.append(item)})()

    def test_hanging_start_returns_and_requests_relaunch(self):
        self.p.recorder = _HangingRecorder(hang_start=True)
        with patch("flow.pipeline.START_TIMEOUT_S", 0.05), patch("flow.pipeline.sound"):
            t0 = time.monotonic()
            self.p.on_hold_start()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(self.p._relaunch_pending)
        self.assertEqual(self.queued, [None])  # sentinel only — nothing to rescue

    def test_hanging_stop_rescues_audio_then_relaunches(self):
        self.p.recorder = _HangingRecorder(hang_stop=True)
        self.p.recorder._recording = True
        self.p._press_time = time.monotonic() - 2.0  # a real 2 s dictation
        with patch("flow.pipeline.STOP_TIMEOUT_S", 0.05), patch("flow.pipeline.sound"):
            self.p._finish_recording()
        self.assertEqual(len(self.queued), 2)
        captured, _released_at, mode = self.queued[0]
        self.assertEqual(len(captured), 16000)
        self.assertEqual(mode, "dictation")
        self.assertIsNone(self.queued[1])
        self.assertTrue(self.p._relaunch_pending)

    def test_presses_are_ignored_once_relaunch_is_pending(self):
        self.p._relaunch_pending = True
        self.p.on_hold_start()
        self.assertFalse(self.p.recorder.recording)

    def test_relaunch_waits_for_queued_dictation(self):
        import queue as queue_mod

        p = _build_pipeline()
        p._queue = queue_mod.Queue()
        order = []
        p._process = lambda captured, released_at, mode="dictation": order.append("processed")
        p._relaunch = lambda reason: order.append("relaunch")
        p._load_model = lambda: True
        p._queue.put((np.zeros(10, dtype=np.float32), 0.0, "dictation"))
        p._queue.put(None)
        p._worker()  # returns after the sentinel
        self.assertEqual(order, ["processed", "relaunch"])

    def test_hanging_portaudio_reset_requests_relaunch_and_frees_recovery(self):
        """Pa_Terminate blocks in exactly the wedged state recovery exists
        for. Unbounded, the recovery lock was never released and every later
        press was refused without a relaunch."""
        gate = threading.Event()
        self.addCleanup(gate.set)
        with patch("flow.pipeline.RESET_TIMEOUT_S", 0.05), \
                patch("flow.pipeline.audio_mod.reset_portaudio",
                      side_effect=lambda: gate.wait(5)), \
                patch("flow.pipeline.Recorder", _Recorder):
            self.p._schedule_recovery()
            _wait_for_recovery(self.p)
        self.assertTrue(self.p._relaunch_pending)
        self.assertEqual(self.queued, [None])
        self.assertFalse(self.p._recovery_lock.locked())

    def test_recovery_is_not_started_once_relaunch_is_pending(self):
        self.p._relaunch_pending = True
        with patch("flow.pipeline.audio_mod.reset_portaudio") as reset:
            self.p._schedule_recovery()
            _wait_for_recovery(self.p)
        reset.assert_not_called()

    def test_finish_while_relaunch_pending_queues_nothing(self):
        """A start that returned late (after its timeout requested the
        relaunch) leaves a live recording behind. It must not be queued
        behind the relaunch sentinel."""
        self.p._relaunch_pending = True
        self.p.recorder._recording = True
        self.p._press_time = time.monotonic() - 2.0
        with patch("flow.pipeline.audio_mod.is_silent", return_value=False), \
                patch("flow.pipeline.sound"):
            self.p._finish_recording()
        self.assertFalse(self.p.recorder.recording)
        self.assertEqual(self.queued, [])

    def test_silent_finish_while_relaunch_pending_skips_recovery(self):
        self.p._relaunch_pending = True
        self.p.recorder._recording = True
        self.p._press_time = time.monotonic() - 2.0
        with patch("flow.pipeline.audio_mod.is_silent", return_value=True), \
                patch("flow.pipeline.audio_mod.reset_portaudio") as reset, \
                patch.object(self.p, "_schedule_recovery") as schedule, \
                patch("flow.pipeline.sound"):
            self.p._finish_recording()
        schedule.assert_not_called()
        reset.assert_not_called()
        self.assertEqual(self.queued, [])


class HandsFreeTests(unittest.TestCase):
    def setUp(self):
        self.p = _build_pipeline()
        self.p.engine = object()
        self.states = []
        self.p.set_state = self.states.append
        self.queued = []
        self.p._queue = type("_Q", (), {"put": lambda s, item: self.queued.append(item)})()
        self.sound = patch("flow.pipeline.sound").start()
        self.sound.start_duration_s.return_value = 0.2
        self.sound.handsfree_duration_s.return_value = 0.3
        # The fake recorder returns zeros, which the silence check would send
        # down the recovery path instead of the queue.
        patch("flow.pipeline.audio_mod.is_silent", return_value=False).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self._cancel_timers)

    def _cancel_timers(self):
        for timer in (self.p._tap_timer, self.p._max_timer):
            if timer is not None and hasattr(timer, "cancel"):
                timer.cancel()

    def _tap(self):
        self.p.on_hold_start("dictation")
        self.p._press_time = time.monotonic() - 0.1  # a 0.1 s tap
        self.p.on_hold_end()

    def test_double_tap_enters_handsfree_on_same_stream(self):
        self._tap()
        self.p.on_hold_start("dictation")
        self.assertEqual(self.p._mode, "handsfree")
        self.assertEqual(self.p.recorder.stops, 0)  # stream never restarted
        self.assertEqual(self.p.recorder.clears, 1)
        self.assertIn("handsfree", self.states)
        self.sound.play_handsfree.assert_called_once()

    def test_release_is_ignored_in_handsfree(self):
        self._tap()
        self.p.on_hold_start("dictation")
        self.p.on_hold_end()
        self.assertTrue(self.p.recorder.recording)

    def test_next_press_stops_handsfree_and_queues_it(self):
        self._tap()
        self.p.on_hold_start("dictation")
        self.p._press_time = time.monotonic() - 5.0
        self.p.on_hold_start("dictation")
        self.assertFalse(self.p.recorder.recording)
        self.assertEqual(self.queued[-1][2], "handsfree")

    def test_single_tap_is_discarded_after_grace(self):
        with patch("flow.pipeline.DOUBLE_TAP_S", 0.05):
            self._tap()
            self.assertTrue(self.p.recorder.recording)  # grace window open
            time.sleep(0.2)
        self.assertFalse(self.p.recorder.recording)
        self.assertEqual(self.queued, [])

    def test_long_hold_is_a_normal_dictation(self):
        self.p.on_hold_start("dictation")
        self.p._press_time = time.monotonic() - 2.0
        self.p.on_hold_end()
        self.assertEqual(self.queued[-1][2], "dictation")
        self.assertIsNone(self.p._tap_timer)

    def test_handsfree_uses_its_own_max_duration(self):
        self._tap()
        with patch("flow.pipeline.threading.Timer") as timer:
            self.p.on_hold_start("dictation")
        self.assertEqual(timer.call_args.args[0], 600.0)

    def test_mode_change_turns_dictation_into_command(self):
        self.p.on_hold_start("dictation")
        self.p.on_mode_change("command")
        self.assertEqual(self.p._mode, "command")
        self.assertEqual(self.p.overlay.styles[-1], "command")

    def test_mode_change_ignored_once_handsfree_is_released(self):
        self._tap()
        self.p.on_hold_start("dictation")
        self.p.on_hold_end()  # hands-free dictation is now running
        self.p.on_mode_change("command")
        self.assertEqual(self.p._mode, "handsfree")

    def test_control_during_the_handsfree_entry_hold_becomes_a_rewrite(self):
        """Spec: holding fn+Control never triggers hands-free, in either
        order — so fn, then Control on the second press of a double tap is
        a rewrite."""
        self._tap()
        self.p.on_hold_start("dictation")  # enters hands-free, still held
        self.p.on_mode_change("command")
        self.assertEqual(self.p._mode, "command")
        self.assertEqual(self.p.overlay.styles[-1], "command")
        self.assertEqual(self.states[-1], "recording")
        self.assertEqual(self.p._max_timer.interval, 120.0)
        self.p._press_time = time.monotonic() - 2.0
        self.p.on_hold_end()  # releasing ends it like any command
        self.assertFalse(self.p.recorder.recording)
        self.assertEqual(self.queued[-1][2], "command")

    def test_command_press_never_enters_handsfree(self):
        self._tap()
        self.p.on_hold_start("command")  # Control held on the second press
        self.assertNotEqual(self.p._mode, "handsfree")

    # -- additions beyond the brief --

    def test_command_press_in_grace_reuses_the_open_stream(self):
        """Stopping and restarting CoreAudio back-to-back is what the tap
        window exists to avoid; the command records on the tap's stream."""
        self._tap()
        old_max = self.p._max_timer
        self.p.on_hold_start("command")
        self.assertEqual(self.p._mode, "command")
        self.assertIsNone(self.p._tap_timer)
        self.assertTrue(self.p.recorder.recording)
        self.assertEqual(self.p.recorder.stops, 0)  # stream never restarted
        self.assertEqual(self.p.recorder.clears, 1)  # tap audio dropped
        self.assertEqual(self.queued, [])
        self.assertNotIn("idle", self.states)  # nothing was discarded
        self.assertEqual(self.states[-1], "recording")
        self.assertEqual(self.p.overlay.styles[-1], "command")
        self.sound.play_stop.assert_not_called()
        self.assertEqual(self.sound.play_start.call_count, 2)
        self.assertAlmostEqual(self.p.recorder.muted_for, 0.2 + 0.05)
        self.assertLess(time.monotonic() - self.p._press_time, 0.5)
        self.assertIsNot(self.p._max_timer, old_max)
        self.assertEqual(self.p._max_timer.interval, 120.0)

    def test_command_press_in_grace_then_release_queues_command(self):
        self._tap()
        self.p.on_hold_start("command")
        self.p._press_time = time.monotonic() - 2.0
        self.p.on_hold_end()
        self.assertFalse(self.p.recorder.recording)
        self.assertEqual(self.queued[-1][2], "command")

    def test_command_recording_is_queued_as_command(self):
        self.p.on_hold_start("dictation")
        self.p.on_mode_change("command")
        self.p._press_time = time.monotonic() - 2.0
        self.p.on_hold_end()
        self.assertIsNone(self.p._tap_timer)
        self.assertEqual(self.queued[-1][2], "command")
        self.assertIsNone(self.p._mode)

    def test_handsfree_restarts_the_max_timer(self):
        self._tap()
        old = self.p._max_timer
        self.p.on_hold_start("dictation")
        self.assertIsNot(self.p._max_timer, old)
        self.assertFalse(old.is_alive() and not old.finished.is_set())
        self.assertEqual(self.p._max_timer.interval, 600.0)

    def test_discard_queues_nothing_and_goes_idle(self):
        self._tap()
        self.p._finish_recording(discard=True)
        self.assertFalse(self.p.recorder.recording)
        self.assertEqual(self.queued, [])
        self.assertEqual(self.states[-1], "idle")
        self.sound.play_stop.assert_called_once()
        self.assertIsNone(self.p._mode)
        self.assertIsNone(self.p._tap_timer)

    def test_superseded_grace_timer_cannot_discard_handsfree(self):
        """The grace timer can fire just as the second press switches to
        hands-free and then wait on the lock; it must not drop the new
        hands-free recording once it gets in."""
        self._tap()
        stale = self.p._tap_timer
        self.p.on_hold_start("dictation")  # hands-free; cancels `stale`
        self.p._finish_recording(discard=True, tap=stale)
        self.assertTrue(self.p.recorder.recording)
        self.assertEqual(self.p._mode, "handsfree")

    def test_short_press_without_mode_finishes_immediately(self):
        """Recordings not started through on_hold_start (no mode) keep the
        old release-to-finish behaviour."""
        self.p.recorder._recording = True
        self.p._press_time = time.monotonic() - 0.1
        self.p.on_hold_end()
        self.assertFalse(self.p.recorder.recording)
        self.assertIsNone(self.p._tap_timer)

    def test_force_stop_finishes_handsfree(self):
        self._tap()
        self.p.on_hold_start("dictation")
        self.p._press_time = time.monotonic() - 10.0
        self.p._force_stop()
        self.assertFalse(self.p.recorder.recording)
        self.assertEqual(self.queued[-1][2], "handsfree")

    def test_timed_out_stop_in_handsfree_queues_handsfree(self):
        rec = _HangingRecorder(hang_stop=True)
        self.p.recorder = rec
        self._tap()
        self.p.on_hold_start("dictation")
        self.p._press_time = time.monotonic() - 3.0
        with patch("flow.pipeline.STOP_TIMEOUT_S", 0.05), \
                patch("flow.pipeline.Recorder", _Recorder):
            self.p.on_hold_start("dictation")
        rec._gate.set()
        self.assertEqual(self.queued[0][2], "handsfree")
        self.assertIsNone(self.queued[1])
        self.assertTrue(self.p._relaunch_pending)


class RewritePathTests(unittest.TestCase):
    def setUp(self):
        self.p = _build_pipeline()
        self.p.engine = type("E", (), {"transcribe": lambda s, a, initial_prompt=None: "mach Stichpunkte"})()
        self.p.cleaner = mock.Mock()
        self.p.cleaner.available = True
        self.p.cleaner.rewrite.return_value = "- a\n- b"
        self.store = _FakeStore()
        self.store.add_dictation = mock.Mock()
        patch("flow.pipeline.get_store", return_value=self.store).start()
        self.grab = patch("flow.pipeline.transform.grab_selection", return_value="a b").start()
        self.insert = patch("flow.pipeline.insert_text").start()
        patch("flow.pipeline._frontmost_app", return_value="Notes").start()
        self.addCleanup(patch.stopall)

    def _run(self):
        self.p._process(np.zeros(16000, dtype=np.float32), time.monotonic(), "command")

    def test_success_pastes_rewrite_and_records_it(self):
        self._run()
        self.p.cleaner.rewrite.assert_called_once_with("a b", "mach Stichpunkte")
        self.insert.assert_called_once_with("- a\n- b", mode="paste")
        self.assertEqual(self.store.add_dictation.call_args.kwargs["kind"], "rewrite")
        args = self.store.add_dictation.call_args.args
        self.assertEqual(args[0], "mach Stichpunkte")
        self.assertEqual(args[1], "- a\n- b")

    def test_grab_selection_raising_flashes_failure(self):
        self.grab.side_effect = RuntimeError("boom")
        self._run()
        self.insert.assert_not_called()
        self.assertIn(("Rewrite failed", 2.0), self.p.overlay.flashes)

    def test_transcribe_raising_flashes_failure(self):
        def boom(s, a, initial_prompt=None):
            raise RuntimeError("boom")
        self.p.engine = type("E", (), {"transcribe": boom})()
        self._run()
        self.insert.assert_not_called()
        self.assertIn(("Rewrite failed", 2.0), self.p.overlay.flashes)

    def test_no_selection_pastes_nothing(self):
        self.grab.return_value = None
        self._run()
        self.insert.assert_not_called()
        self.assertIn(("Select some text first", 2.0), self.p.overlay.flashes)

    def test_failed_rewrite_pastes_nothing(self):
        self.p.cleaner.rewrite.return_value = None
        self._run()
        self.insert.assert_not_called()
        self.assertIn(("Rewrite failed", 2.0), self.p.overlay.flashes)

    def test_empty_instruction_pastes_nothing(self):
        self.p.engine = type("E", (), {"transcribe": lambda s, a, initial_prompt=None: ""})()
        self._run()
        self.grab.assert_not_called()
        self.assertIn(("Didn't catch that", 2.0), self.p.overlay.flashes)

    def test_no_api_key_pastes_nothing(self):
        self.p.cleaner.available = False
        self._run()
        self.grab.assert_not_called()
        self.assertIn(("Rewrite needs an API key", 2.0), self.p.overlay.flashes)

    def test_newer_recording_keeps_the_overlay_on_success(self):
        """A press while the rewrite runs owns the overlay; hiding it would
        blank that recording's pill."""
        self.p.recorder._recording = True
        self.p.overlay.hide = mock.Mock()
        self._run()
        self.p.overlay.hide.assert_not_called()
        self.insert.assert_called_once_with("- a\n- b", mode="paste")

    def test_newer_recording_keeps_the_overlay_on_failure(self):
        self.p.recorder._recording = True
        self.grab.return_value = None
        self._run()
        self.assertEqual(self.p.overlay.flashes, [])

    def test_newer_recording_keeps_the_overlay_when_rewrite_raises(self):
        self.p.recorder._recording = True
        self.grab.side_effect = RuntimeError("boom")
        self._run()
        self.assertEqual(self.p.overlay.flashes, [])

    def test_dictation_mode_still_uses_cleanup_path(self):
        self.p._process(np.zeros(16000, dtype=np.float32), time.monotonic(), "dictation")
        self.p.cleaner.rewrite.assert_not_called()
