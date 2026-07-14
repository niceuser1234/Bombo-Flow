import os
import plistlib
import tempfile
import threading
import time
import unittest
from pathlib import Path

from flow.lifecycle import (
    WATCHDOG_DEADLINE_S,
    WATCHDOG_INTERVAL_S,
    MainLoopWatchdog,
    consume_open_request,
    InstanceLock,
    is_running_from_disk_image,
    select_existing_instance,
    request_open_or_take_over,
    should_show_dashboard,
    write_open_request,
)
from flow.menubar import (
    FlowApp,
    _FlowAppDelegate,
    _OpenRequestObserver,
    _launch_agent_plist,
)


class _RunningApplication:
    def __init__(self, pid, terminated=False):
        self._pid = pid
        self._terminated = terminated

    def processIdentifier(self):
        return self._pid

    def isTerminated(self):
        return self._terminated


class _Window:
    def __init__(self):
        self.shows = 0

    def show(self):
        self.shows += 1


class LifecycleTests(unittest.TestCase):
    def test_instance_lock_allows_only_one_process_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "instance.lock"
            first = InstanceLock.acquire(path)
            second = InstanceLock.acquire(path)

            self.assertIsNotNone(first)
            self.assertIsNone(second)

            first.release()
            third = InstanceLock.acquire(path)
            self.assertIsNotNone(third)
            third.release()

    def test_open_request_persists_until_running_app_consumes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "open.request"

            write_open_request(path)

            self.assertTrue(consume_open_request(path))
            self.assertFalse(consume_open_request(path))

    def test_stale_open_request_is_removed_without_opening_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "open.request"
            write_open_request(path)
            stale = time.time() - 60.0
            os.utime(path, (stale, stale))

            self.assertFalse(consume_open_request(path, max_age_s=10.0))
            self.assertFalse(path.exists())

    def test_duplicate_takes_over_when_primary_releases_during_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "instance.lock"
            request_path = Path(directory) / "open.request"
            primary = InstanceLock.acquire(lock_path)

            takeover = request_open_or_take_over(
                lock_path,
                request_path,
                notify=primary.release,
                settle_delay_s=0.0,
            )

            self.assertIsNotNone(takeover)
            self.assertFalse(request_path.exists())
            takeover.release()

    def test_running_app_timer_consumes_durable_request_and_shows_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "open.request"
            window = _Window()
            app = type("_App", (), {})()
            app._open_request_path = path
            app.main_window = window
            write_open_request(path)

            FlowApp._check_open_request(app, None)

            self.assertEqual(window.shows, 1)
            self.assertFalse(path.exists())

    def test_select_existing_instance_excludes_current_and_terminated_processes(self):
        applications = [
            _RunningApplication(41),
            _RunningApplication(42),
            _RunningApplication(43, terminated=True),
        ]

        selected = select_existing_instance(applications, current_pid=42)

        self.assertIs(selected, applications[0])

    def test_select_existing_instance_returns_none_when_only_current_process_runs(self):
        current = _RunningApplication(42)

        self.assertIsNone(select_existing_instance([current], current_pid=42))

    def test_disk_image_detection_only_matches_volumes_mount(self):
        self.assertTrue(
            is_running_from_disk_image(
                "/Volumes/Bomboclat Flow/Bomboclat Flow.app/Contents/MacOS/BomboclatFlow"
            )
        )
        self.assertFalse(
            is_running_from_disk_image(
                "/Applications/Bomboclat Flow.app/Contents/MacOS/BomboclatFlow"
            )
        )

    def test_direct_launch_shows_dashboard_but_login_launch_stays_in_background(self):
        self.assertTrue(should_show_dashboard(["BomboclatFlow"]))
        self.assertFalse(should_show_dashboard(["BomboclatFlow", "--background"]))

    def test_launch_agent_marks_automatic_start_as_background(self):
        payload = plistlib.loads(_launch_agent_plist())

        self.assertIn("--background", payload["ProgramArguments"])

    def test_reopen_delegate_routes_launch_services_event_to_dashboard(self):
        window = _Window()
        delegate = _FlowAppDelegate.alloc().init()
        delegate._app = {"main_window": window}

        handled = delegate.applicationShouldHandleReopen_hasVisibleWindows_(None, False)

        self.assertTrue(handled)
        self.assertEqual(window.shows, 1)

    def test_distributed_open_request_routes_to_dashboard(self):
        window = _Window()
        observer = _OpenRequestObserver.alloc().initWithWindow_(window)

        observer.handleOpenRequest_(None)

        self.assertEqual(window.shows, 1)


class _Clock:
    """Hand-cranked clock, so watchdog deadlines are tested without sleeping."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MainLoopWatchdogTests(unittest.TestCase):
    def test_a_beating_main_loop_is_not_stalled(self):
        clock = _Clock()
        watchdog = MainLoopWatchdog(deadline_s=10.0, clock=clock)

        for _ in range(5):
            clock.advance(9.0)
            watchdog.beat()
            self.assertFalse(watchdog.is_stalled())

    def test_silent_main_loop_counts_as_stalled_past_the_deadline(self):
        clock = _Clock()
        watchdog = MainLoopWatchdog(deadline_s=10.0, clock=clock)

        clock.advance(10.0)
        self.assertFalse(watchdog.is_stalled())
        clock.advance(0.1)
        self.assertTrue(watchdog.is_stalled())

    def test_beat_never_blocks_the_main_thread(self):
        """The old monitor waited on an Event from the main thread, stalling the
        very loop it was meant to protect."""
        watchdog = MainLoopWatchdog()

        started = time.monotonic()
        for _ in range(1000):
            watchdog.beat()

        self.assertLess(time.monotonic() - started, 0.05)

    def test_watchdog_thread_force_exits_a_frozen_main_loop(self):
        """The main thread never beats — as when CoreAudio wedges it. Detection
        must happen off that thread, or it never happens at all."""
        stalls = []
        fired = threading.Event()

        def on_stall(stalled_for):
            stalls.append(stalled_for)
            fired.set()

        watchdog = MainLoopWatchdog(
            deadline_s=0.05, interval_s=0.01, on_stall=on_stall
        )
        watchdog.start()

        self.assertTrue(fired.wait(2.0), "watchdog never fired on a frozen loop")
        self.assertGreater(stalls[0], 0.05)

    def test_watchdog_leaves_a_healthy_main_loop_alone(self):
        fired = threading.Event()
        watchdog = MainLoopWatchdog(
            deadline_s=0.2, interval_s=0.01, on_stall=lambda _s: fired.set()
        )
        watchdog.start()
        try:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                watchdog.beat()
                time.sleep(0.01)
        finally:
            watchdog.stop()

        self.assertFalse(fired.is_set(), "watchdog killed a responsive app")

    def test_constructor_resolves_constants(self):
        self.assertGreater(WATCHDOG_INTERVAL_S, 0)
        self.assertGreater(WATCHDOG_DEADLINE_S, WATCHDOG_INTERVAL_S)


if __name__ == "__main__":
    unittest.main()
