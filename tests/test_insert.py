"""Tests for marshalling NSPasteboard access onto the main thread.

_paste_text runs on the pipeline worker thread, but AppKit's pasteboard is
only supported from the main thread. These tests stub NSThread.isMainThread
to simulate running on a worker thread, patch PyObjCTools.AppHelper.callAfter
to record calls (running them either inline or not at all, depending on the
scenario), and stub the pasteboard to observe ordering/behavior without
touching the real system clipboard.
"""

import threading
import time
from unittest.mock import patch

import flow.insert as insert


class _FakeNSThread:
    """Stand-in for AppKit's NSThread — patched in wholesale so we never
    touch attributes on the real ObjC class (which doesn't support the
    delattr teardown unittest.mock does when unpatching)."""

    @staticmethod
    def isMainThread():
        return False


class _FakePasteboard:
    def __init__(self, initial=None):
        self._content = initial
        self._change_count = 0
        self.calls = []

    def stringForType_(self, _type):
        self.calls.append("read")
        return self._content

    def clearContents(self):
        self.calls.append("clear")

    def setString_forType_(self, s, _type):
        self.calls.append(("set", s))
        self._content = s
        self._change_count += 1

    def changeCount(self):
        return self._change_count


def _run_callafter_inline(order):
    """Return a fake callAfter that records call order and runs immediately."""

    def fake_call_after(func, *args, **kwargs):
        order.append("callAfter")
        func(*args, **kwargs)

    return fake_call_after


def test_save_and_set_happens_before_post_cmd_v():
    order = []
    pb = _FakePasteboard(initial="old clipboard")

    def fake_post_cmd_v():
        order.append("post_cmd_v")

    with (
        patch.object(insert, "NSPasteboard") as mock_pb_cls,
        patch.object(insert, "NSThread", _FakeNSThread),
        patch.object(
            insert.AppHelper, "callAfter", side_effect=_run_callafter_inline(order)
        ),
        patch.object(insert, "_post_cmd_v", side_effect=fake_post_cmd_v),
        patch.object(time, "sleep", lambda _s: None),
    ):
        mock_pb_cls.generalPasteboard.return_value = pb
        insert._paste_text("hello")

    assert order == ["callAfter", "post_cmd_v", "callAfter"]
    assert pb._content == "old clipboard"  # restored, unchanged in the meantime


def test_restore_skipped_when_change_count_advanced():
    order = []
    pb = _FakePasteboard(initial="old clipboard")

    def fake_post_cmd_v():
        # Simulate some other app writing to the pasteboard meanwhile.
        pb.setString_forType_("someone else's copy", insert.NSPasteboardTypeString)

    with (
        patch.object(insert, "NSPasteboard") as mock_pb_cls,
        patch.object(insert, "NSThread", _FakeNSThread),
        patch.object(
            insert.AppHelper, "callAfter", side_effect=_run_callafter_inline(order)
        ),
        patch.object(insert, "_post_cmd_v", side_effect=fake_post_cmd_v),
        patch.object(time, "sleep", lambda _s: None),
    ):
        mock_pb_cls.generalPasteboard.return_value = pb
        insert._paste_text("hello")

    # Not restored — the intervening write must win.
    assert pb._content == "someone else's copy"


def test_timeout_skips_post_cmd_v():
    pb = _FakePasteboard(initial="old clipboard")
    post_cmd_v_called = threading.Event()

    def fake_post_cmd_v():
        post_cmd_v_called.set()

    def hanging_call_after(func, *args, **kwargs):
        # Simulate a wedged main thread: never actually invoke func.
        pass

    with (
        patch.object(insert, "NSPasteboard") as mock_pb_cls,
        patch.object(insert, "NSThread", _FakeNSThread),
        patch.object(insert.AppHelper, "callAfter", side_effect=hanging_call_after),
        patch.object(insert, "_post_cmd_v", side_effect=fake_post_cmd_v),
        patch.object(insert, "_ON_MAIN_TIMEOUT_S", 0.05),
    ):
        mock_pb_cls.generalPasteboard.return_value = pb
        insert._paste_text("hello")

    assert not post_cmd_v_called.is_set()
