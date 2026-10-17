"""macOS application lifecycle helpers.

The packaged app is a menu-bar agent, so LaunchServices events need explicit
routing to the dashboard and duplicate processes must exit before loading the
speech model.
"""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import IO, Callable, Iterable, Protocol

log = logging.getLogger(__name__)


BUNDLE_IDENTIFIER = "com.bomboclat.flow"
OPEN_REQUEST_NOTIFICATION = "com.bomboclat.flow.open-dashboard"


class _RunningApplication(Protocol):
    def processIdentifier(self) -> int: ...

    def isTerminated(self) -> bool: ...


class InstanceLock:
    """Lifetime-held advisory lock preventing duplicate frozen processes."""

    def __init__(self, handle: IO[str]):
        self._handle = handle

    @classmethod
    def acquire(cls, path: Path) -> InstanceLock | None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return cls(handle)

    def release(self) -> None:
        if self._handle.closed:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()


def write_open_request(path: Path) -> None:
    """Persist an open request until the primary app's run loop can consume it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def consume_open_request(path: Path, max_age_s: float = 10.0) -> bool:
    """Consume a fresh open request, discarding stale requests after crashes."""
    try:
        age_s = max(0.0, time.time() - path.stat().st_mtime)
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        # Runs from a rumps timer on the main thread; anything escaping here
        # (a permission problem, a stale mount) would surface as a crash.
        log.exception("Could not consume the open request at %s", path)
        return False
    return age_s <= max_age_s


def request_open_or_take_over(
    lock_path: Path,
    request_path: Path,
    notify: Callable[[], object],
    settle_delay_s: float = 0.15,
) -> InstanceLock | None:
    """Notify the primary app, or take ownership if it exits during handoff."""
    write_open_request(request_path)
    notify()
    if settle_delay_s > 0:
        time.sleep(settle_delay_s)
    takeover = InstanceLock.acquire(lock_path)
    if takeover is not None:
        consume_open_request(request_path, max_age_s=float("inf"))
    return takeover


def select_existing_instance(
    applications: Iterable[_RunningApplication], current_pid: int
) -> _RunningApplication | None:
    """Return another live instance, excluding this process."""
    for application in applications:
        if application.processIdentifier() == current_pid:
            continue
        if application.isTerminated():
            continue
        return application
    return None


def existing_instance() -> _RunningApplication | None:
    """Find another packaged app process with the same bundle identifier."""
    from AppKit import NSRunningApplication

    applications = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        BUNDLE_IDENTIFIER
    )
    return select_existing_instance(applications, os.getpid())


def request_existing_instance_to_open() -> bool:
    """Ask a running instance to show its dashboard.

    Returns ``True`` when another instance was found and notified.
    """
    application = existing_instance()
    if application is None:
        return False

    from Foundation import NSDistributedNotificationCenter

    center = NSDistributedNotificationCenter.defaultCenter()
    center.postNotificationName_object_userInfo_deliverImmediately_(
        OPEN_REQUEST_NOTIFICATION, BUNDLE_IDENTIFIER, None, True
    )
    application.activateWithOptions_(1 << 1)  # activate ignoring other apps
    return True


def is_running_from_disk_image(executable: str | Path | None = None) -> bool:
    """Whether the executable lives below macOS' standard /Volumes mount."""
    path = Path(executable or sys.executable)
    try:
        path = path.resolve()
    except OSError:
        pass
    return len(path.parts) > 1 and path.parts[:2] == ("/", "Volumes")


def should_show_dashboard(arguments: list[str]) -> bool:
    """Direct launches show UI; login-item launches remain unobtrusive."""
    return "--background" not in arguments


def show_install_required_alert() -> None:
    """Explain why running directly from the mounted DMG is unsupported."""
    from AppKit import NSAlert, NSAlertStyleInformational, NSApplication

    NSApplication.sharedApplication()
    alert = NSAlert.alloc().init()
    alert.setAlertStyle_(NSAlertStyleInformational)
    alert.setMessageText_("Move Bomboclat Flow to Applications")
    alert.setInformativeText_(
        "Drag Bomboclat Flow into the Applications folder, eject the installer, "
        "then open the app from Applications. Permissions cannot remain stable "
        "while the app runs from the disk image."
    )
    alert.addButtonWithTitle_("OK")
    alert.runModal()


