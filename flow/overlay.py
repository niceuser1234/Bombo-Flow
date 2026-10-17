"""Floating native macOS recording overlay with symmetric live waveform."""

import logging
import math

import objc
from AppKit import (
    NSAnimationContext,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSFont,
    NSGradient,
    NSMakeRect,
    NSScreen,
    NSTextField,
    NSTimer,
    NSView,
    NSWindow,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSObject, NSPointInRect
from PyObjCTools import AppHelper

log = logging.getLogger(__name__)

# Pill geometry (points).
PILL_WIDTH = 198.0
PILL_HEIGHT = 42.0
PILL_MARGIN = 9.0
BOTTOM_OFFSET = 80.0
BAR_COUNT = 24
FPS = 60.0
IDLE_LEVEL = 0.07
SHOW_DURATION = 0.18
HIDE_DURATION = 0.16
SHOW_SCALE = 0.94
HIDE_SCALE = 0.97
WINDOW_HAS_SHADOW = False

# NSWindowCollectionBehavior: canJoinAllSpaces | fullScreenAuxiliary.
_COLLECTION_BEHAVIOR = (1 << 0) | (1 << 8)
_WINDOW_LEVEL = 25


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def build_symmetric_waveform(
    level: float,
    phase: float,
    bar_count: int = BAR_COUNT,
    idle_floor: float = IDLE_LEVEL,
) -> list[float]:
    clamped_level = _clamp01(level)
    clamped_idle = _clamp01(idle_floor)
    center = (bar_count - 1) / 2.0
    out = []
    for i in range(bar_count):
        distance = abs(i - center) / max(center, 1.0)
        falloff = max(0.0, 1.0 - distance**1.7)
        ripple = 0.72 + 0.28 * math.sin(phase * 2.0 + (1.0 - distance) * 7.2)
        envelope = clamped_idle + clamped_level * (1.0 - clamped_idle)
        value = 0.02 + envelope * falloff * ripple
        out.append(_clamp01(value))
    return out


def positioned_pill_frame(screen_frame, width=PILL_WIDTH):
    """Return the canonical full-size pill frame for a screen."""
    x = screen_frame.origin.x + (screen_frame.size.width - width) / 2.0
    y = screen_frame.origin.y + BOTTOM_OFFSET
    return NSMakeRect(x, y, width, PILL_HEIGHT)


WIDE_PILL_WIDTH = 330.0
HANDSFREE_PILL_WIDTH = 232.0
LOCK_SLOT = 26.0
STYLE_CHANGE_DURATION = 0.08
PULSE_SPEED = 0.09  # radians per tick of the border pulse (~1 s period at 60 fps)

_LAYOUTS = {
    "dictation": {"width": PILL_WIDTH, "show_wave": True, "show_lock": False, "label": None},
    "handsfree": {"width": HANDSFREE_PILL_WIDTH, "show_wave": True, "show_lock": True, "label": None},
    "command": {"width": WIDE_PILL_WIDTH, "show_wave": True, "show_lock": False,
                "label": "What should I do with it?"},
    "message": {"width": WIDE_PILL_WIDTH, "show_wave": False, "show_lock": False, "label": ""},
}

_COLORS = {
    # (top, bottom, border) RGBA
    "dictation": ((0.12, 0.14, 0.20, 0.96), (0.07, 0.09, 0.13, 0.96), (0.26, 0.43, 0.58, 0.28)),
    "handsfree": ((0.10, 0.27, 0.19, 0.96), (0.05, 0.16, 0.11, 0.96), (0.36, 0.78, 0.55, 0.55)),
    "command": ((0.12, 0.14, 0.20, 0.96), (0.07, 0.09, 0.13, 0.96), (0.26, 0.43, 0.58, 0.28)),
    "message": ((0.12, 0.14, 0.20, 0.96), (0.07, 0.09, 0.13, 0.96), (0.26, 0.43, 0.58, 0.28)),
}


def layout_for(style: str) -> dict:
    return dict(_LAYOUTS.get(style, _LAYOUTS["dictation"]))


def pill_colors(style: str):
    return _COLORS.get(style, _COLORS["dictation"])


class _PillBackgroundView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(_PillBackgroundView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.style = "dictation"
        self.pulse = 0.0
        return self

    def drawRect_(self, _rect):
        top, bottom, border = pill_colors(self.style)
        bounds = self.bounds()
        radius = bounds.size.height / 2.0
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, radius, radius
        )
        gradient = NSGradient.alloc().initWithColors_(
            [
                NSColor.colorWithCalibratedRed_green_blue_alpha_(*top),
                NSColor.colorWithCalibratedRed_green_blue_alpha_(*bottom),
            ]
        )
        gradient.drawInBezierPath_angle_(path, 90.0)

        br, bg, bb, ba = border
        handsfree = self.style == "handsfree"
        if handsfree:
            ba = ba * (0.55 + 0.45 * self.pulse)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(br, bg, bb, ba).set()
        path.setLineWidth_(1.5 if handsfree else 1.0)
        path.stroke()

        inset = 1.0
        inner_rect = NSMakeRect(
            bounds.origin.x + inset,
            bounds.origin.y + inset,
            bounds.size.width - 2.0 * inset,
            bounds.size.height - 2.0 * inset,
        )
        inner = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            inner_rect, max(1.0, radius - inset), max(1.0, radius - inset)
        )
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.08).set()
        inner.setLineWidth_(1.0)
        inner.stroke()


