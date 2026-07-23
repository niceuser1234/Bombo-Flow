import threading
import time
import unittest
from unittest import mock
from unittest.mock import patch

import numpy as np

from flow import audio


class AttackReleaseMathTests(unittest.TestCase):
    def test_attack_reacts_quickly(self):
        prev = 0.10
        incoming = 0.90
        updated = audio.apply_attack_release(prev, incoming)
        self.assertGreater(updated, 0.55)

    def test_release_decays_slowly(self):
        prev = 0.90
        incoming = 0.10
        updated = audio.apply_attack_release(prev, incoming)
        self.assertGreater(updated, incoming)
        self.assertLess(updated, 0.90)

    def test_attack_release_is_clamped(self):
        hi = audio.apply_attack_release(-1.0, 2.0)
        lo = audio.apply_attack_release(2.0, -1.0)
        self.assertGreaterEqual(hi, 0.0)
        self.assertLessEqual(hi, 1.0)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(lo, 1.0)


class RecorderCallbackTests(unittest.TestCase):
    def test_callback_updates_level_with_attack_release(self):
        rec = audio.Recorder()
        rec.level = 0.0
        loud = np.full((160, 1), 0.2, dtype=np.float32)
        rec._callback(loud, 160, None, None)
        first = rec.level
        self.assertGreater(first, 0.4)

        quiet = np.full((160, 1), 0.01, dtype=np.float32)
        rec._callback(quiet, 160, None, None)
        self.assertGreater(rec.level, 0.1)


class _FakeStream:
    def __init__(self, done_event):
        self._done_event = done_event
        self.abort_calls = 0
        self.close_calls = 0

    def abort(self, ignore_errors=False):
        self.abort_calls += 1

    def close(self, ignore_errors=False):
        self.close_calls += 1
        self._done_event.set()


class RecorderAbandonTests(unittest.TestCase):
    def test_abandon_discards_callback_and_closes_stream_async(self):
        rec = audio.Recorder()
        done = threading.Event()
        fake_stream = _FakeStream(done)
        rec._stream = fake_stream
        rec._chunks = [np.zeros((10, 1), dtype=np.float32)]
        rec.level = 0.42

        rec.abandon()

        # Discard must take effect synchronously so the callback can't
        # append more chunks even before the abort thread runs.
        loud = np.full((160, 1), 0.5, dtype=np.float32)
        rec._callback(loud, 160, None, None)
        self.assertEqual(rec._chunks, [])
        self.assertEqual(rec.level, 0.0)

        self.assertTrue(done.wait(2.0), "stream was not closed in background")
        self.assertEqual(fake_stream.abort_calls, 1)
        self.assertEqual(fake_stream.close_calls, 1)


class RecorderMuteTests(unittest.TestCase):
    """The start chime plays into an already-open microphone. Dropping those
    frames keeps it out of the transcript without clipping the sound itself."""

    def test_muted_window_is_dropped_then_capture_resumes(self):
        rec = audio.Recorder()
        loud = np.full((160, 1), 0.5, dtype=np.float32)

        rec.mute_for(0.05)
        rec._callback(loud, 160, None, None)
        self.assertEqual(rec._chunks, [], "chime frames were captured")
        self.assertEqual(rec.level, 0.0)

        time.sleep(0.06)
        rec._callback(loud, 160, None, None)
        self.assertEqual(len(rec._chunks), 1, "capture did not resume")
        self.assertGreater(rec.level, 0.0)

    def test_zero_duration_does_not_mute(self):
        rec = audio.Recorder()
        rec.mute_for(0.0)
        rec._callback(np.full((160, 1), 0.5, dtype=np.float32), 160, None, None)
        self.assertEqual(len(rec._chunks), 1)

    def test_start_clears_a_stale_mute(self):
        rec = audio.Recorder()
        rec.mute_for(30.0)
        with patch.object(audio.sd, "InputStream", return_value=mock.Mock()):
            rec.start()
        self.assertEqual(rec._muted_until, 0.0)


class RecorderStartFailureTests(unittest.TestCase):
    """A stream that was constructed but failed to start must not be left in
    self._stream: the caller's recovery path would then call reset_portaudio()
    — Pa_Terminate — with a live CoreAudio stream, which is exactly the crash
    the recovery exists to avoid."""

    class _FailingStream:
        def __init__(self):
            self.close_calls = 0

        def start(self):
            raise RuntimeError("device is busy")

        def close(self, ignore_errors=False):
            self.close_calls += 1

    def test_failed_start_closes_the_stream_and_leaves_recorder_idle(self):
        rec = audio.Recorder()
        stream = self._FailingStream()
        with patch.object(audio.sd, "InputStream", return_value=stream):
            with self.assertRaises(RuntimeError):
                rec.start()

        self.assertIsNone(rec._stream)
        self.assertFalse(rec.recording)
        self.assertEqual(stream.close_calls, 1)

    def test_successful_start_publishes_the_stream(self):
        rec = audio.Recorder()
        stream = mock.Mock()
        with patch.object(audio.sd, "InputStream", return_value=stream):
            rec.start()

        self.assertIs(rec._stream, stream)
        self.assertTrue(rec.recording)
        stream.start.assert_called_once_with()


class ResetPortaudioTests(unittest.TestCase):
    def test_reset_terminates_then_reinitializes(self):
        with patch.object(audio.sd, "_terminate") as term, \
                patch.object(audio.sd, "_initialize") as init:
            audio.reset_portaudio()
        term.assert_called_once_with()
        init.assert_called_once_with()

    def test_reset_swallows_errors(self):
        with patch.object(audio.sd, "_terminate", side_effect=RuntimeError("boom")):
            audio.reset_portaudio()  # darf nie raisen


if __name__ == "__main__":
    unittest.main()
