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

import rumps

from . import config as config_mod
from .cleanup import Cleaner, KEYCHAIN_SERVICE
from .config import CONFIG_PATH, LOG_PATH, PROJECT_ROOT
from .insert import accessibility_trusted
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


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launch_agent_dest() -> Path:
    return _agents_dir() / f"{LAUNCH_AGENT_LABEL}.plist"


def _launch_agent_plist() -> bytes:
    """Build the LaunchAgent for however the app is currently running:
    the .app binary when frozen, `python -m flow` from a source checkout."""
    if config_mod.IS_FROZEN:
        program_args = [sys.executable]
        working_dir = None
    else:
        program_args = [sys.executable, "-m", "flow"]
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
    def __init__(self, pipeline):
        super().__init__(
            "Bomboclat Flow", title=STATE_ICONS["loading"], quit_button=None
        )
        self.pipeline = pipeline
        self.main_window = MainWindow()
        self._pending_state = "loading"
        self._shown_state = None

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
        # One-shot onboarding check shortly after launch, once the app is
        # registered with the window server (alerts need the run loop).
        self._onboarding_timer = rumps.Timer(self._onboarding, 1.5)
        self._onboarding_timer.start()

    # Called from any thread; a plain attribute write is atomic in CPython.
    def set_state(self, state):
        self._pending_state = state

    def _tick(self, _timer):
        if self._pending_state != self._shown_state:
            self._shown_state = self._pending_state
            self.title = STATE_ICONS.get(self._shown_state, "🎤")

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
            subprocess.run(
                ["security", "add-generic-password", "-U",
                 "-s", KEYCHAIN_SERVICE, "-a", provider, "-w", key],
                check=True, capture_output=True, timeout=5,
            )
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

    def _quit(self, _item):
        rumps.quit_application()
