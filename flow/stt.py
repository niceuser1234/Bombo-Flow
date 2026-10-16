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

    def transcribe(self, audio: np.ndarray, initial_prompt: str | None = None) -> str:
        # initial_prompt is accepted for call-site uniformity with
        # WhisperEngine but ignored — parakeet-mlx has no biasing hook.
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        mel = get_logmel(mx.array(audio), self.model.preprocessor_config)
        result = self.model.generate(mel)[0]
        return result.text.strip()


# Same thresholds mlx_whisper.transcribe uses by default.
TEMPERATURES = (0.0, 0.2, 0.4)
COMPRESSION_RATIO_THRESHOLD = 2.4
LOGPROB_THRESHOLD = -1.0
NO_SPEECH_THRESHOLD = 0.6


def decode_with_fallback(model, features, prompt_tokens):
    """Decode pre-encoded features, retrying at higher temperatures when the
    result looks repetitive or unlikely - mirrors mlx_whisper.transcribe's
    ladder, minus the encoder pass it repeats on every call."""
    from mlx_whisper.decoding import DecodingOptions

    result = None
    for t in TEMPERATURES:
        result = model.decode(
            features, DecodingOptions(temperature=t, prompt=prompt_tokens, fp16=True)
        )
        if result.no_speech_prob > NO_SPEECH_THRESHOLD:
            break  # silence - a hotter decode will not help
        if (result.compression_ratio <= COMPRESSION_RATIO_THRESHOLD
                and result.avg_logprob >= LOGPROB_THRESHOLD):
            break
    return result


class WhisperEngine:
    """OpenAI Whisper large-v3-turbo via mlx-whisper (optional fallback).
    Install with: pip install mlx-whisper"""

    def __init__(self, model_id: str):
        import mlx_whisper  # noqa: F401 — fail fast if not installed

        self.model_id = model_id

    def warmup(self):
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def _model(self):
        import importlib

        import mlx.core as mx

        # importlib: the package re-exports the transcribe *function* under
        # the same name as the module that holds ModelHolder.
        holder = importlib.import_module("mlx_whisper.transcribe").ModelHolder
        return holder.get_model(self.model_id, mx.float16)

    def transcribe(self, audio: np.ndarray, initial_prompt: str | None = None) -> str:
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES

        if len(audio) > N_SAMPLES:
            # Longer than one 30 s window (hands-free dictations): the
            # library's seek loop handles the windowing.
            return self._transcribe_windowed(audio, initial_prompt)

        import mlx.core as mx
        from mlx_whisper.audio import log_mel_spectrogram, pad_or_trim
        from mlx_whisper.tokenizer import get_tokenizer

        model = self._model()
        # Same framing as mlx_whisper.transcribe's decode: the content frames,
        # zero-padded to the 30 s window the encoder expects.
        mel = log_mel_spectrogram(audio, n_mels=model.dims.n_mels, padding=N_SAMPLES)
        content = mel[: mel.shape[-2] - N_FRAMES]
        segment = pad_or_trim(content, N_FRAMES, axis=-2).astype(mx.float16)
        # One encoder pass, reused for language detection and every
        # temperature fallback (transcribe() encodes at least twice).
        features = model.encoder(segment[None])[0]

        prompt_tokens = None
        if initial_prompt:
            tokenizer = get_tokenizer(
                model.is_multilingual, num_languages=model.num_languages, task="transcribe"
            )
            prompt_tokens = tokenizer.encode(" " + initial_prompt.strip())

        result = decode_with_fallback(model, features, prompt_tokens)
        if (result.no_speech_prob > NO_SPEECH_THRESHOLD
                and result.avg_logprob < LOGPROB_THRESHOLD):
            return ""  # transcribe() drops such segments as silence
        return result.text.strip()

    def _transcribe_windowed(self, audio, initial_prompt):
        import mlx_whisper

        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.model_id,
            initial_prompt=initial_prompt,
            # Each dictation is one independent utterance: carrying decoded
            # text across 30s windows costs time and propagates
            # hallucinations into later windows.
            condition_on_previous_text=False,
            temperature=TEMPERATURES,
        )
        return result["text"].strip()


def create_engine(cfg):
    if cfg.engine == "whisper":
        return WhisperEngine(cfg.whisper_model)
    return ParakeetEngine(cfg.parakeet_model)
