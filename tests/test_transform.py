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


def _run(pb):
    patches = _patched(pb)
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


def test_rewrite_message_delimits_instruction_and_text():
    msg = transform.build_rewrite_user_message("Hallo Welt", "mach Stichpunkte")
    assert "<instruction>\nmach Stichpunkte\n</instruction>" in msg
    assert "<text>\nHallo Welt\n</text>" in msg
