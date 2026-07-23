"""Menu bar UI via rumps.

The pipeline reports state changes from worker threads; a 0.1s timer on the
main thread picks them up and updates the title (rumps/AppKit must only be
touched from the main thread).
"""

import logging
import plistlib
import subprocess
import sys
from pathlib import Path

import objc
import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyRegular
from Foundation import NSDistributedNotificationCenter, NSObject
from rumps import rumps as rumps_impl

from . import config as config_mod
from .cleanup import Cleaner, store_api_key
from .config import CONFIG_PATH, LOG_PATH, PROJECT_ROOT
from .insert import accessibility_trusted
from .lifecycle import (
    WATCHDOG_INTERVAL_S,
    OPEN_REQUEST_NOTIFICATION,
    MainLoopWatchdog,
    consume_open_request,
)
from .permissions import (
    PANE_ACCESSIBILITY,
    PANE_INPUT_MONITORING,
    input_monitoring_granted,
    open_privacy_pane,
)
from .window import MainWindow

log = logging.getLogger(__name__)

STATE_ICONS = {
    "loading": "⏳",
    "idle": "🎤",
    "recording": "🔴",
    "processing": "✍️",
    "error": "⚠️",
}

LAUNCH_AGENT_LABEL = "com.bomboclat.flow"
LEGACY_AGENT_LABELS = ["io.julius.whisperflow", "com.whisperflow.app"]


class _OpenRequestObserver(NSObject):
    """Routes a second process' distributed open request to the dashboard."""

    def initWithWindow_(self, window):
        self = objc.super(_OpenRequestObserver, self).init()
        if self is None:
            return None
        self._window = window
        return self

    def handleOpenRequest_(self, _notification):
        self._window.show()


class _FlowAppDelegate(rumps_impl.NSApp):
    """rumps delegate extended with Finder/Dock reopen handling."""

    def applicationShouldHandleReopen_hasVisibleWindows_(
        self, _application, _has_visible_windows
    ):
        window = self._app.get("main_window")
        if window is not None:
            window.show()
        return True


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launch_agent_dest() -> Path:
    return _agents_dir() / f"{LAUNCH_AGENT_LABEL}.plist"


def _launch_agent_plist() -> bytes:
    """Build the LaunchAgent for however the app is currently running:
    the .app binary when frozen, `python -m flow` from a source checkout."""
    if config_mod.IS_FROZEN:
        program_args = [sys.executable, "--background"]
        working_dir = None
    else:
        program_args = [sys.executable, "-m", "flow", "--background"]
        working_dir = str(PROJECT_ROOT)
    launchd_log = str(Path.home() / "Library" / "Logs" / "whisper-flow-launchd.log")
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": launchd_log,
        "StandardErrorPath": launchd_log,
    }
    if working_dir:
        payload["WorkingDirectory"] = working_dir
    return plistlib.dumps(payload)


