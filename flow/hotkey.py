"""Global hold-to-talk hotkey via pynput.

Requires the Input Monitoring permission for the host process
(Terminal during development, Python.app under the LaunchAgent).
"""

import logging
import queue
import threading
import time
from pynput import keyboard

log = logging.getLogger(__name__)

DEBOUNCE_S = 0.05  # 50 ms gate suppressing duplicate press transitions only.


class SerialRunner:
    """Runs submitted callables in order on one daemon thread.

    The Fn monitor's handler is delivered on the AppKit main thread, so a
    callback that blocks there blocks the whole UI. Worse than slow: starting
    and stopping a CoreAudio stream from the main thread deadlocks against
    CoreAudio's own IO thread (main holds the AudioUnit lock and waits for the
    HAL IO lock; the IO thread holds the HAL IO lock and waits for the
    AudioUnit lock). Handing the work to this thread keeps the main loop free.

    The pynput-backed `HotkeyListener` shares this same protection: its
    on_press/on_release callbacks are delivered on the pynput listener thread
    and would otherwise wedge keyboard event delivery the same way.
    """

    def __init__(self, name="hotkey-dispatch"):
        self._queue: queue.Queue = queue.Queue()
        self._name = name
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=self._name
        )
        self._thread.start()

    def __call__(self, func):
        self._queue.put(func)

    def stop(self):
        self._queue.put(None)
        self._thread = None

    def _loop(self):
        while True:
            func = self._queue.get()
            if func is None:
                return
            func()

KEY_MAP = {
    "alt_r": keyboard.Key.alt_r,
    "alt_l": keyboard.Key.alt_l,
    "cmd_r": keyboard.Key.cmd_r,
    "ctrl_r": keyboard.Key.ctrl_r,
    "f13": keyboard.Key.f13,
    "f14": keyboard.Key.f14,
    "f15": keyboard.Key.f15,
}


def _safe_call(callback, label):
    try:
        callback()
    except Exception:
        log.exception("%s failed", label)


class HotkeyListener:
    """Calls on_hold_start when the key goes down, on_hold_end when it comes up.

    on_press/on_release are delivered on the pynput listener thread, so they
    never call the pipeline directly — they only decide the press/release
    transition and submit the callback to `runner`, which is a background
    thread by default (see SerialRunner).
    """

    def __init__(self, key_name, on_hold_start, on_hold_end, runner=None):
        if key_name not in KEY_MAP:
            raise ValueError(f"Unknown hotkey {key_name!r}; options: {sorted(KEY_MAP)}")
        self._key = KEY_MAP[key_name]
        self._on_hold_start = on_hold_start
        self._on_hold_end = on_hold_end
        self._held = False
        self._listener = None
        self._own_runner = runner is None
        self._runner = runner if runner is not None else SerialRunner()

    def start(self):
        self.start_dispatcher()
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.start()
        log.info("Hotkey listener started (key=%s)", self._key)

    def start_dispatcher(self):
        if self._own_runner:
            self._runner.start()

    def stop(self):
        if self._listener:
            self._listener.stop()
            # pynput's stop() does not wait for the listener thread, so an
            # _on_press/_on_release may still be mid-flight. Join it before
            # stopping the runner: the None sentinel must not overtake an
            # in-flight callback submission (pynput's Listener is a Thread).
            try:
                if self._listener.is_alive():
                    self._listener.join(timeout=1.0)
            except Exception:
                log.exception("joining hotkey listener thread failed")
        if self._own_runner:
            self._runner.stop()

    def _on_press(self, key):
        if key == self._key and not self._held:
            self._held = True
            self._runner(lambda: _safe_call(self._on_hold_start, "on_hold_start"))

    def _on_release(self, key):
        if key == self._key and self._held:
            self._held = False
            self._runner(lambda: _safe_call(self._on_hold_end, "on_hold_end"))


class FnHotkeyListener:
    """Hold-to-talk on the physical Fn (🌐) key.

    pynput cannot see Fn — it is a hardware modifier that fires a flags-changed
    event (the secondary-Fn flag) rather than a normal key press. We watch those
    events through an AppKit global monitor, which runs on the existing AppKit
    main loop and needs the same Input Monitoring permission as the pynput path.

    The monitor is passive: it cannot swallow Fn's default behaviour (emoji
    picker / input-source switch). Users disable that in System Settings →
    Keyboard → "Press 🌐 key to → Do Nothing".

    The handler runs on the main thread, so it never calls the pipeline
    directly — it only decides the press/release transition and submits the
    callback to `runner`, which is a background thread by default.
    """

    def __init__(self, on_hold_start, on_hold_end, runner=None):
        self._on_hold_start = on_hold_start
        self._on_hold_end = on_hold_end
        self._held = False
        self._monitor = None
        self._last_event_time = 0.0
        self._own_runner = runner is None
        self._runner = runner if runner is not None else SerialRunner()

    def start(self):
        from AppKit import NSEvent, NSEventMaskFlagsChanged

        self.start_dispatcher()
        self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged, self._handle_event
        )
        log.info("Fn hotkey listener started")

    def start_dispatcher(self):
        if self._own_runner:
            self._runner.start()

    def stop(self):
        if self._monitor is not None:
            from AppKit import NSEvent

            NSEvent.removeMonitor_(self._monitor)
            self._monitor = None
        if self._own_runner:
            self._runner.stop()

    def _handle_event(self, event):
        from AppKit import NSEventModifierFlagFunction

        now = time.monotonic()
        fn_down = bool(event.modifierFlags() & NSEventModifierFlagFunction)
        if fn_down and now - self._last_event_time < DEBOUNCE_S:
            return
        self._last_event_time = now
        self._dispatch(fn_down)

    def _dispatch(self, fn_down):
        if fn_down and not self._held:
            self._held = True
            self._runner(lambda: _safe_call(self._on_hold_start, "on_hold_start"))
        elif not fn_down and self._held:
            self._held = False
            self._runner(lambda: _safe_call(self._on_hold_end, "on_hold_end"))


def make_listener(key_name, on_hold_start, on_hold_end):
    """Pick the right listener for the configured key.

    "fn" needs the AppKit flags-changed monitor; every other key goes through
    the pynput KEY_MAP listener.
    """
    if key_name == "fn":
        return FnHotkeyListener(on_hold_start, on_hold_end)
    return HotkeyListener(key_name, on_hold_start, on_hold_end)
