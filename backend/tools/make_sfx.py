"""Generate the one-shot sound effects used by the highlight reel.

Original synthesis only -- no samples, no licensing, no network -- like the
music beds in make_music.py. Run from backend/:

    ./venv/bin/python tools/make_sfx.py
"""

import os
import wave

import numpy as np

SR = 48000


def boom(seconds=1.1):
    """The meme-beat hit: a sub that drops in pitch, a click on top, saturated."""
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float64) / SR
    # Pitch falls 130 -> 38 Hz; phase is the running integral of frequency.
    freq = 38 + 92 * np.exp(-t * 5.5)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * np.exp(-t * 3.2)
    # The transient that makes it read as a hit rather than a hum.
    rng = np.random.default_rng(7)
    click = rng.standard_normal(n) * np.exp(-t * 90) * 0.5
    x = np.tanh(2.6 * (body + click))
    # 5 ms fade-in so the first sample is not a step.
    fade = min(n, int(0.005 * SR))
    x[:fade] *= np.linspace(0, 1, fade)
    x *= 0.89 / max(1e-9, np.abs(x).max())     # ~ -1 dBFS peak
    return x


def write(path, mono):
    pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(stereo.tobytes())
    print(f"wrote {path}")


if __name__ == "__main__":
    OUT = os.path.join(os.path.dirname(__file__), "..", "assets", "sfx")
    os.makedirs(OUT, exist_ok=True)
    write(os.path.join(OUT, "boom.wav"), boom())
