"""whisper-flow entry point: python -m flow"""

import logging
import logging.handlers
import os

from . import config as config_mod
from .hotkey import HotkeyListener
from .insert import accessibility_trusted
from .menubar import FlowApp
from .pipeline import Pipeline


def setup_logging():
    config_mod.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config_mod.LOG_PATH, maxBytes=1_000_000, backupCount=2
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )


def main():
    setup_logging()
    log = logging.getLogger("flow")
    # librosa (via parakeet-mlx) imports numba, which wants a writable JIT
    # cache — inside a read-only .app bundle that must live elsewhere.
    numba_cache = config_mod.APP_SUPPORT / "numba-cache"
    numba_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("NUMBA_CACHE_DIR", str(numba_cache))
    cfg = config_mod.load()
    log.info("Starting whisper-flow (hotkey=%s engine=%s cleanup=%s)",
             cfg.hotkey, cfg.engine, cfg.cleanup_enabled)

    # Pasting into other apps needs the Accessibility grant for the HOST
    # process (the terminal in dev, Python.app under the LaunchAgent). Without
    # it the synthetic Cmd+V only reaches the host app, so text lands in the
    # terminal instead of the focused app. Prompt once so the fix is obvious.
    if not accessibility_trusted(prompt=True):
        log.warning(
            "Accessibility permission NOT granted for the host app — pasted "
            "text will land in the terminal, not the focused app. Grant it in "
            "System Settings > Privacy & Security > Accessibility, then fully "
            "quit and relaunch the host app."
        )

    pipeline = Pipeline(cfg, set_state=lambda s: None)
    app = FlowApp(pipeline)
    pipeline.set_state = app.set_state
    pipeline.start()

    listener = HotkeyListener(
        cfg.hotkey,
        on_hold_start=pipeline.on_hold_start,
        on_hold_end=pipeline.on_hold_end,
    )
    listener.start()

    app.run()  # blocks on the AppKit main loop


if __name__ == "__main__":
    main()
