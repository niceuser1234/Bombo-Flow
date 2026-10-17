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

    Two things the naive version gets wrong, both of which corrupt the user's
    clipboard: a timed-out block stays queued and runs whenever the main
    thread frees up (hence the cancel flag, checked on the main thread just
    before the work), and an exception inside func() would leave `result`
    empty so the caller saw an unrelated IndexError instead of the real
    failure (hence the explicit re-raise).
    """
    if NSThread.isMainThread():
        return func()

    if timeout is None:
        timeout = _ON_MAIN_TIMEOUT_S

    result = []
    failure = []
    cancelled = threading.Event()
    done = threading.Event()

    def run():
        if cancelled.is_set():
            return
        try:
            result.append(func())
        except Exception as exc:  # noqa: BLE001 — re-raised on the caller's thread
            log.exception(
                "Main-thread call %s failed", getattr(func, "__name__", func)
            )
            failure.append(exc)
        finally:
            done.set()

    AppHelper.callAfter(run)
    if not done.wait(timeout):
        cancelled.set()
        raise TimeoutError(f"main thread did not respond within {timeout:.1f}s")
    if failure:
        raise failure[0]
    return result[0]


def _snapshot_pasteboard(pb) -> list[dict]:
    """Every item on the pasteboard, as {type: data} dicts.

    Not just the string type: a pasteboard holding an image, a file or PDF
    data returns None for NSPasteboardTypeString, and clearContents() below
    destroys that content. Snapshotting all representations is what makes the
    paste round-trip non-destructive.
    """
    items = []
    for item in pb.pasteboardItems() or []:
        entry = {}
        for type_name in item.types() or []:
            data = item.dataForType_(type_name)
            if data is not None:
                entry[str(type_name)] = data
        if entry:
            items.append(entry)
    return items


def _restore_pasteboard(pb, items: list[dict]) -> None:
    """Put a _snapshot_pasteboard() result back. An empty snapshot clears the
    pasteboard rather than leaving our dictation text behind."""
    from AppKit import NSPasteboardItem

    pb.clearContents()
    if not items:
        return
    restored = []
    for entry in items:
        item = NSPasteboardItem.alloc().init()
        for type_name, data in entry.items():
            item.setData_forType_(data, type_name)
        restored.append(item)
    pb.writeObjects_(restored)


def _paste_text(text: str):
    # Filled by save_and_set once it actually runs on the main thread, so the
    # timeout path below can tell "never ran" from "ran too late".
    saved: dict = {}

    def save_and_set():
        pb = NSPasteboard.generalPasteboard()
        saved["items"] = _snapshot_pasteboard(pb)
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        return pb.changeCount()

    try:
        our_change = _on_main(save_and_set)
    except TimeoutError:
        # save_and_set may still be queued and fire later. callAfter is FIFO,
        # so an undo queued now lands *behind* it: if it runs, this repairs
        # the clipboard; if the cancel won, `saved` is empty and this is a
        # no-op. Pasting a stale clipboard is worse than not pasting.
        log.warning("Main thread unresponsive — skipping paste")
        _schedule_clipboard_repair(saved)
        return
    except Exception:
        log.exception("Could not stage the clipboard — skipping paste")
        return

    time.sleep(0.05)  # let the pasteboard settle before the app reads it
    _post_cmd_v()

    # Restore the previous clipboard, unless something else wrote meanwhile.
    time.sleep(0.3)

    def restore():
        pb = NSPasteboard.generalPasteboard()
        if pb.changeCount() == our_change:
            _restore_pasteboard(pb, saved.get("items") or [])

    try:
        _on_main(restore)
    except TimeoutError:
        log.warning("Main thread unresponsive — could not restore previous clipboard")
    except Exception:
        log.exception("Restoring the previous clipboard failed")


def _schedule_clipboard_repair(saved: dict) -> None:
    """Undo a paste-staging that timed out but may still land on the main
    thread. Runs after the staging block thanks to callAfter's FIFO order."""

    def repair():
        items = saved.get("items")
        if items is None:
            return  # staging was cancelled in time — nothing to undo
        _restore_pasteboard(NSPasteboard.generalPasteboard(), items)
        log.info("Clipboard restored after a timed-out paste")

    AppHelper.callAfter(repair)


def _post_cmd_key(keycode: int):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(src, keycode, down)
        Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.01)


def _post_cmd_v():
    _post_cmd_key(_KEYCODE_V)


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units — what CGEventKeyboardSetUnicodeString
    counts. Python's len() counts code points, so emoji and other non-BMP
    characters would be under-reported and the tail of the chunk dropped."""
    return len(text.encode("utf-16-le")) // 2


def _utf16_chunks(text: str, limit: int):
    """Split into chunks of at most `limit` UTF-16 units, never mid-character
    (a surrogate pair split across two events types as garbage)."""
    chunk: list[str] = []
    size = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if chunk and size + width > limit:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(ch)
        size += width
    if chunk:
        yield "".join(chunk)


def _type_text(text: str, chunk_size: int = 20):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for chunk in _utf16_chunks(text, chunk_size):
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(src, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, _utf16_len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.01)
