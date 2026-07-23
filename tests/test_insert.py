"""Tests for marshalling NSPasteboard access onto the main thread.

_paste_text runs on the pipeline worker thread, but AppKit's pasteboard is
only supported from the main thread. These tests stub NSThread.isMainThread
to simulate running on a worker thread, patch PyObjCTools.AppHelper.callAfter
to record calls (running them either inline, deferred, or not at all, depending
on the scenario), and stub the pasteboard to observe ordering/behavior without
touching the real system clipboard.

The fake pasteboard stores real NSData per type so the snapshot/restore path
exercises the same NSPasteboardItem plumbing the app uses in production.
"""

import threading
import time
from unittest.mock import patch

from Foundation import NSData

import flow.insert as insert

TEXT_TYPE = str(insert.NSPasteboardTypeString)
TIFF_TYPE = "public.tiff"


def _data(payload: bytes) -> NSData:
    return NSData.dataWithBytes_length_(payload, len(payload))


def _bytes(data) -> bytes:
    return bytes(data)


class _FakeNSThread:
    """Stand-in for AppKit's NSThread — patched in wholesale so we never
    touch attributes on the real ObjC class (which doesn't support the
    delattr teardown unittest.mock does when unpatching)."""

    @staticmethod
    def isMainThread():
        return False


class _FakeItem:
    """Minimal NSPasteboardItem stand-in for what the pasteboard hands out."""

    def __init__(self, mapping):
        self._mapping = dict(mapping)

    def types(self):
        return list(self._mapping)

    def dataForType_(self, type_name):
        return self._mapping.get(str(type_name))


class _FakePasteboard:
    def __init__(self, initial=None, items=None):
        if items is not None:
            self._items = [dict(i) for i in items]
        elif initial is not None:
            self._items = [{TEXT_TYPE: _data(initial.encode())}]
        else:
            self._items = []
        self._change_count = 0
        self.calls = []

    # -- API used by flow.insert --

    def pasteboardItems(self):
        self.calls.append("read")
        return [_FakeItem(i) for i in self._items]

    def clearContents(self):
        self.calls.append("clear")
        self._items = []

    def setString_forType_(self, s, type_name):
        self.calls.append(("set", s))
        self._items = [{str(type_name): _data(s.encode())}]
        self._change_count += 1

    def writeObjects_(self, objects):
        self.calls.append("writeObjects")
        self._items = [
            {str(t): obj.dataForType_(t) for t in obj.types()} for obj in objects
        ]
        self._change_count += 1

    def changeCount(self):
        return self._change_count

    # -- assertions helpers --

    @property
    def text(self):
        for item in self._items:
            if TEXT_TYPE in item:
                return _bytes(item[TEXT_TYPE]).decode()
        return None

    def data_for(self, type_name):
        for item in self._items:
            if type_name in item:
                return _bytes(item[type_name])
        return None


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
    assert pb.text == "old clipboard"  # restored, unchanged in the meantime


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
    assert pb.text == "someone else's copy"


def test_non_text_clipboard_survives_the_paste():
    """A pasteboard holding an image has no string representation. The old
    code read None, cleared it anyway, and then skipped the restore — the
    image was destroyed and the dictation text left behind."""
    order = []
    image = b"\x4d\x4d\x00\x2a fake tiff bytes"
    pb = _FakePasteboard(items=[{TIFF_TYPE: _data(image)}])

    with (
        patch.object(insert, "NSPasteboard") as mock_pb_cls,
        patch.object(insert, "NSThread", _FakeNSThread),
        patch.object(
            insert.AppHelper, "callAfter", side_effect=_run_callafter_inline(order)
        ),
        patch.object(insert, "_post_cmd_v", lambda: None),
        patch.object(time, "sleep", lambda _s: None),
    ):
        mock_pb_cls.generalPasteboard.return_value = pb
        insert._paste_text("dictated text")

    assert pb.data_for(TIFF_TYPE) == image
    assert pb.text is None


def test_empty_clipboard_is_not_left_holding_the_dictation():
    order = []
    pb = _FakePasteboard()  # nothing on the clipboard at all

    with (
        patch.object(insert, "NSPasteboard") as mock_pb_cls,
        patch.object(insert, "NSThread", _FakeNSThread),
        patch.object(
            insert.AppHelper, "callAfter", side_effect=_run_callafter_inline(order)
        ),
        patch.object(insert, "_post_cmd_v", lambda: None),
        patch.object(time, "sleep", lambda _s: None),
    ):
        mock_pb_cls.generalPasteboard.return_value = pb
        insert._paste_text("dictated text")

    assert pb.text is None


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


