"""Global hold-to-talk hotkey via pynput.

Requires the Input Monitoring permission for the host process
(Terminal during development, Python.app under the LaunchAgent).
"""

import logging
from pynput import keyboard

log = logging.getLogger(__name__)

KEY_MAP = {
    "alt_r": keyboard.Key.alt_r,
    "alt_l": keyboard.Key.alt_l,
    "cmd_r": keyboard.Key.cmd_r,
    "ctrl_r": keyboard.Key.ctrl_r,
    "f13": keyboard.Key.f13,
    "f14": keyboard.Key.f14,
    "f15": keyboard.Key.f15,
}


class HotkeyListener:
    """Calls on_hold_start when the key goes down, on_hold_end when it comes up."""

    def __init__(self, key_name, on_hold_start, on_hold_end):
        if key_name not in KEY_MAP:
            raise ValueError(f"Unknown hotkey {key_name!r}; options: {sorted(KEY_MAP)}")
        self._key = KEY_MAP[key_name]
        self._on_hold_start = on_hold_start
        self._on_hold_end = on_hold_end
        self._held = False
        self._listener = None

    def start(self):
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.start()
        log.info("Hotkey listener started (key=%s)", self._key)

    def stop(self):
        if self._listener:
            self._listener.stop()

    def _on_press(self, key):
        if key == self._key and not self._held:
            self._held = True
            try:
                self._on_hold_start()
            except Exception:
                log.exception("on_hold_start failed")

    def _on_release(self, key):
        if key == self._key and self._held:
            self._held = False
            try:
                self._on_hold_end()
            except Exception:
                log.exception("on_hold_end failed")


class FnHotkeyListener:
    """Hold-to-talk on the physical Fn (🌐) key.

    pynput cannot see Fn — it is a hardware modifier that fires a flags-changed
    event (the secondary-Fn flag) rather than a normal key press. We watch those
    events through an AppKit global monitor, which runs on the existing AppKit
    main loop and needs the same Input Monitoring permission as the pynput path.

    The monitor is passive: it cannot swallow Fn's default behaviour (emoji
    picker / input-source switch). Users disable that in System Settings →
    Keyboard → "Press 🌐 key to → Do Nothing".
    """

    def __init__(self, on_hold_start, on_hold_end):
        self._on_hold_start = on_hold_start
        self._on_hold_end = on_hold_end
        self._held = False
        self._monitor = None

    def start(self):
        from AppKit import NSEvent, NSEventMaskFlagsChanged

        self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged, self._handle_event
        )
        log.info("Fn hotkey listener started")

    def stop(self):
        if self._monitor is not None:
            from AppKit import NSEvent

            NSEvent.removeMonitor_(self._monitor)
            self._monitor = None

    def _handle_event(self, event):
        from AppKit import NSEventModifierFlagFunction

        fn_down = bool(event.modifierFlags() & NSEventModifierFlagFunction)
        self._dispatch(fn_down)

    def _dispatch(self, fn_down):
        if fn_down and not self._held:
            self._held = True
            try:
                self._on_hold_start()
            except Exception:
                log.exception("on_hold_start failed")
        elif not fn_down and self._held:
            self._held = False
            try:
                self._on_hold_end()
            except Exception:
                log.exception("on_hold_end failed")


def make_listener(key_name, on_hold_start, on_hold_end):
    """Pick the right listener for the configured key.

    "fn" needs the AppKit flags-changed monitor; every other key goes through
    the pynput KEY_MAP listener.
    """
    if key_name == "fn":
        return FnHotkeyListener(on_hold_start, on_hold_end)
    return HotkeyListener(key_name, on_hold_start, on_hold_end)
