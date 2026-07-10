"""Speech-to-text engines running locally on the GPU via MLX.

ParakeetEngine feeds audio arrays directly through get_logmel/generate,
bypassing parakeet-mlx's file-based transcribe() (which shells out to ffmpeg).
"""

import logging

import numpy as np

log = logging.getLogger(__name__)


class ParakeetEngine:
    """NVIDIA Parakeet TDT via MLX — multilingual (25 languages incl. German),
    automatic language detection, built-in punctuation and casing."""

    def __init__(self, model_id: str):
        from parakeet_mlx import from_pretrained

        log.info("Loading Parakeet model %s ...", model_id)
        self.model = from_pretrained(model_id)
        log.info("Parakeet model loaded")

    def warmup(self):
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        mel = get_logmel(mx.array(audio), self.model.preprocessor_config)
        result = self.model.generate(mel)[0]
        return result.text.strip()


class WhisperEngine:
    """OpenAI Whisper large-v3-turbo via mlx-whisper (optional fallback).
    Install with: pip install mlx-whisper"""

    def __init__(self, model_id: str):
        import mlx_whisper  # noqa: F401 — fail fast if not installed

        self.model_id = model_id

    def warmup(self):
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(audio, path_or_hf_repo=self.model_id)
        return result["text"].strip()


def create_engine(cfg):
    if cfg.engine == "whisper":
        return WhisperEngine(cfg.whisper_model)
    return ParakeetEngine(cfg.parakeet_model)
