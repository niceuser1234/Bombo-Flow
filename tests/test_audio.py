import unittest

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


if __name__ == "__main__":
    unittest.main()
