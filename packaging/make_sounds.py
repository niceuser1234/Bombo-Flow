"""Generate flow/ui/assets/rec_start.wav and rec_stop.wav.

Synthesizes two short, quiet sine-wave blips used for recording feedback
(NSSound, played by flow/sound.py): a bright tone on start and a lower
tone on stop — reproducible, no hand-recorded assets. Plain PCM .wav (via
the stdlib `wave` module) rather than .aiff — NSSound plays it fine and it
avoids pulling in `aifc`.

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
PEAK_AMPLITUDE = 0.25
ATTACK_S = 0.010
RELEASE_S = 0.060


def _envelope(i: int, n: int, attack_n: int, release_n: int) -> float:
    if i < attack_n:
        return i / attack_n
    remaining = n - i
    if remaining < release_n:
        return remaining / release_n
    return 1.0


def _tone(freq_hz: float, duration_s: float) -> list[float]:
    n = int(SAMPLE_RATE * duration_s)
    attack_n = max(1, int(SAMPLE_RATE * ATTACK_S))
    release_n = max(1, int(SAMPLE_RATE * RELEASE_S))
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = _envelope(i, n, attack_n, release_n)
        samples.append(PEAK_AMPLITUDE * env * math.sin(2 * math.pi * freq_hz * t))
    return samples


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
    # Single bright note, ~120ms, ~880Hz (A5) — "recording started".
    start = _tone(880.0, 0.12)
    _write_wav(ASSETS_DIR / "rec_start.wav", start)

    # Lower, same shape — "recording stopped".
    stop = _tone(587.0, 0.12)  # D5
    _write_wav(ASSETS_DIR / "rec_stop.wav", stop)

    print(f"Wrote {ASSETS_DIR / 'rec_start.wav'}")
    print(f"Wrote {ASSETS_DIR / 'rec_stop.wav'}")


if __name__ == "__main__":
    main()
