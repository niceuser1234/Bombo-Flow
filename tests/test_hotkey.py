import unittest

from flow.hotkey import FnHotkeyListener, HotkeyListener, make_listener


class MakeListenerTests(unittest.TestCase):
    def _noop(self):
        pass

    def test_fn_returns_fn_listener(self):
        listener = make_listener("fn", self._noop, self._noop)
        self.assertIsInstance(listener, FnHotkeyListener)

    def test_known_key_returns_pynput_listener(self):
        listener = make_listener("alt_r", self._noop, self._noop)
        self.assertIsInstance(listener, HotkeyListener)

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            make_listener("does_not_exist", self._noop, self._noop)


class _Recorder:
    def __init__(self):
        self.starts = 0
        self.ends = 0

    def start(self):
        self.starts += 1

    def end(self):
        self.ends += 1


class FnDispatchTests(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()
        self.listener = FnHotkeyListener(self.rec.start, self.rec.end)

    def test_down_then_up_fires_once_each(self):
        self.listener._dispatch(fn_down=True)
        self.listener._dispatch(fn_down=False)
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.ends, 1)

    def test_repeated_down_does_not_refire(self):
        self.listener._dispatch(fn_down=True)
        self.listener._dispatch(fn_down=True)
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.ends, 0)

    def test_up_without_prior_down_is_ignored(self):
        self.listener._dispatch(fn_down=False)
        self.assertEqual(self.rec.starts, 0)
        self.assertEqual(self.rec.ends, 0)

    def test_callback_exception_does_not_propagate(self):
        def boom():
            raise RuntimeError("nope")

        listener = FnHotkeyListener(boom, boom)
        # Must not raise — exceptions are logged, and the held-state still flips
        # so the matching release can fire.
        listener._dispatch(fn_down=True)
        listener._dispatch(fn_down=False)

    def test_handle_event_reads_function_flag(self):
        from AppKit import NSEventModifierFlagFunction

        class FakeEvent:
            def __init__(self, flags):
                self._flags = flags

            def modifierFlags(self):
                return self._flags

        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        self.listener._handle_event(FakeEvent(0))
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.ends, 1)


if __name__ == "__main__":
    unittest.main()