class FlowApp(rumps.App):
    def __init__(self, pipeline, show_on_launch=False):
        super().__init__(
            "Bomboclat Flow", title=STATE_ICONS["loading"], quit_button=None
        )
        # Regular (not Accessory/LSUIElement): macOS only lists apps that own a
        # Dock icon in Force Quit, and a dictation app that can wedge must stay
        # killable by hand. Costs a Dock icon, buys Cmd+Tab and Force Quit.
        #
        # sharedApplication() rather than the NSApp global: rumps only creates
        # the shared instance in run(), so NSApp is still None here. This call
        # creates it; rumps then picks up the very same instance.
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyRegular
        )

        self.pipeline = pipeline
        self.main_window = MainWindow()
        self._pending_state = "loading"
        self._shown_state = None
        self._notification_center = NSDistributedNotificationCenter.defaultCenter()
        self._open_observer = _OpenRequestObserver.alloc().initWithWindow_(
            self.main_window
        )
        self._notification_center.addObserver_selector_name_object_(
            self._open_observer,
            b"handleOpenRequest:",
            OPEN_REQUEST_NOTIFICATION,
            None,
        )

        self.cleanup_item = rumps.MenuItem(
            "AI cleanup", callback=self._toggle_cleanup
        )
        self._refresh_cleanup_item()

        self.login_item = rumps.MenuItem(
            "Start at login", callback=self._toggle_login
        )
        self.login_item.state = _launch_agent_dest().exists()

        self.menu = [
            rumps.MenuItem("Open Bomboclat Flow", callback=self._open_window),
            None,
            self.cleanup_item,
            rumps.MenuItem("Set API key…", callback=self._set_api_key),
            None,
            self.login_item,
            rumps.MenuItem("Check permissions", callback=self._check_permissions),
            rumps.MenuItem("Open config", callback=self._open_config),
            rumps.MenuItem("Open log", callback=self._open_log),
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ]

        rumps.Timer(self._tick, 0.1).start()
        self._open_request_path = (
            config_mod.APP_SUPPORT / "open-dashboard.request"
        )
        self._open_request_timer = rumps.Timer(self._check_open_request, 0.2)
        self._open_request_timer.start()
        # One-shot onboarding check shortly after launch, once the app is
        # registered with the window server (alerts need the run loop).
        self._onboarding_timer = rumps.Timer(self._onboarding, 1.5)
        self._onboarding_timer.start()
        if show_on_launch:
            self._launch_timer = rumps.Timer(self._show_initial_window, 0.35)
            self._launch_timer.start()

        # ― Emergency quit: Cmd+Alt+Shift+Q, for a responsive app.
        self._install_emergency_quit()

        # ― Main-loop watchdog: a daemon thread force-exits with a stack dump
        #   when the main loop stops beating for >10 s. This is the only escape
        #   from a wedged main thread — the menu, the hotkey and the emergency
        #   shortcut all need that thread to be alive.
        self._watchdog = MainLoopWatchdog()
        self._watchdog.start()
        rumps.Timer(self._beat, WATCHDOG_INTERVAL_S / 2).start()

    def run(self, **options):
        """Run rumps with our delegate subclass while preserving its machinery."""
        original_delegate = rumps_impl.NSApp
        rumps_impl.NSApp = _FlowAppDelegate
        try:
            return super().run(**options)
        finally:
            rumps_impl.NSApp = original_delegate

    # Called from any thread; a plain attribute write is atomic in CPython.
    def set_state(self, state):
        self._pending_state = state

    def _tick(self, _timer):
        if self._pending_state != self._shown_state:
            self._shown_state = self._pending_state
            self.title = STATE_ICONS.get(self._shown_state, "🎤")

    def _beat(self, _timer):
        self._watchdog.beat()

    # -- cleanup / API key --

    def _refresh_cleanup_item(self):
        available = self.pipeline.cleaner.available
        self.cleanup_item.title = (
            "AI cleanup" if available else "AI cleanup (no API key)"
        )
        self.cleanup_item.state = self.pipeline.cleanup_enabled

    def _toggle_cleanup(self, item):
        if not self.pipeline.cleaner.available:
            provider = self.pipeline.cfg.cleanup_provider
            rumps.alert(
                f"No {provider} API key",
                "Use \"Set API key…\" in the menu to store one "
                "securely in the Keychain.",
            )
            return
        self.pipeline.cleanup_enabled = not self.pipeline.cleanup_enabled
        item.state = self.pipeline.cleanup_enabled

    def _set_api_key(self, _item):
        provider = self.pipeline.cfg.cleanup_provider
        window = rumps.Window(
            message=f"Paste your {provider} API key.\nIt is stored in the "
                    "macOS Keychain, never in a file.",
            title="Set API key",
            default_text="",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
            secure=True,
        )
        resp = window.run()
        key = resp.text.strip()
        if not resp.clicked or not key:
            return
        try:
            store_api_key(provider, key)
        except Exception:
            log.exception("Storing API key in Keychain failed")
            rumps.alert("Could not store key", f"See log: {LOG_PATH}")
            return
        self.pipeline.cleaner = Cleaner(self.pipeline.cfg)
        self.pipeline.cleanup_enabled = (
            self.pipeline.cfg.cleanup_enabled and self.pipeline.cleaner.available
        )
        self._refresh_cleanup_item()
        if self.pipeline.cleaner.available:
            rumps.alert("API key saved", "AI cleanup is now active.")
        else:
            rumps.alert("Key stored, but cleanup unavailable",
                        f"See log: {LOG_PATH}")

    # -- permissions --

    def _onboarding(self, timer):
        timer.stop()
        if not input_monitoring_granted():
            clicked = rumps.alert(
                "Input Monitoring permission needed",
                "The hotkey cannot be detected without it.\n\n"
                "System Settings → Privacy & Security → Input Monitoring:\n"
                "enable WhisperFlow, then quit and reopen the app.",
                ok="Open System Settings",
                cancel="Later",
            )
            if clicked == 1:
                open_privacy_pane(PANE_INPUT_MONITORING)

    def _show_initial_window(self, timer):
        timer.stop()
        self.main_window.show()

    def _check_open_request(self, _timer):
        if consume_open_request(self._open_request_path):
            self.main_window.show()

    def _check_permissions(self, _item):
        im = "✓" if input_monitoring_granted() else "✗"
        ax = "✓" if accessibility_trusted(prompt=False) else "✗"
        clicked = rumps.alert(
            "Permissions",
            f"{im}  Input Monitoring (hotkey)\n"
            f"{ax}  Accessibility (paste)\n"
            "•  Microphone: macOS asks on first recording\n\n"
            "After changing a permission, quit and reopen the app.",
            ok="Open System Settings",
            cancel="Close",
        )
        if clicked == 1:
            pane = (PANE_INPUT_MONITORING if im == "✗" else PANE_ACCESSIBILITY)
            open_privacy_pane(pane)

    # -- login item --

    def _toggle_login(self, item):
        dest = _launch_agent_dest()
        try:
            if dest.exists():
                subprocess.run(
                    ["launchctl", "unload", str(dest)], capture_output=True
                )
                dest.unlink()
                item.state = False
            else:
                for legacy in LEGACY_AGENT_LABELS:
                    old = _agents_dir() / f"{legacy}.plist"
                    if old.exists():
                        subprocess.run(
                            ["launchctl", "unload", str(old)],
                            capture_output=True,
                        )
                        old.unlink()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(_launch_agent_plist())
                subprocess.run(
                    ["launchctl", "load", str(dest)], capture_output=True
                )
                item.state = True
        except Exception:
            log.exception("Toggling start-at-login failed")
            rumps.alert("Start at login failed", f"See log: {LOG_PATH}")

    # -- misc --

    def _open_window(self, _item):
        self.main_window.show()

    def _open_config(self, _item):
        subprocess.run(["open", str(CONFIG_PATH)])

    def _open_log(self, _item):
        subprocess.run(["open", str(LOG_PATH)])

    def _install_emergency_quit(self):
        """Cmd+Alt+Shift+Q force-exits without going through the menu.

        This only helps while the app still responds: monitors are delivered
        on the main thread, so a wedged main loop never runs this handler.
        Recovering from that case is MainLoopWatchdog's job.

        Both a global and a local monitor are registered. A global monitor
        only sees events aimed at *other* apps, so on its own the shortcut
        would be dead whenever Bomboclat Flow itself is frontmost — which is
        precisely when a user reaches for it.
        """
        import logging as _logging
        import os as _os
        from AppKit import (
            NSEvent,
            NSEventMaskKeyDown,
            NSEventModifierFlagCommand,
            NSEventModifierFlagOption,
            NSEventModifierFlagShift,
        )

        _log = _logging.getLogger(__name__)
        _required_flags = int(
            NSEventModifierFlagCommand
            | NSEventModifierFlagOption
            | NSEventModifierFlagShift
        )

        def _handle(event):
            flags = int(event.modifierFlags())
            if event.keyCode() != 12:  # key code for 'q'
                return
            if (flags & _required_flags) != _required_flags:
                return
            _log.warning("Emergency quit (Cmd+Alt+Shift+Q) — force-exiting")
            _os._exit(1)

        def _handle_local(event):
            _handle(event)
            return event  # pass through, or we swallow keys in our own window

        self._emergency_monitors = [
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, _handle
            ),
            NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, _handle_local
            ),
        ]
        _log.info("Emergency quit handler installed (Cmd+Alt+Shift+Q)")

    def _quit(self, _item):
        from AppKit import NSEvent

        for monitor in getattr(self, "_emergency_monitors", []):
            if monitor is not None:
                NSEvent.removeMonitor_(monitor)
        self._notification_center.removeObserver_(self._open_observer)
        rumps.quit_application()
