"""Rewrite mode helpers: read the user's current selection.

The selection is read the way a user would copy it — Cmd+C — because the
Accessibility selected-text attribute is missing in browsers and Electron
apps. The user's clipboard is snapshotted first and put back afterwards.
"""

import logging
import time

from AppKit import NSPasteboard, NSPasteboardTypeString
from PyObjCTools import AppHelper

from .insert import (
    _on_main,
    _post_cmd_key,
    _restore_pasteboard,
    _snapshot_pasteboard,
    _wait_modifiers_released,
)

log = logging.getLogger(__name__)

_KEYCODE_C = 8
COPY_WAIT_S = 0.5  # how long the frontmost app gets to answer Cmd+C
_POLL_S = 0.02


def _pasteboard():
    return NSPasteboard.generalPasteboard()


def grab_selection() -> str | None:
    """The frontmost app's selected text, or None when nothing is selected
    (Cmd+C left the clipboard untouched) or the selection isn't text."""
    _wait_modifiers_released()

    def snapshot():
        pb = _pasteboard()
        return pb.changeCount(), _snapshot_pasteboard(pb)

    try:
        before, saved = _on_main(snapshot)
    except TimeoutError:
        log.warning("Rewrite: main thread unresponsive before Cmd+C — skipping")
        return None
    _post_cmd_key(_KEYCODE_C)

    def read_and_restore():
        pb = _pasteboard()
        text = pb.stringForType_(NSPasteboardTypeString)
        _restore_pasteboard(pb, saved)
        return str(text) if text else None

    try:
        deadline = time.monotonic() + COPY_WAIT_S
        changed = False
        while time.monotonic() < deadline:
            if _on_main(lambda: _pasteboard().changeCount()) != before:
                changed = True
                break
            time.sleep(_POLL_S)
        if not changed:
            # One last look: a slow app may have answered right at the deadline.
            changed = _on_main(lambda: _pasteboard().changeCount()) != before
        if not changed:
            log.info("Rewrite: Cmd+C changed nothing — no selection")
            return None
        return _on_main(read_and_restore)
    except TimeoutError:
        log.warning("Rewrite: main thread unresponsive after Cmd+C — repairing clipboard")
        _schedule_repair(before, saved)
        return None


def _schedule_repair(before: int, saved: list) -> None:
    """Put the user's clipboard back if Cmd+C landed after we gave up."""

    def repair():
        pb = _pasteboard()
        if pb.changeCount() != before:
            _restore_pasteboard(pb, saved)
            log.info("Clipboard restored after a timed-out selection grab")

    AppHelper.callAfter(repair)


def build_rewrite_user_message(text: str, instruction: str) -> str:
    return (
        f"<instruction>\n{instruction.strip()}\n</instruction>\n\n"
        f"<text>\n{text}\n</text>"
    )
