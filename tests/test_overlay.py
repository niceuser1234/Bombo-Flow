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


class OverlayStyleTests(unittest.TestCase):
    def test_dictation_layout_is_unchanged(self):
        lay = overlay.layout_for("dictation")
        self.assertEqual(lay["width"], overlay.PILL_WIDTH)
        self.assertTrue(lay["show_wave"])
        self.assertFalse(lay["show_lock"])
        self.assertIsNone(lay["label"])

    def test_handsfree_layout_shows_lock_and_wave(self):
        lay = overlay.layout_for("handsfree")
        self.assertTrue(lay["show_lock"] and lay["show_wave"])
        self.assertGreater(lay["width"], overlay.PILL_WIDTH)

    def test_command_layout_asks_the_question(self):
        lay = overlay.layout_for("command")
        self.assertEqual(lay["label"], "What should I do with it?")
        self.assertTrue(lay["show_wave"])

    def test_message_layout_has_no_wave(self):
        lay = overlay.layout_for("message")
        self.assertFalse(lay["show_wave"] or lay["show_lock"])

    def test_handsfree_pill_is_green_not_blue(self):
        r, g, b, _a = overlay.pill_colors("handsfree")[0]
        self.assertTrue(g > r and g > b)

    def test_unknown_style_falls_back_to_dictation(self):
        self.assertEqual(overlay.layout_for("bogus"), overlay.layout_for("dictation"))

    def test_positioned_frame_centres_a_wider_pill(self):
        screen = NSMakeRect(0.0, 0.0, 1000.0, 800.0)
        frame = overlay.positioned_pill_frame(screen, 330.0)
        self.assertEqual(frame.size.width, 330.0)
        self.assertEqual(frame.origin.x, 335.0)

    def test_new_api_marshals_to_main_thread(self):
        ov = overlay.Overlay(level_source=lambda: 0.0)
        with patch("flow.overlay.AppHelper.callAfter") as call_after:
            ov.show("handsfree")
            ov.set_style("command")
            ov.flash("hi", 1.0)
        self.assertEqual(
            [c.args for c in call_after.call_args_list],
            [
                (ov._show_main, "handsfree"),
                (ov._set_style_main, "command"),
                (ov._flash_main, "hi", 1.0),
            ],
        )


class OverlayLivePillTests(unittest.TestCase):
    def _live_overlay(self):
        ov = overlay.Overlay(level_source=lambda: 0.0)
        ov._window = Mock()
        ov._window.isVisible.return_value = True
        ov._pill, ov._wave, ov._label, ov._lock = Mock(), Mock(), Mock(), Mock()
        ov._timer_target = Mock()
        return ov

    def test_set_style_on_visible_pill_sets_frame_via_animator(self):
        ov = self._live_overlay()
        with patch("flow.overlay.NSAnimationContext"):
            ov._set_style_main("command")
        ov._window.animator().setFrame_display_.assert_called_once()
        width = ov._window.animator().setFrame_display_.call_args.args[0].size.width
        self.assertEqual(width, overlay.WIDE_PILL_WIDTH)
        ov._window.setFrame_display_.assert_not_called()

    def test_flash_on_visible_pill_does_not_restart_pop_in(self):
        ov = self._live_overlay()
        with patch("flow.overlay.NSAnimationContext"), patch(
            "flow.overlay.NSTimer"
        ) as timer:
            ov._flash_main("Rewriting...", 2.0)
        ov._window.setAlphaValue_.assert_not_called()
        ov._window.orderFrontRegardless.assert_not_called()
        ov._label.setStringValue_.assert_called_with("Rewriting...")
        self.assertEqual(ov._style, "message")
        timer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_.assert_called_once()

    def test_show_other_style_on_visible_pill_does_not_restart_pop_in(self):
        ov = self._live_overlay()
        with patch("flow.overlay.NSAnimationContext"):
            ov._show_main("command")
        ov._window.setAlphaValue_.assert_not_called()
        self.assertEqual(ov._style, "command")


if __name__ == "__main__":
    unittest.main()
