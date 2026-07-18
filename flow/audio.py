"""Microphone capture at 16 kHz mono float32 via sounddevice.

macOS returns silent zeros (not an error) when the Microphone permission is
missing, so callers should check `is_silent()` on the result.
"""

import logging
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SILENCE_RMS = 1e-4  # below this the mic almost certainly delivered zeros
LEVEL_GAIN = 12.0
ATTACK_ALPHA = 0.82
RELEASE_ALPHA = 0.18


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def compute_normalized_level(indata: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
    return _clamp01(rms * LEVEL_GAIN)


def apply_attack_release(
    previous: float,
    incoming: float,
    attack: float = ATTACK_ALPHA,
    release: float = RELEASE_ALPHA,
) -> float:
    prev = _clamp01(float(previous))
    target = _clamp01(float(incoming))
    alpha = attack if target >= prev else release
    alpha = _clamp01(alpha)
    return _clamp01(prev + alpha * (target - prev))


class Recorder:
    def __init__(self, samplerate: int = SAMPLE_RATE):
        self.samplerate = samplerate
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        # Live mic level in 0.0..1.0, updated from the audio callback and read
        # (plain attribute, atomic under the GIL) by the recording overlay.
        self.level: float = 0.0
        # Set by abandon() to make a wedged recorder's callback a no-op.
        self._discard = False

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self):
        if self._stream is not None:
            return
        self._chunks = []
        self.level = 0.0
        self._discard = False
        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        if self._discard:
            return
        if status:
            log.warning("audio status: %s", status)
        self._chunks.append(indata.copy())
        new = compute_normalized_level(indata)
        # Fast attack + slower release: reacts immediately without jittering.
        self.level = apply_attack_release(self.level, new)

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

    def abandon(self):
        """Give up on a wedged stream: stop feeding the callback and close
        the CoreAudio stream in the background, without blocking or ever
        touching `self._stream` again (a late-completing `stop()` may still
        be racing to clear it)."""
        self._discard = True
        self._chunks = []
        self.level = 0.0
        stream = self._stream

        def _close():
            try:
                stream.abort(ignore_errors=True)
                stream.close(ignore_errors=True)
            except Exception:
                log.exception("Error closing abandoned stream")

        threading.Thread(target=_close, daemon=True, name="recorder-abort").start()

    def duration(self) -> float:
        """Seconds captured so far (approximate, while recording)."""
        frames = sum(len(c) for c in self._chunks)
        return frames / self.samplerate


def is_silent(audio: np.ndarray) -> bool:
    if len(audio) == 0:
        return True
    rms = float(np.sqrt(np.mean(audio**2)))
    return rms < SILENCE_RMS


def reset_portaudio():
    """Rebuild PortAudio's device state.

    PortAudio snapshots the CoreAudio device list once at initialization.
    When the default input changes later (AirPods/headset connect or
    disconnect, sleep/wake), new InputStreams can open against a stale
    device and silently deliver zeros until the process restarts.
    Terminate + re-initialize forces a fresh snapshot. Only call while no
    stream is active."""
    try:
        sd._terminate()
        sd._initialize()
        log.info("PortAudio reset — device list refreshed")
    except Exception:
        log.exception("PortAudio reset failed")
