"""Insert text at the cursor of the frontmost app.

Default mode: save clipboard -> set text -> synthetic Cmd+V -> restore
clipboard. Requires the Accessibility permission for the host process.

Waits for all modififier keys to be released before posting Cmd+V — the
still-held hotkey (e.g. Right-Option) would otherwise merge into the
synthetic event and turn it into Option+Cmd+V.
"""

import logging
import threading
import time

import Quartz
from AppKit import NSPasteboard, NSPasteboardTypeString, NSThread
from PyObjCTools import AppHelper

log = logging.getLogger(__name__)

_ON_MAIN_TIMEOUT_S = 1.0


def accessibility_trusted(prompt: bool = False) -> bool:
    """Whether this process may inject events into other apps.

    Without the Accessibility grant, the synthetic Cmd+V is confined to the
    host app (e.g. the terminal), so dictation only ever pastes there. With
    prompt=True, macOS shows its permission dialog and deep-links to the pane.
    """
    from ApplicationServices import (
        AXIsProcessTrusted,
        AXIsProcessTrustedWithOptions,
        kAXTrustedCheckOptionPrompt,
    )

    if prompt:
        return bool(
            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        )
    return bool(AXIsProcessTrusted())

_MODIFIER_MASK = (
    Quartz.kCGEventFlagMaskCommand
    | Quartz.kCGEventFlagMaskAlternate
    | Quartz.kCGEventFlagMaskControl
    | Quartz.kCGEventFlagMaskShift
)
_KEYCODE_V = 9


def insert_text(text: str, mode: str = "paste"):
    if not text:
        return
    _wait_modifiers_released()
    if mode == "type":
        _type_text(text)
    else:
        _paste_text(text)


def _wait_modifiers_released(timeout: float = 1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        flags = Quartz.CGEventSourceFlagsState(
            Quartz.kCGEventSourceStateHIDSystemState
        )
        if not (flags & _MODIFIER_MASK):
            return
        time.sleep(0.02)
    log.warning("Modifier keys still held after %.1fs — pasting anyway", timeout)


def _on_main(func, timeout=None):
    """Run func() on the main thread and return its result.

    AppKit's NSPasteboard is only supported from the main thread, but
    _paste_text runs on the pipeline worker thread. When already on the
    main thread, run inline; otherwise marshal the call over via
    PyObjCTools.AppHelper.callAfter and block (with a timeout) on a
    threading.Event for the result.
    """
    if NSThread.isMainThread():
        return func()

    if timeout is None:
        timeout = _ON_MAIN_TIMEOUT_S

    result = []
    done = threading.Event()

    def run():
        try:
            result.append(func())
        finally:
            done.set()

    AppHelper.callAfter(run)
    if not done.wait(timeout):
        raise TimeoutError(f"main thread did not respond within {timeout:.1f}s")
    return result[0]


def _paste_text(text: str):
    def save_and_set():
        pb = NSPasteboard.generalPasteboard()
        old = pb.stringForType_(NSPasteboardTypeString)
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        return old, pb.changeCount()

    try:
        old, our_change = _on_main(save_and_set)
    except TimeoutError:
        # A wedged main thread means we can't safely save/restore the
        # clipboard — pasting a stale one is worse than not pasting.
        log.warning("Main thread unresponsive — skipping paste")
        return

    time.sleep(0.05)  # let the pasteboard settle before the app reads it
    _post_cmd_v()

    # Restore the previous clipboard, unless something else wrote meanwhile.
    time.sleep(0.3)

    def restore():
        pb = NSPasteboard.generalPasteboard()
        if old is not None and pb.changeCount() == our_change:
            pb.clearContents()
            pb.setString_forType_(old, NSPasteboardTypeString)

    try:
        _on_main(restore)
    except TimeoutError:
        log.warning("Main thread unresponsive — could not restore previous clipboard")


def _post_cmd_v():
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(src, _KEYCODE_V, down)
        Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.01)


def _type_text(text: str, chunk_size: int = 20):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for i in range(0, len(text), chunk_size):
        chunk = text[i : i + chunk_size]
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(src, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.01)