class _WaveformView(NSView):
    """Custom view drawing BAR_COUNT rounded mirrored bars from level list."""

    def initWithFrame_(self, frame):
        self = objc.super(_WaveformView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._levels = build_symmetric_waveform(0.0, 0.0, BAR_COUNT)
        return self

    def setLevels_(self, levels):
        if not levels:
            return
        self._levels = [_clamp01(float(v)) for v in levels][:BAR_COUNT]

    def isFlipped(self):
        return False

    def drawRect_(self, _rect):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        if w <= 0 or h <= 0:
            return

        slot = w / max(len(self._levels), 1)
        bar_w = min(4.4, slot * 0.62)
        gap = (slot - bar_w) / 2.0
        avail = h - 6.0
        min_bar = 3.0

        for i, level in enumerate(self._levels):
            bar_h = min_bar + _clamp01(level) * (avail - min_bar)
            x = i * slot + gap
            y = (h - bar_h) / 2.0
            glow_alpha = 0.25 + 0.35 * level
            fill_alpha = 0.42 + 0.55 * level

            glow = NSMakeRect(x - 0.8, y - 0.7, bar_w + 1.6, bar_h + 1.4)
            glow_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                glow, (bar_w + 1.6) / 2.0, (bar_w + 1.6) / 2.0
            )
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.29, 0.79, 0.96, glow_alpha
            ).set()
            glow_path.fill()

            bar = NSMakeRect(x, y, bar_w, bar_h)
            bar_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                bar, bar_w / 2.0, bar_w / 2.0
            )
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.56, 0.90, 1.00, fill_alpha
            ).set()
            bar_path.fill()


class _TimerTarget(NSObject):
    """ObjC-visible NSTimer target that ticks the owning Overlay."""

    def initWithOverlay_(self, overlay):
        self = objc.super(_TimerTarget, self).init()
        if self is None:
            return None
        self._overlay = overlay
        return self

    def tick_(self, _timer):
        self._overlay._on_tick()

    def finishHide_(self, _timer):
        self._overlay._finish_hide_main()

    def autoHide_(self, _timer):
        self._overlay._hide_main()


