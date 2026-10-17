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
        self.modes = []
        self.mode_changes = []

    def start(self, mode="dictation"):
        self.starts += 1
        self.modes.append(mode)

    def end(self):
        self.ends += 1

    def change(self, mode):
        self.mode_changes.append(mode)


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
        def boom(*_):
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
        # Releases are never debounced, so this is delivered immediately.
        self.listener._handle_event(FakeEvent(0))
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.ends, 1)

    def test_debounce_ignores_duplicate_press_events(self):
        from AppKit import NSEventModifierFlagFunction

        class FakeEvent:
            def __init__(self, flags):
                self._flags = flags

            def modifierFlags(self):
                return self._flags

        # Two press events in a row: the second changes nothing and is gated.
        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.ends, 0)

    def test_debounce_ignores_duplicate_release_events(self):
        from AppKit import NSEventModifierFlagFunction

        class FakeEvent:
            def __init__(self, flags):
                self._flags = flags

            def modifierFlags(self):
                return self._flags

        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        self.listener._handle_event(FakeEvent(0))
        self.listener._handle_event(FakeEvent(0))
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.ends, 1)

    def test_release_is_never_debounced(self):
        from AppKit import NSEventModifierFlagFunction

        class FakeEvent:
            def __init__(self, flags):
                self._flags = flags

            def modifierFlags(self):
                return self._flags

        # Press immediately followed by release, well inside DEBOUNCE_S.
        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        self.listener._handle_event(FakeEvent(0))
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.ends, 1)

    def test_press_within_debounce_of_release_still_starts_recording(self):
        """Regression: gating on fn_down alone swallowed a genuine re-press
        within 50 ms of a release. Because flagsChanged is edge-triggered, no
        further event arrives while Fn stays held — the user spoke a whole
        sentence with no recording and no error."""
        from AppKit import NSEventModifierFlagFunction

        class FakeEvent:
            def __init__(self, flags):
                self._flags = flags

            def modifierFlags(self):
                return self._flags

        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        self.listener._handle_event(FakeEvent(0))
        # A press right after the release, still within DEBOUNCE_S.
        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        self.assertEqual(self.rec.starts, 2)
        self.assertEqual(self.rec.ends, 1)
        self.assertTrue(self.listener._held)

        # ...and the matching release is delivered too.
        self.listener._handle_event(FakeEvent(0))
        self.assertEqual(self.rec.ends, 2)

    def test_local_monitor_passes_the_event_through(self):
        """A local monitor that returns None swallows the key inside our own
        window; it must hand the event back to AppKit."""
        from AppKit import NSEventModifierFlagFunction

        class FakeEvent:
            def __init__(self, flags):
                self._flags = flags

            def modifierFlags(self):
                return self._flags

        event = FakeEvent(NSEventModifierFlagFunction)
        self.assertIs(self.listener._handle_local_event(event), event)
        self.assertEqual(self.rec.starts, 1)

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


