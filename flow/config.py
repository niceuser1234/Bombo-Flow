"""Load config.toml into a simple namespace with defaults."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
LOG_PATH = Path.home() / "Library" / "Logs" / "whisper-flow.log"


@dataclass
class Config:
    hotkey: str = "alt_r"
    min_duration_s: float = 0.3
    max_duration_s: float = 120.0
    engine: str = "parakeet"
    parakeet_model: str = "mlx-community/parakeet-tdt-0.6b-v3"
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    cleanup_enabled: bool = True
    cleanup_model: str = "claude-haiku-4-5"
    cleanup_min_words: int = 4
    cleanup_timeout_s: float = 5.0
    insert_mode: str = "paste"


def load() -> Config:
    cfg = Config()
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
    cfg.engine = stt.get("engine", cfg.engine)
    cfg.parakeet_model = stt.get("parakeet_model", cfg.parakeet_model)
    cfg.whisper_model = stt.get("whisper_model", cfg.whisper_model)
    cfg.cleanup_enabled = bool(cleanup.get("enabled", cfg.cleanup_enabled))
    cfg.cleanup_model = cleanup.get("model", cfg.cleanup_model)
    cfg.cleanup_min_words = int(cleanup.get("min_words", cfg.cleanup_min_words))
    cfg.cleanup_timeout_s = float(cleanup.get("timeout_s", cfg.cleanup_timeout_s))
    cfg.insert_mode = insert.get("mode", cfg.insert_mode)
    return cfg
