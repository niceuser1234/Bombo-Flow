from unittest import mock

from flow import transform


class _PB:
    def __init__(self, text_after_copy=None):
        self.count = 1
        self.text = "users clipboard"
        self.text_after_copy = text_after_copy
        self.restored = None

    def changeCount(self):
        return self.count

    def stringForType_(self, _t):
        return self.text

    def copy(self):
        if self.text_after_copy is not None:
            self.count += 1
            self.text = self.text_after_copy


def _patched(pb):
    return [
        mock.patch.object(transform, "_pasteboard", return_value=pb),
        mock.patch.object(transform, "_on_main", side_effect=lambda f, timeout=None: f()),
        mock.patch.object(transform, "_wait_modifiers_released"),
        mock.patch.object(transform, "_post_cmd_key", side_effect=lambda code: pb.copy()),
        mock.patch.object(transform, "_snapshot_pasteboard", return_value=["SNAP"]),
        mock.patch.object(transform, "_restore_pasteboard",
                          side_effect=lambda p, items: setattr(pb, "restored", items)),
    ]


def _run(pb, extra=()):
    patches = _patched(pb) + list(extra)
    for p in patches:
        p.start()
    try:
        return transform.grab_selection()
    finally:
        for p in patches:
            p.stop()


def test_grab_selection_returns_copied_text_and_restores_clipboard():
    pb = _PB(text_after_copy="selected words")
    assert _run(pb) == "selected words"
    assert pb.restored == ["SNAP"]


def test_grab_selection_restores_clipboard_when_nothing_selected():
    pb = _PB(text_after_copy=None)
    with mock.patch.object(transform, "COPY_WAIT_S", 0.05):
        assert _run(pb) is None
    # Nothing was written, so nothing needs restoring — and the user's data is intact.
    assert pb.text == "users clipboard"
    assert pb.restored is None


def test_rewrite_message_delimits_instruction_and_text():
    msg = transform.build_rewrite_user_message("Hallo Welt", "mach Stichpunkte")
    assert "<instruction>\nmach Stichpunkte\n</instruction>" in msg
    assert "<text>\nHallo Welt\n</text>" in msg


def test_grab_selection_late_write_is_still_read_and_restored():
    class Late(_PB):
        def changeCount(self):
            n = self.calls = getattr(self, "calls", 0) + 1
            if n > 1:
                self.text = "slow words"
                return 2
            return 1

    pb = Late()
    with mock.patch.object(transform, "COPY_WAIT_S", 0):
        assert _run(pb) == "slow words"
    assert pb.restored == ["SNAP"]


def test_grab_selection_non_text_selection_returns_none_and_restores():
    pb = _PB(text_after_copy="x")
    pb.stringForType_ = lambda _t: None
    assert _run(pb) is None
    assert pb.restored == ["SNAP"]


def test_grab_selection_timeout_after_copy_schedules_repair():
    pb = _PB(text_after_copy="selected")
    calls = {"n": 0}

    def on_main(f, timeout=None):
        calls["n"] += 1
        if calls["n"] > 1:  # first call is the snapshot
            raise TimeoutError("busy")
        return f()

    scheduled = []
    extra = [
        mock.patch.object(transform, "_on_main", side_effect=on_main),
        mock.patch.object(transform.AppHelper, "callAfter",
                          side_effect=lambda f: scheduled.append(f)),
    ]
    assert _run(pb, extra) is None  # later patches override _patched's _on_main
    assert len(scheduled) == 1
    patches = _patched(pb)
    for p in patches:
        p.start()
    try:
        scheduled[0]()
    finally:
        for p in patches:
            p.stop()
    assert pb.restored == ["SNAP"]


def test_grab_selection_timeout_on_initial_snapshot_returns_none():
    pb = _PB(text_after_copy="selected")
    extra = [mock.patch.object(transform, "_on_main", side_effect=TimeoutError("busy"))]
    assert _run(pb, extra) is None
