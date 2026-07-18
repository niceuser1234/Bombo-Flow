import threading
import unittest
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
