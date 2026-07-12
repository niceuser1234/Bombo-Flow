"""Wispr-Flow-style recording overlay: a floating dark "pill" at the bottom
of the screen showing an animated waveform driven by the live mic level.

AppKit must only be touched from the main thread, so show()/hide() marshal
onto it with AppHelper.callAfter. A ~30fps NSTimer on the main runloop reads
the level source and repaints while the pill is visible.

PyObjC does not retain Objective-C objects on our behalf, so the Overlay
instance keeps strong Python references to the window, views and timer.
"""

import logging

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSMakeRect,
    NSScreen,
    NSTimer,
    NSView,
    NSVisualEffectView,
    NSWindow,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSMakePoint, NSObject, NSPointInRect
from PyObjCTools import AppHelper

log = logging.getLogger(__name__)

# Pill geometry (points).
PILL_WIDTH = 220.0
PILL_HEIGHT = 48.0
PILL_MARGIN = 12.0          # inset of the waveform inside the pill
BOTTOM_OFFSET = 80.0        # gap above the screen's bottom edge
BAR_COUNT = 24
FPS = 30.0

# NSVisualEffectView constants (avoid importing symbol names that vary by SDK).
_MATERIAL_DARK = 2
_STATE_ACTIVE = 1
# NSWindowCollectionBehavior: canJoinAllSpaces | fullScreenAuxiliary.
_COLLECTION_BEHAVIOR = (1 << 0) | (1 << 8)
_WINDOW_LEVEL = 25


class _WaveformView(NSView):
    """Custom view drawing BAR_COUNT rounded vertical bars from a level list."""

    def initWithFrame_(self, frame):
        self = objc.super(_WaveformView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._levels = [0.0] * BAR_COUNT
        return self

    def pushLevel_(self, level):
        # New level at the END, drop the oldest -> scrolling waveform feel.
        self._levels.append(float(level))
        if len(self._levels) > BAR_COUNT:
            self._levels = self._levels[-BAR_COUNT:]

    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        if w <= 0 or h <= 0:
            return

        n = BAR_COUNT
        slot = w / n
        bar_w = min(4.0, slot * 0.6)
        gap = (slot - bar_w) / 2.0
        avail = h - 8.0        # vertical padding
        min_bar = 4.0

        for i, level in enumerate(self._levels):
            level = 0.0 if level < 0.0 else (1.0 if level > 1.0 else level)
            bar_h = min_bar + level * (avail - min_bar)
            x = i * slot + gap
            y = (h - bar_h) / 2.0
            alpha = 0.35 + 0.65 * level
            NSColor.colorWithCalibratedWhite_alpha_(1.0, alpha).set()
            r = NSMakeRect(x, y, bar_w, bar_h)
            radius = bar_w / 2.0
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                r, radius, radius
            )
            path.fill()


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


class Overlay:
    def __init__(self, level_source):
        # level_source: () -> float, called on the main thread.
        self._level_source = level_source
        self._window = None
        self._effect = None
        self._wave = None
        self._timer = None
        self._timer_target = None

    # -- public API (thread-safe) --

    def show(self):
        AppHelper.callAfter(self._show_main)

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
        window.setHasShadow_(True)
        window.setIgnoresMouseEvents_(True)
        window.setCollectionBehavior_(_COLLECTION_BEHAVIOR)

        effect = NSVisualEffectView.alloc().initWithFrame_(frame)
        effect.setMaterial_(_MATERIAL_DARK)
        effect.setState_(_STATE_ACTIVE)
        effect.setWantsLayer_(True)
        effect.layer().setCornerRadius_(24.0)
        effect.layer().setMasksToBounds_(True)
        window.setContentView_(effect)

        wave_frame = NSMakeRect(
            PILL_MARGIN,
            PILL_MARGIN,
            PILL_WIDTH - 2 * PILL_MARGIN,
            PILL_HEIGHT - 2 * PILL_MARGIN,
        )
        wave = _WaveformView.alloc().initWithFrame_(wave_frame)
        wave.setWantsLayer_(True)
        effect.addSubview_(wave)

        self._window = window
        self._effect = effect
        self._wave = wave

    def _position_window(self):
        mouse = NSEvent.mouseLocation()
        screen = None
        for scr in NSScreen.screens():
            if NSPointInRect(mouse, scr.frame()):
                screen = scr
                break
        if screen is None:
            screen = NSScreen.mainScreen()
        vf = screen.frame()
        x = vf.origin.x + (vf.size.width - PILL_WIDTH) / 2.0
        y = vf.origin.y + BOTTOM_OFFSET
        self._window.setFrameOrigin_(NSMakePoint(x, y))

    def _show_main(self):
        if self._window is None:
            self._build_window()
        # Reset bars so a new recording starts flat.
        self._wave._levels = [0.0] * BAR_COUNT
        self._position_window()
        self._window.orderFrontRegardless()

        if self._timer is None:
            self._timer_target = _TimerTarget.alloc().initWithOverlay_(self)
            self._timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    1.0 / FPS, self._timer_target, b"tick:", None, True
                )
            )

    def _hide_main(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
            self._timer_target = None
        if self._window is not None:
            self._window.orderOut_(None)

    def _on_tick(self):
        if self._wave is None:
            return
        try:
            level = float(self._level_source())
        except Exception:
            level = 0.0
        self._wave.pushLevel_(level)
        self._wave.setNeedsDisplay_(True)
