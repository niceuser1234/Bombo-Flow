"""Load config.toml into a simple namespace with defaults.

Path resolution is bundle-aware: running from a source checkout, config.toml
lives next to the flow/ package; inside a frozen .app bundle (PyInstaller sets
sys.frozen) the bundle is read-only, so the config lives in
~/Library/Application Support/whisper-flow/ and is seeded from the bundled
default on first launch.
"""

import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "whisper-flow"
LOG_PATH = Path.home() / "Library" / "Logs" / "whisper-flow.log"

if IS_FROZEN:
    CONFIG_PATH = APP_SUPPORT / "config.toml"
else:
    CONFIG_PATH = PROJECT_ROOT / "config.toml"


def _bundled_default_config() -> Path:
    # PyInstaller unpacks data files under sys._MEIPASS.
    return Path(getattr(sys, "_MEIPASS", PROJECT_ROOT)) / "config.toml"


@dataclass
class Config:
    hotkey: str = "fn"
    min_duration_s: float = 0.3
    max_duration_s: float = 120.0
    engine: str = "whisper"
    parakeet_model: str = "mlx-community/parakeet-tdt-0.6b-v3"
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    cleanup_enabled: bool = True
    cleanup_provider: str = "openrouter"
    cleanup_base_url: str = "https://openrouter.ai/api/v1"
    cleanup_model: str = "google/gemini-2.5-flash-lite"
    cleanup_min_words: int = 4
    cleanup_timeout_s: float = 5.0
    insert_mode: str = "paste"
    sounds: bool = True


def load() -> Config:
    cfg = Config()
    if IS_FROZEN and not CONFIG_PATH.exists():
        default = _bundled_default_config()
        if default.exists():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(default, CONFIG_PATH)
    if not CONFIG_PATH.exists():
        return cfg
    with open(CONFIG_PATH, "rb") as f:
        data = tomllib.load(f)
    hotkey = data.get("hotkey", {})
    audio = data.get("audio", {})
    stt = data.get("stt", {})
    cleanup = data.get("cleanup", {})
    insert = data.get("insert", {})
    cfg.hotkey = hotkey.get("key", cfg.hotkey)
    cfg.min_duration_s = float(audio.get("min_duration_s", cfg.min_duration_s))
    cfg.max_duration_s = float(audio.get("max_duration_s", cfg.max_duration_s))
    cfg.sounds = bool(audio.get("sounds", cfg.sounds))
    cfg.engine = stt.get("engine", cfg.engine)
    cfg.parakeet_model = stt.get("parakeet_model", cfg.parakeet_model)
    cfg.whisper_model = stt.get("whisper_model", cfg.whisper_model)
    cfg.cleanup_enabled = bool(cleanup.get("enabled", cfg.cleanup_enabled))
    cfg.cleanup_provider = cleanup.get("provider", cfg.cleanup_provider)
    cfg.cleanup_base_url = cleanup.get("base_url", cfg.cleanup_base_url)
    cfg.cleanup_model = cleanup.get("model", cfg.cleanup_model)
    cfg.cleanup_min_words = int(cleanup.get("min_words", cfg.cleanup_min_words))
    cfg.cleanup_timeout_s = float(cleanup.get("timeout_s", cfg.cleanup_timeout_s))
    cfg.insert_mode = insert.get("mode", cfg.insert_mode)
    return cfg
