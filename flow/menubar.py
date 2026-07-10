"""Menu bar UI via rumps.

The pipeline reports state changes from worker threads; a 0.1s timer on the
main thread picks them up and updates the title (rumps/AppKit must only be
touched from the main thread).
"""

import logging
import subprocess

import rumps

from .config import PROJECT_ROOT, LOG_PATH

log = logging.getLogger(__name__)

STATE_ICONS = {
    "loading": "⏳",
    "idle": "🎤",
    "recording": "🔴",
    "processing": "✍️",
    "error": "⚠️",
}

LAUNCH_AGENT_LABEL = "io.julius.whisperflow"
LAUNCH_AGENT_SRC = PROJECT_ROOT / "launchagent" / f"{LAUNCH_AGENT_LABEL}.plist"


def _launch_agent_dest():
    import pathlib

    return pathlib.Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


class FlowApp(rumps.App):
    def __init__(self, pipeline):
        super().__init__("Flow", title=STATE_ICONS["loading"], quit_button=None)
        self.pipeline = pipeline
        self._pending_state = "loading"
        self._shown_state = None

        self.cleanup_item = rumps.MenuItem(
            "AI cleanup", callback=self._toggle_cleanup
        )
        self.cleanup_item.state = pipeline.cleanup_enabled
        if not pipeline.cleaner.available:
            self.cleanup_item.title = "AI cleanup (no API key)"

        self.login_item = rumps.MenuItem(
            "Start at login", callback=self._toggle_login
        )
        self.login_item.state = _launch_agent_dest().exists()

        self.menu = [
            self.cleanup_item,
            self.login_item,
            rumps.MenuItem("Open log", callback=self._open_log),
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ]

        rumps.Timer(self._tick, 0.1).start()

    # Called from any thread; a plain attribute write is atomic in CPython.
    def set_state(self, state):
        self._pending_state = state

    def _tick(self, _timer):
        if self._pending_state != self._shown_state:
            self._shown_state = self._pending_state
            self.title = STATE_ICONS.get(self._shown_state, "🎤")

    def _toggle_cleanup(self, item):
        if not self.pipeline.cleaner.available:
            rumps.alert(
                "No Anthropic API key",
                "Store one with:\nsecurity add-generic-password "
                "-s whisper-flow -a anthropic -w YOUR_KEY\nthen restart Flow.",
            )
            return
        self.pipeline.cleanup_enabled = not self.pipeline.cleanup_enabled
        item.state = self.pipeline.cleanup_enabled

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
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(LAUNCH_AGENT_SRC.read_text())
                subprocess.run(
                    ["launchctl", "load", str(dest)], capture_output=True
                )
                item.state = True
        except Exception:
            log.exception("Toggling start-at-login failed")
            rumps.alert("Start at login failed", f"See log: {LOG_PATH}")

    def _open_log(self, _item):
        subprocess.run(["open", str(LOG_PATH)])

    def _quit(self, _item):
        rumps.quit_application()
