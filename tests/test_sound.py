import unittest
from unittest.mock import MagicMock, patch

from flow import sound


def _reset_cache():
    sound._loaded = False
    sound._start_sound = None
    sound._stop_sound = None


class SoundPlaybackTests(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    def test_play_start_plays_loaded_sound(self):
        fake = MagicMock()
        with patch.object(sound, "NSSound") as ns:
            ns.alloc.return_value.initWithContentsOfFile_byReference_.return_value = fake
            sound.play_start()
        fake.play.assert_called_once_with()

    def test_play_stop_plays_loaded_sound(self):
        fake = MagicMock()
        with patch.object(sound, "NSSound") as ns:
            ns.alloc.return_value.initWithContentsOfFile_byReference_.return_value = fake
            sound.play_stop()
        fake.play.assert_called_once_with()

    def test_play_start_does_not_raise_when_nssound_unavailable(self):
        with patch.object(sound, "NSSound", None):
            sound.play_start()  # must not raise

    def test_play_stop_does_not_raise_when_nssound_unavailable(self):
        with patch.object(sound, "NSSound", None):
            sound.play_stop()  # must not raise

    def test_play_start_does_not_raise_when_asset_missing(self):
        with patch.object(sound, "NSSound") as ns:
            ns.alloc.return_value.initWithContentsOfFile_byReference_.return_value = None
            sound.play_start()  # must not raise; sound stays None

    def test_play_start_does_not_raise_when_play_itself_errors(self):
        fake = MagicMock()
        fake.play.side_effect = RuntimeError("boom")
        with patch.object(sound, "NSSound") as ns:
            ns.alloc.return_value.initWithContentsOfFile_byReference_.return_value = fake
            sound.play_start()  # must not raise

    def test_sounds_are_loaded_only_once(self):
        fake = MagicMock()
        with patch.object(sound, "NSSound") as ns:
            ns.alloc.return_value.initWithContentsOfFile_byReference_.return_value = fake
            sound.play_start()
            sound.play_start()
        self.assertEqual(
            ns.alloc.return_value.initWithContentsOfFile_byReference_.call_count, 2
        )  # one for start, one for stop asset — loaded once each, not per call


if __name__ == "__main__":
    unittest.main()
