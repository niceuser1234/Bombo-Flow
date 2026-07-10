"""Insert text at the cursor of the frontmost app.

Default mode: save clipboard -> set text -> synthetic Cmd+V -> restore
clipboard. Requires the Accessibility permission for the host process.

Waits for all modififier keys to be released before posting Cmd+V — the
still-held hotkey (e.g. Right-Option) would otherwise merge into the
synthetic event and turn it into Option+Cmd+V.
"""

import logging
import time

import Quartz
from AppKit import NSPasteboard, NSPasteboardTypeString

log = logging.getLogger(__name__)


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


def _paste_text(text: str):
    pb = NSPasteboard.generalPasteboard()
    old = pb.stringForType_(NSPasteboardTypeString)

    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    our_change = pb.changeCount()

    time.sleep(0.05)  # let the pasteboard settle before the app reads it
    _post_cmd_v()

    # Restore the previous clipboard, unless something else wrote meanwhile.
    time.sleep(0.3)
    if old is not None and pb.changeCount() == our_change:
        pb.clearContents()
        pb.setString_forType_(old, NSPasteboardTypeString)


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
