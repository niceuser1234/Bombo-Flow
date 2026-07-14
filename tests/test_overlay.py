import unittest
from unittest.mock import Mock, patch

from Foundation import NSMakeRect

from flow import overlay


class OverlayWaveMathTests(unittest.TestCase):
    def test_waveform_is_symmetric(self):
        values = overlay.build_symmetric_waveform(level=0.6, phase=0.0, bar_count=24)
        for i in range(len(values) // 2):
            self.assertAlmostEqual(values[i], values[-(i + 1)], places=6)

    def test_idle_wave_has_life_before_first_signal(self):
        a = overlay.build_symmetric_waveform(level=0.0, phase=0.0, bar_count=24)
        b = overlay.build_symmetric_waveform(level=0.0, phase=1.5, bar_count=24)
        self.assertNotEqual(a, b)
        self.assertGreater(max(a), 0.02)

    def test_center_is_stronger_than_edges(self):
        values = overlay.build_symmetric_waveform(level=0.7, phase=0.2, bar_count=24)
        self.assertGreater(values[len(values) // 2], values[0])

    def test_overlay_targets_60_fps(self):
        self.assertEqual(overlay.FPS, 60.0)

    def test_positioned_frame_restores_full_pill_size_after_scale_animation(self):
        screen = NSMakeRect(100.0, 50.0, 1440.0, 900.0)

        frame = overlay.positioned_pill_frame(screen)

        self.assertEqual(frame.size.width, overlay.PILL_WIDTH)
        self.assertEqual(frame.size.height, overlay.PILL_HEIGHT)
        self.assertEqual(
            frame.origin.x,
            screen.origin.x + (screen.size.width - overlay.PILL_WIDTH) / 2.0,
        )
        self.assertEqual(frame.origin.y, screen.origin.y + overlay.BOTTOM_OFFSET)

    def test_window_shadow_is_disabled_to_avoid_rectangular_frame(self):
        self.assertFalse(overlay.WINDOW_HAS_SHADOW)


class OverlayThreadSafeApiTests(unittest.TestCase):
    def test_prewarm_marshals_to_main_thread(self):
        ov = overlay.Overlay(level_source=lambda: 0.0)
        with patch("flow.overlay.AppHelper.callAfter") as call_after:
            ov.prewarm()
        call_after.assert_called_once_with(ov._prewarm_main)

    def test_wave_timer_stops_as_soon_as_hide_begins(self):
        ov = overlay.Overlay(level_source=lambda: 0.0)
        timer = Mock()
        ov._timer = timer

        ov._stop_wave_timer()

        timer.invalidate.assert_called_once_with()
        self.assertIsNone(ov._timer)


if __name__ == "__main__":
    unittest.main()
