"""Rewrite mode helpers: read the user's current selection.

The selection is read the way a user would copy it — Cmd+C — because the
Accessibility selected-text attribute is missing in browsers and Electron
apps. The user's clipboard is snapshotted first and put back afterwards.
"""

import logging
import time

from AppKit import NSPasteboard, NSPasteboardTypeString

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

    before, saved = _on_main(snapshot)
    _post_cmd_key(_KEYCODE_C)

    deadline = time.monotonic() + COPY_WAIT_S
    while time.monotonic() < deadline:
        if _on_main(lambda: _pasteboard().changeCount()) != before:
            break
        time.sleep(_POLL_S)
    else:
        log.info("Rewrite: Cmd+C changed nothing — no selection")
        return None

    def read_and_restore():
        pb = _pasteboard()
        text = pb.stringForType_(NSPasteboardTypeString)
        _restore_pasteboard(pb, saved)
        return str(text) if text else None

    return _on_main(read_and_restore)


def build_rewrite_user_message(text: str, instruction: str) -> str:
    return (
        f"<instruction>\n{instruction.strip()}\n</instruction>\n\n"
        f"<text>\n{text}\n</text>"
    )
