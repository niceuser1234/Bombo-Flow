"""Microphone capture at 16 kHz mono float32 via sounddevice.

macOS returns silent zeros (not an error) when the Microphone permission is
missing, so callers should check `is_silent()` on the result.
"""

import logging
import threading
import time

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


def resolve_input_device(name: str | None):
    """Map a configured microphone name to a PortAudio device index.

    Matching is a case-insensitive substring, so "MacBook Air" is enough.
    Returns None — PortAudio's default — when unset or when nothing matches.

    Why this exists: recording from a Bluetooth headset forces macOS to
    switch it out of A2DP into the hands-free profile. Music playing through
    the same headset then drops to mono and changes volume mid-sentence, and
    the headset microphone itself falls back to narrowband, so the
    transcription gets worse too. Recording from the built-in microphone
    instead leaves the headset in full-quality playback mode.
    """
    if not name or not name.strip():
        return None
    wanted = name.strip().lower()
    try:
        devices = sd.query_devices()
    except Exception:
        log.exception("Could not query audio devices — using the default input")
        return None
    for index, device in enumerate(devices):
        if device.get("max_input_channels", 0) <= 0:
            continue
        if wanted in str(device.get("name", "")).lower():
            return index
    log.warning(
        "Configured input device %r not found — falling back to the system "
        "default. Available inputs: %s",
        name,
        [d["name"] for d in devices if d.get("max_input_channels", 0) > 0],
    )
    return None


class Recorder:
    def __init__(self, samplerate: int = SAMPLE_RATE, device_name: str | None = None):
        self.samplerate = samplerate
        # Resolved on every start(): the device list changes as headsets
        # connect and disconnect, and reset_portaudio() rebuilds it entirely.
        self.device_name = device_name
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        # Live mic level in 0.0..1.0, updated from the audio callback and read
        # (plain attribute, atomic under the GIL) by the recording overlay.
        self.level: float = 0.0
        # Set by abandon() to make a wedged recorder's callback a no-op.
        self._discard = False
        # Deadline set by mute_for(): captured audio before it is dropped.
        self._muted_until = 0.0

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self):
        if self._stream is not None:
            return
        self._chunks = []
        self.level = 0.0
        self._discard = False
        self._muted_until = 0.0
        stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=1,
            dtype="float32",
            callback=self._callback,
            device=resolve_input_device(self.device_name),
        )
        try:
            stream.start()
        except Exception:
            # Publish the stream only once it is actually running. A
            # half-open stream left in self._stream would make the caller's
            # recovery path call reset_portaudio() — i.e. Pa_Terminate — with
            # a live CoreAudio stream, which is exactly what crashes.
            try:
                stream.close(ignore_errors=True)
            except Exception:
                log.exception("Closing a stream that failed to start went wrong")
            raise
        self._stream = stream

    def mute_for(self, seconds: float):
        """Drop captured audio for the next `seconds`.

        Used for the start chime. The chime cannot simply be played before
        the stream opens: opening the input device reconfigures CoreAudio,
        which cuts off a sound that is already playing — audibly so, since
        most of the chime is a soft release tail. So the stream opens first,
        the chime plays into an open microphone, and this keeps it out of the
        recording instead.
        """
        if seconds > 0:
            self._muted_until = time.monotonic() + seconds

    def _callback(self, indata, frames, time_info, status):
        if self._discard:
            return
        if status:
            log.warning("audio status: %s", status)
        if self._muted_until and time.monotonic() < self._muted_until:
            return
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

    def rescue(self) -> np.ndarray:
        """Audio captured so far, for a stream whose stop() wedged.

        Copies the chunk list first: the CoreAudio callback may still append
        to it from its own thread until abandon() flips _discard."""
        chunks = list(self._chunks)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)[:, 0]

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