class Overlay:
    def __init__(self, level_source):
        # level_source: () -> float, called on the main thread.
        self._level_source = level_source
        self._window = None
        self._content = None
        self._pill = None
        self._wave = None
        self._timer = None
        self._timer_target = None
        self._hide_timer = None
        self._phase = 0.0
        self._pulse_phase = 0.0
        self._style = "dictation"
        self._label = None
        self._lock = None
        self._auto_hide_timer = None

    # -- public API (thread-safe) --

    def prewarm(self):
        AppHelper.callAfter(self._prewarm_main)

    def show(self, style="dictation"):
        AppHelper.callAfter(self._show_main, style)

    def set_style(self, style):
        AppHelper.callAfter(self._set_style_main, style)

    def flash(self, message, seconds=2.0):
        """Show a short status text; auto-hides after `seconds` (None = stay
        until hide() or the next show/flash)."""
        AppHelper.callAfter(self._flash_main, message, seconds)

    def hide(self):
        AppHelper.callAfter(self._hide_main)

    # -- main-thread implementation --

    def _build_window(self):
        frame = NSMakeRect(0, 0, PILL_WIDTH, PILL_HEIGHT)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        window.setLevel_(_WINDOW_LEVEL)
        window.setOpaque_(False)
        window.setBackgroundColor_(NSColor.clearColor())
        # NSWindow shadows follow the transparent rectangular window rather
        # than the painted pill and therefore look like a visible frame.
        window.setHasShadow_(WINDOW_HAS_SHADOW)
        window.setIgnoresMouseEvents_(True)
        window.setCollectionBehavior_(_COLLECTION_BEHAVIOR)
        window.setAlphaValue_(0.0)

        content = NSView.alloc().initWithFrame_(frame)
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(PILL_HEIGHT / 2.0)
        content.layer().setMasksToBounds_(False)
        window.setContentView_(content)

        pill = _PillBackgroundView.alloc().initWithFrame_(frame)
        pill.setAutoresizingMask_(18)
        content.addSubview_(pill)

        wave_frame = NSMakeRect(
            PILL_MARGIN,
            PILL_MARGIN,
            PILL_WIDTH - 2 * PILL_MARGIN,
            PILL_HEIGHT - 2 * PILL_MARGIN,
        )
        wave = _WaveformView.alloc().initWithFrame_(wave_frame)
        wave.setWantsLayer_(True)
        wave.setAutoresizingMask_(0)  # laid out explicitly per style
        content.addSubview_(wave)

        label = NSTextField.labelWithString_("")
        label.setFont_(NSFont.systemFontOfSize_weight_(13.0, 0.3))
        label.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.92))
        label.setAutoresizingMask_(0)
        label.setHidden_(True)
        content.addSubview_(label)

        lock = NSTextField.labelWithString_("\U0001F512")
        lock.setFont_(NSFont.systemFontOfSize_(14.0))
        lock.setHidden_(True)
        content.addSubview_(lock)

        self._window = window
        self._content = content
        self._pill = pill
        self._wave = wave
        self._label = label
        self._lock = lock

    def _position_window(self, width=None, animated=False):
        if width is None:
            width = layout_for(self._style)["width"]
        mouse = NSEvent.mouseLocation()
        screen = None
        for scr in NSScreen.screens():
            if NSPointInRect(mouse, scr.frame()):
                screen = scr
                break
        if screen is None:
            screen = NSScreen.mainScreen()
        frame = positioned_pill_frame(screen.frame(), width)
        if animated:
            # Through the animator so it supersedes an in-flight show animation.
            NSAnimationContext.beginGrouping()
            NSAnimationContext.currentContext().setDuration_(STYLE_CHANGE_DURATION)
            self._window.animator().setFrame_display_(frame, True)
            NSAnimationContext.endGrouping()
        else:
            self._window.setFrame_display_(frame, False)

    def _scaled_frame(self, frame, scale):
        scale = _clamp01(scale) if scale <= 1.0 else scale
        new_w = frame.size.width * scale
        new_h = frame.size.height * scale
        dx = (frame.size.width - new_w) / 2.0
        dy = (frame.size.height - new_h) / 2.0
        return NSMakeRect(
            frame.origin.x + dx,
            frame.origin.y + dy,
            new_w,
            new_h,
        )

    def _prewarm_main(self):
        if self._window is None:
            self._build_window()
            self._position_window()
            self._window.orderOut_(None)

    def _is_live(self):
        """Window is on screen and not fading out."""
        return (
            self._window is not None
            and self._window.isVisible()
            and self._hide_timer is None
        )

    def _apply_style_main(self, style, animated=False):
        self._style = style
        lay = layout_for(style)
        w = lay["width"]
        h = PILL_HEIGHT
        m = PILL_MARGIN
        self._pill.style = style
        self._pill.setNeedsDisplay_(True)
        self._position_window(w, animated)

        if lay["show_lock"]:
            self._lock.setFrame_(NSMakeRect(m + 2, (h - 18) / 2.0, 20, 18))
        self._lock.setHidden_(not lay["show_lock"])

        wave_x = m
        if lay["show_lock"]:
            wave_x = m + LOCK_SLOT
        if lay["label"] is not None:
            label_w = w * 0.58 if lay["show_wave"] else w - 2 * m - 12
            self._label.setStringValue_(lay["label"])
            self._label.setFrame_(NSMakeRect(m + 6, (h - 18) / 2.0, label_w, 18))
            if lay["show_wave"]:
                wave_x = m + w * 0.58 + 10
        self._label.setHidden_(lay["label"] is None)

        if lay["show_wave"]:
            self._wave.setFrame_(NSMakeRect(wave_x, m, w - m - wave_x, h - 2 * m))
        self._wave.setHidden_(not lay["show_wave"])

    def _cancel_auto_hide(self):
        if self._auto_hide_timer is not None:
            self._auto_hide_timer.invalidate()
            self._auto_hide_timer = None

    def _set_style_main(self, style):
        if self._window is not None and self._window.isVisible():
            self._apply_style_main(style, animated=self._is_live())

    def _flash_main(self, message, seconds):
        self._show_main("message")
        self._label.setStringValue_(message)
        if seconds is not None:
            self._auto_hide_timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    seconds, self._timer_target, b"autoHide:", None, False
                )
            )

    def _show_main(self, style="dictation"):
        self._prewarm_main()
        self._cancel_auto_hide()
        if self._is_live():
            # Already showing: restyle in place, no pop-in restart.
            self._apply_style_main(style, animated=True)
            return
        self._apply_style_main(style)
        if self._hide_timer is not None:
            self._hide_timer.invalidate()
            self._hide_timer = None
        self._phase = 0.0
        self._wave.setLevels_(build_symmetric_waveform(0.0, self._phase, BAR_COUNT))
        self._position_window()
        target_frame = self._window.frame()
        start_frame = self._scaled_frame(target_frame, SHOW_SCALE)
        self._window.setAlphaValue_(0.0)
        self._window.setFrame_display_(start_frame, False)
        self._window.orderFrontRegardless()

        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(SHOW_DURATION)
        self._window.animator().setAlphaValue_(1.0)
        self._window.animator().setFrame_display_(target_frame, False)
        NSAnimationContext.endGrouping()

        if self._timer_target is None:
            self._timer_target = _TimerTarget.alloc().initWithOverlay_(self)
        if self._timer is None:
            self._timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    1.0 / FPS, self._timer_target, b"tick:", None, True
                )
            )

    def _hide_main(self):
        if self._window is None:
            return
        self._stop_wave_timer()
        self._cancel_auto_hide()
        if self._hide_timer is not None:
            self._hide_timer.invalidate()
            self._hide_timer = None
        start_frame = self._window.frame()
        end_frame = self._scaled_frame(start_frame, HIDE_SCALE)

        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(HIDE_DURATION)
        self._window.animator().setAlphaValue_(0.0)
        self._window.animator().setFrame_display_(end_frame, False)
        NSAnimationContext.endGrouping()

        if self._timer_target is None:
            self._timer_target = _TimerTarget.alloc().initWithOverlay_(self)
        self._hide_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                HIDE_DURATION, self._timer_target, b"finishHide:", None, False
            )
        )

    def _finish_hide_main(self):
        if self._window is not None:
            self._window.orderOut_(None)
            self._position_window()
        self._stop_wave_timer()
        if self._hide_timer is not None:
            self._hide_timer.invalidate()
            self._hide_timer = None
        self._timer_target = None

    def _stop_wave_timer(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def _on_tick(self):
        if self._wave is None:
            return
        if self._style == "handsfree":
            self._pulse_phase += PULSE_SPEED
            self._pill.pulse = 0.5 + 0.5 * math.sin(self._pulse_phase)
            self._pill.setNeedsDisplay_(True)
        if self._style == "message":
            return
        try:
            level = _clamp01(float(self._level_source()))
        except Exception:
            level = 0.0
        self._phase += 0.32
        self._wave.setLevels_(build_symmetric_waveform(level, self._phase, BAR_COUNT))
        self._wave.setNeedsDisplay_(True)
