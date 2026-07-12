"""Microphone capture at 16 kHz mono float32 via sounddevice.

macOS returns silent zeros (not an error) when the Microphone permission is
missing, so callers should check `is_silent()` on the result.
"""

import logging

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SILENCE_RMS = 1e-4  # below this the mic almost certainly delivered zeros


class Recorder:
    def __init__(self, samplerate: int = SAMPLE_RATE):
        self.samplerate = samplerate
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        # Live mic level in 0.0..1.0, updated from the audio callback and read
        # (plain attribute, atomic under the GIL) by the recording overlay.
        self.level: float = 0.0

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self):
        if self._stream is not None:
            return
        self._chunks = []
        self.level = 0.0
        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        if status:
            log.warning("audio status: %s", status)
        self._chunks.append(indata.copy())
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        new = min(1.0, rms * 12.0)
        # Light smoothing so the waveform eases rather than jitters.
        self.level = 0.6 * self.level + 0.4 * new

    def stop(self) -> np.ndarray:
        """Stop recording and return the captured audio as 1-D float32."""
        if self._stream is None:
            return np.zeros(0, dtype=np.float32)
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self.level = 0.0
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(self._chunks)[:, 0]
        self._chunks = []
        return audio

    def duration(self) -> float:
        """Seconds captured so far (approximate, while recording)."""
        frames = sum(len(c) for c in self._chunks)
        return frames / self.samplerate


def is_silent(audio: np.ndarray) -> bool:
    if len(audio) == 0:
        return True
    rms = float(np.sqrt(np.mean(audio**2)))
    return rms < SILENCE_RMS
