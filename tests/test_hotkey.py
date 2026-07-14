import threading
import unittest

from flow.hotkey import DEBOUNCE_S, FnHotkeyListener, HotkeyListener, make_listener


def _direct(func):
    """Runner that executes inline, so transition logic can be asserted synchronously."""
    func()


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
        self.listener = FnHotkeyListener(self.rec.start, self.rec.end, runner=_direct)

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

        listener = FnHotkeyListener(boom, boom, runner=_direct)
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
        # Skip past the debounce window so the release is accepted.
        self.listener._last_event_time = 0.0
        self.listener._handle_event(FakeEvent(0))
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.ends, 1)

    def test_debounce_ignores_rapid_fire_events(self):
        from AppKit import NSEventModifierFlagFunction

        class FakeEvent:
            def __init__(self, flags):
                self._flags = flags

            def modifierFlags(self):
                return self._flags

        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        # Release within the debounce window — ignored.
        self.listener._handle_event(FakeEvent(0))
        # Another press within debounce — also ignored.
        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        # Only the first press took effect; release and second press were
        # swallowed by the debounce gate.
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.ends, 0)

    def test_debounce_allows_event_after_timeout(self):
        from AppKit import NSEventModifierFlagFunction

        class FakeEvent:
            def __init__(self, flags):
                self._flags = flags

            def modifierFlags(self):
                return self._flags

        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        # Simulate that debounce window has passed.
        self.listener._last_event_time = 0.0
        self.listener._handle_event(FakeEvent(0))
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.ends, 1)
        # A second start after release is allowed if debounce expired.
        self.listener._last_event_time = 0.0
        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        self.assertEqual(self.rec.starts, 2)


class FnRunsOffMainThreadTests(unittest.TestCase):
    """The Fn monitor handler is delivered on the AppKit main thread.

    Calling CoreAudio from there deadlocks against CoreAudio's IO thread and
    freezes the entire app, so the default runner must move the callback off
    the thread that delivered the event.
    """

    def test_callback_runs_on_another_thread(self):
        ran_on = []
        done = threading.Event()

        def on_start():
            ran_on.append(threading.current_thread().ident)
            done.set()

        listener = FnHotkeyListener(on_start, lambda: None)
        listener.start_dispatcher()
        try:
            listener._dispatch(fn_down=True)
            self.assertTrue(done.wait(2.0), "callback never ran")
        finally:
            listener.stop()

        self.assertNotEqual(ran_on[0], threading.current_thread().ident)

    def test_handler_returns_while_callback_is_still_blocked(self):
        entered = threading.Event()
        release = threading.Event()

        def stuck_start():
            entered.set()
            release.wait(5.0)  # stands in for a wedged CoreAudio start

        listener = FnHotkeyListener(stuck_start, lambda: None)
        listener.start_dispatcher()
        try:
            listener._dispatch(fn_down=True)  # must not block
            self.assertTrue(entered.wait(2.0))
            self.assertFalse(release.is_set())
        finally:
            release.set()
            listener.stop()

    def test_press_and_release_stay_ordered(self):
        seen = []
        done = threading.Event()

        def on_end():
            seen.append("end")
            done.set()

        listener = FnHotkeyListener(lambda: seen.append("start"), on_end)
        listener.start_dispatcher()
        try:
            listener._dispatch(fn_down=True)
            listener._dispatch(fn_down=False)
            self.assertTrue(done.wait(2.0))
        finally:
            listener.stop()

        self.assertEqual(seen, ["start", "end"])

    def test_callback_exception_does_not_kill_the_dispatch_thread(self):
        done = threading.Event()

        def boom():
            raise RuntimeError("nope")

        listener = FnHotkeyListener(boom, done.set)
        listener.start_dispatcher()
        try:
            listener._dispatch(fn_down=True)   # raises inside the runner
            listener._dispatch(fn_down=False)  # must still be delivered
            self.assertTrue(done.wait(2.0), "dispatch thread died on exception")
        finally:
            listener.stop()


if __name__ == "__main__":
    unittest.main()