def test_timed_out_block_does_not_clobber_the_clipboard_later():
    """The queued block is not cancellable at the AppHelper level, so it must
    no-op itself once the caller has given up. Otherwise it fires whenever the
    main thread frees up and replaces the clipboard with text nobody pasted."""
    pb = _FakePasteboard(initial="user clipboard")
    queued = []

    with (
        patch.object(insert, "NSPasteboard") as mock_pb_cls,
        patch.object(insert, "NSThread", _FakeNSThread),
        patch.object(insert.AppHelper, "callAfter", side_effect=queued.append),
        patch.object(insert, "_post_cmd_v", lambda: None),
        patch.object(insert, "_ON_MAIN_TIMEOUT_S", 0.05),
        patch.object(time, "sleep", lambda _s: None),
    ):
        mock_pb_cls.generalPasteboard.return_value = pb
        insert._paste_text("dictated text")

        # The main thread unwedges and drains its queue in FIFO order.
        for block in queued:
            block()

    assert pb.text == "user clipboard"


def test_repair_restores_the_clipboard_when_staging_ran_late():
    """If the staging block wins the race and runs anyway, the repair queued
    behind it puts the original clipboard back."""
    order = []
    pb = _FakePasteboard(initial="user clipboard")
    saved = {"items": [{TEXT_TYPE: _data(b"user clipboard")}]}
    pb.setString_forType_("dictated text", insert.NSPasteboardTypeString)

    with (
        patch.object(insert, "NSPasteboard") as mock_pb_cls,
        patch.object(
            insert.AppHelper, "callAfter", side_effect=_run_callafter_inline(order)
        ),
    ):
        mock_pb_cls.generalPasteboard.return_value = pb
        insert._schedule_clipboard_repair(saved)

    assert pb.text == "user clipboard"


def test_repair_is_a_noop_when_staging_never_ran():
    order = []
    pb = _FakePasteboard(initial="user clipboard")

    with (
        patch.object(insert, "NSPasteboard") as mock_pb_cls,
        patch.object(
            insert.AppHelper, "callAfter", side_effect=_run_callafter_inline(order)
        ),
    ):
        mock_pb_cls.generalPasteboard.return_value = pb
        insert._schedule_clipboard_repair({})

    assert pb.text == "user clipboard"
    assert "clear" not in pb.calls


def test_on_main_reraises_the_original_exception():
    """Previously the empty result list surfaced as IndexError, hiding the
    real failure and losing the dictation behind a bogus traceback."""
    order = []

    def boom():
        raise ValueError("pasteboard exploded")

    with (
        patch.object(insert, "NSThread", _FakeNSThread),
        patch.object(
            insert.AppHelper, "callAfter", side_effect=_run_callafter_inline(order)
        ),
    ):
        try:
            insert._on_main(boom)
        except ValueError as exc:
            assert str(exc) == "pasteboard exploded"
        except IndexError:  # pragma: no cover - regression guard
            raise AssertionError("IndexError masked the real exception")
        else:  # pragma: no cover - regression guard
            raise AssertionError("exception did not propagate")


def test_paste_survives_a_failing_pasteboard():
    """A raising save_and_set must not take the whole dictation down."""
    order = []

    class _ExplodingPasteboard(_FakePasteboard):
        def clearContents(self):
            raise RuntimeError("no pasteboard today")

    pb = _ExplodingPasteboard(initial="old clipboard")

    with (
        patch.object(insert, "NSPasteboard") as mock_pb_cls,
        patch.object(insert, "NSThread", _FakeNSThread),
        patch.object(
            insert.AppHelper, "callAfter", side_effect=_run_callafter_inline(order)
        ),
        patch.object(insert, "_post_cmd_v", lambda: None),
        patch.object(time, "sleep", lambda _s: None),
    ):
        mock_pb_cls.generalPasteboard.return_value = pb
        insert._paste_text("hello")  # must not raise


def test_utf16_chunks_never_split_a_surrogate_pair():
    text = "a" + "😀" * 12
    chunks = list(insert._utf16_chunks(text, 5))
    assert "".join(chunks) == text
    for chunk in chunks:
        assert insert._utf16_len(chunk) <= 5


def test_utf16_len_counts_code_units_not_code_points():
    assert insert._utf16_len("abc") == 3
    assert insert._utf16_len("😀") == 2
    assert insert._utf16_len("ä😀") == 3
