"""whisper-flow entry point: python -m flow"""

import logging
import logging.handlers
import os
import sys

from . import config as config_mod
from .hotkey import make_listener
from .insert import accessibility_trusted
from .lifecycle import (
    InstanceLock,
    is_running_from_disk_image,
    request_open_or_take_over,
    request_existing_instance_to_open,
    should_show_dashboard,
    show_install_required_alert,
)
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

    instance_lock = None
    if config_mod.IS_FROZEN:
        if is_running_from_disk_image():
            log.info("Refusing to run from a mounted disk image")
            show_install_required_alert()
            return

        lock_path = config_mod.APP_SUPPORT / "instance.lock"
        request_path = config_mod.APP_SUPPORT / "open-dashboard.request"
        instance_lock = InstanceLock.acquire(lock_path)
        if instance_lock is None:
            instance_lock = request_open_or_take_over(
                lock_path,
                request_path,
                notify=request_existing_instance_to_open,
            )
            if instance_lock is None:
                log.info("Dashboard request forwarded to existing instance")
                return
            log.info("Previous instance exited during handoff; taking over")

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

    try:
        from .store import get_store

        purged = get_store().purge_old(days=14)
        if purged:
            log.info("Purged %d history entries older than 14 days", purged)
    except Exception:
        log.exception("History purge failed")

    pipeline = Pipeline(cfg, set_state=lambda s: None)
    app = FlowApp(pipeline, show_on_launch=should_show_dashboard(sys.argv))
    pipeline.set_state = app.set_state
    pipeline.start()

    listener = make_listener(
        cfg.hotkey,
        on_hold_start=pipeline.on_hold_start,
        on_hold_end=pipeline.on_hold_end,
    )
    listener.start()

    try:
        app.run()  # blocks on the AppKit main loop
    finally:
        if instance_lock is not None:
            instance_lock.release()


if __name__ == "__main__":
    main()