class FnControlTests(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()
        self.listener = FnHotkeyListener(
            self.rec.start, self.rec.end, runner=_direct, on_mode_change=self.rec.change
        )

    def test_plain_fn_is_dictation(self):
        self.listener._dispatch(fn_down=True)
        self.listener._dispatch(fn_down=False)
        self.assertEqual(self.rec.modes, ["dictation"])

    def test_control_then_fn_is_command(self):
        self.listener._dispatch(fn_down=True, ctrl_down=True)
        self.assertEqual(self.rec.modes, ["command"])
        self.assertEqual(self.rec.mode_changes, [])

    def test_fn_then_control_switches_to_command_once(self):
        self.listener._dispatch(fn_down=True)
        self.listener._dispatch(fn_down=True, ctrl_down=True)
        self.listener._dispatch(fn_down=True, ctrl_down=False)
        self.listener._dispatch(fn_down=True, ctrl_down=True)
        self.assertEqual(self.rec.modes, ["dictation"])
        self.assertEqual(self.rec.mode_changes, ["command"])

    def test_control_without_fn_does_nothing(self):
        self.listener._dispatch(fn_down=False, ctrl_down=True)
        self.assertEqual(self.rec.starts, 0)

    def test_handle_event_reads_control_flag(self):
        from AppKit import NSEventModifierFlagControl, NSEventModifierFlagFunction

        class FakeEvent:
            def __init__(self, flags):
                self._flags = flags

            def modifierFlags(self):
                return self._flags

        self.listener._handle_event(FakeEvent(NSEventModifierFlagFunction))
        # Within the debounce window, but Control changed — must not be gated.
        self.listener._handle_event(
            FakeEvent(NSEventModifierFlagFunction | NSEventModifierFlagControl)
        )
        self.assertEqual(self.rec.mode_changes, ["command"])


class FnRunsOffMainThreadTests(unittest.TestCase):
    """The Fn monitor handler is delivered on the AppKit main thread.

    Calling CoreAudio from there deadlocks against CoreAudio's IO thread and
    freezes the entire app, so the default runner must move the callback off
    the thread that delivered the event.
    """

    def test_callback_runs_on_another_thread(self):
        ran_on = []
        done = threading.Event()

        def on_start(*_):
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

        def stuck_start(*_):
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

        listener = FnHotkeyListener(lambda *a: seen.append("start"), on_end)
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

        def boom(*_):
            raise RuntimeError("nope")

        listener = FnHotkeyListener(boom, done.set)
        listener.start_dispatcher()
        try:
            listener._dispatch(fn_down=True)   # raises inside the runner
            listener._dispatch(fn_down=False)  # must still be delivered
            self.assertTrue(done.wait(2.0), "dispatch thread died on exception")
        finally:
            listener.stop()


class PynputRunsOffMainThreadTests(unittest.TestCase):
    """pynput delivers on_press/on_release on its own listener thread.

    A wedged callback there must not freeze delivery of further keyboard
    events, so HotkeyListener routes callbacks through a SerialRunner just
    like FnHotkeyListener does.
    """

    def test_callback_runs_on_another_thread(self):
        ran_on = []
        done = threading.Event()

        def on_start():
            ran_on.append(threading.current_thread().ident)
            done.set()

        listener = HotkeyListener("alt_r", on_start, lambda: None)
        listener.start_dispatcher()
        try:
            listener._on_press(listener._key)
            self.assertTrue(done.wait(2.0), "callback never ran")
        finally:
            listener.stop()

        self.assertNotEqual(ran_on[0], threading.current_thread().ident)

    def test_on_press_returns_while_callback_is_still_blocked(self):
        entered = threading.Event()
        release = threading.Event()

        def stuck_start():
            entered.set()
            release.wait(5.0)  # stands in for a wedged callback

        listener = HotkeyListener("alt_r", stuck_start, lambda: None)
        listener.start_dispatcher()
        try:
            listener._on_press(listener._key)  # must not block
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

        listener = HotkeyListener("alt_r", lambda: seen.append("start"), on_end)
        listener.start_dispatcher()
        try:
            listener._on_press(listener._key)
            listener._on_release(listener._key)
            self.assertTrue(done.wait(2.0))
        finally:
            listener.stop()

        self.assertEqual(seen, ["start", "end"])

    def test_callback_exception_does_not_kill_the_dispatch_thread(self):
        done = threading.Event()

        def boom():
            raise RuntimeError("nope")

        listener = HotkeyListener("alt_r", boom, done.set)
        listener.start_dispatcher()
        try:
            listener._on_press(listener._key)    # raises inside the runner
            listener._on_release(listener._key)  # must still be delivered
            self.assertTrue(done.wait(2.0), "dispatch thread died on exception")
        finally:
            listener.stop()

    def test_injected_runner_runs_synchronously(self):
        rec = _Recorder()
        listener = HotkeyListener("alt_r", rec.start, rec.end, runner=_direct)
        listener._on_press(listener._key)
        listener._on_release(listener._key)
        self.assertEqual(rec.starts, 1)
        self.assertEqual(rec.ends, 1)

    def test_stop_joins_listener_before_stopping_runner(self):
        # pynput's Listener.stop() does not wait for its thread, so a
        # callback submission can still be mid-flight when stop() returns.
        # stop() must join the listener thread before stopping the runner,
        # or the SerialRunner None sentinel overtakes the in-flight
        # submission and the final callback is silently dropped.
        done = threading.Event()
        listener = HotkeyListener("alt_r", done.set, lambda: None)
        listener.start_dispatcher()

        class FakeListenerThread:
            def __init__(self, on_join):
                self.calls = []
                self._on_join = on_join

            def stop(self):
                self.calls.append("stop")

            def is_alive(self):
                return True

            def join(self, timeout=None):
                self.calls.append("join")
                self._on_join()  # in-flight _on_press lands during the join

        fake = FakeListenerThread(
            on_join=lambda: listener._on_press(listener._key)
        )
        listener._listener = fake
        listener.stop()

        self.assertEqual(fake.calls, ["stop", "join"])
        self.assertTrue(
            done.wait(2.0), "in-flight callback was dropped by runner shutdown"
        )


if __name__ == "__main__":
    unittest.main()
