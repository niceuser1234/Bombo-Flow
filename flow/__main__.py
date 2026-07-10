"""whisper-flow entry point: python -m flow"""

import logging
import logging.handlers

from . import config as config_mod
from .hotkey import HotkeyListener
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
    cfg = config_mod.load()
    log.info("Starting whisper-flow (hotkey=%s engine=%s cleanup=%s)",
             cfg.hotkey, cfg.engine, cfg.cleanup_enabled)

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