# ---------------------------------------------------------------------------
# Main-loop watchdog
# ---------------------------------------------------------------------------

WATCHDOG_INTERVAL_S = 2.0
WATCHDOG_DEADLINE_S = 10.0


class MainLoopWatchdog:
    """Force-exit the process when the AppKit main loop stops responding.

    The observer must live *outside* the thread it observes: a wedged main
    thread — stuck in a CoreAudio mutex, say — can no longer run a timer, a
    menu callback or a global hotkey handler, so it cannot detect or rescue
    itself, and macOS offers no Force Quit entry for an accessory app.

    So the main thread only stamps a timestamp (:meth:`beat`, a non-blocking
    float write from a rumps timer) while this daemon thread compares it
    against *deadline_s* and, on a stall, dumps every thread's stack and hard
    exits. ``os._exit`` is deliberate — an orderly shutdown would have to run
    on the very thread that is stuck.
    """

    def __init__(
        self,
        deadline_s: float = WATCHDOG_DEADLINE_S,
        interval_s: float = WATCHDOG_INTERVAL_S,
        on_stall: Callable[[float], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._deadline_s = deadline_s
        self._interval_s = interval_s
        self._on_stall = on_stall if on_stall is not None else _force_exit
        self._clock = clock
        self._last_beat = clock()
        self._stopped = threading.Event()

    def beat(self) -> None:
        """Stamp liveness from the main thread. A float write is atomic."""
        self._last_beat = self._clock()

    def stalled_for(self) -> float:
        return self._clock() - self._last_beat

    def is_stalled(self) -> bool:
        return self.stalled_for() > self._deadline_s

    def start(self) -> None:
        thread = threading.Thread(
            target=self._loop, daemon=True, name="main-loop-watchdog"
        )
        thread.start()

    def stop(self) -> None:
        self._stopped.set()

    def _loop(self) -> None:
        while not self._stopped.wait(self._interval_s):
            if self.is_stalled():
                self._on_stall(self.stalled_for())
                return


def _force_exit(stalled_for: float) -> None:
    log.error(
        "AppKit main loop unresponsive for %.1fs — dumping stacks and exiting",
        stalled_for,
    )
    _dump_stacks()
    os._exit(1)


def _dump_stacks() -> None:
    """Log every Python thread's current stack trace before force-exiting."""
    frames = sys._current_frames()
    for tid, frame in sorted(frames.items()):
        stack = traceback.format_stack(frame)
        log.error("Thread %d stack:\n%s", tid, "".join(stack))


# Waits for the old process ($0 = its pid) to exit — it holds the
# single-instance lock until then — and only then starts the new one.
_WAIT_THEN_EXEC = 'while kill -0 "$0" 2>/dev/null; do sleep 0.2; done; exec "$@"'


def relaunch_command(pid: int, executable: str, frozen: bool, project_root: Path) -> list[str]:
    """Shell command that starts a fresh copy of the app once `pid` is gone.
    `--background` keeps the dashboard from popping up on a relaunch."""
    if frozen:
        bundle = str(Path(executable).resolve().parents[2])
        target = ["open", bundle, "--args", "--background"]
    else:
        target = [executable, "-m", "flow", "--background"]
    return ["/bin/sh", "-c", _WAIT_THEN_EXEC, str(pid), *target]


def relaunch(reason: str) -> None:
    """Replace this process with a fresh one.

    A CoreAudio HAL that wedged inside AudioDeviceStop never recovers within
    the process: every later stream open blocks forever. Restarting is the
    only cure, so do it for the user instead of leaving the hotkey dead.
    """
    from .config import IS_FROZEN, PROJECT_ROOT

    log.error("Relaunching the app: %s", reason)
    _dump_stacks()
    try:
        subprocess.Popen(
            relaunch_command(os.getpid(), sys.executable, IS_FROZEN, PROJECT_ROOT),
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        log.exception("Could not spawn the relaunch helper")
    # os._exit: an orderly shutdown would join threads stuck in CoreAudio.
    os._exit(0)
