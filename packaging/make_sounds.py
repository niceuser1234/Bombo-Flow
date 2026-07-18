"""Generate flow/ui/assets/rec_start.wav and rec_stop.wav.

Synthesizes two soft two-note chimes used for recording feedback (NSSound,
played by flow/sound.py): an ascending pair when recording starts and a
lower descending pair when it stops. Each note is a sine fundamental plus a
quiet 2nd harmonic with a fast attack and a long release — warmer and less
piercing than a bare sine blip. Plain PCM .wav via the stdlib `wave` module.

Run when the sound design changes (the committed .wav files are what the
app plays):
    .venv/bin/python packaging/make_sounds.py
"""

import math
import struct
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS_DIR = HERE.parent / "flow" / "ui" / "assets"

SAMPLE_RATE = 44100
PEAK_AMPLITUDE = 0.16
ATTACK_S = 0.006
RELEASE_S = 0.14
NOTE_S = 0.16       # length of each note
STAGGER_S = 0.07    # offset between the two notes (they overlap)
HARMONIC_2ND = 0.30  # level of the 2nd harmonic relative to the fundamental


def _envelope(i: int, n: int, attack_n: int, release_n: int) -> float:
    if i < attack_n:
        return i / attack_n
    remaining = n - i
    if remaining < release_n:
        return remaining / release_n
    return 1.0


def _note(freq_hz: float) -> list[float]:
    """One note: sine + quiet 2nd harmonic, unscaled (normalized later)."""
    n = int(SAMPLE_RATE * NOTE_S)
    attack_n = max(1, int(SAMPLE_RATE * ATTACK_S))
    release_n = max(1, int(SAMPLE_RATE * RELEASE_S))
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = _envelope(i, n, attack_n, release_n)
        s = (math.sin(2 * math.pi * freq_hz * t)
             + HARMONIC_2ND * math.sin(2 * math.pi * 2 * freq_hz * t))
        samples.append(env * s)
    return samples


def _chime(freqs: list[float]) -> list[float]:
    """Overlapping notes at STAGGER_S offsets, normalized to PEAK_AMPLITUDE."""
    total_n = int(SAMPLE_RATE * (STAGGER_S * (len(freqs) - 1) + NOTE_S))
    buf = [0.0] * total_n
    for k, freq in enumerate(freqs):
        offset = int(SAMPLE_RATE * STAGGER_S * k)
        for i, s in enumerate(_note(freq)):
            buf[offset + i] += s
    peak = max(abs(s) for s in buf) or 1.0
    return [s * (PEAK_AMPLITUDE / peak) for s in buf]


def _write_wav(path: Path, samples: list[float]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)  # 16-bit PCM
        f.setframerate(SAMPLE_RATE)
        frames = b"".join(
            struct.pack("<h", max(-32768, min(32767, int(s * 32767))))
            for s in samples
        )
        f.writeframes(frames)


def main():
    # Ascending G5 -> C6: "recording started".
    _write_wav(ASSETS_DIR / "rec_start.wav", _chime([783.99, 1046.50]))
    # Lower descending E5 -> C5: "recording stopped".
    _write_wav(ASSETS_DIR / "rec_stop.wav", _chime([659.25, 523.25]))
    print(f"Wrote {ASSETS_DIR / 'rec_start.wav'}")
    print(f"Wrote {ASSETS_DIR / 'rec_stop.wav'}")


if __name__ == "__main__":
    main()
