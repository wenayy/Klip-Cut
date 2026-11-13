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


def impact(seconds=0.42):
    """A short, dry hit for punch-ins: less sub than the meme boom."""
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float64) / SR
    freq = 46 + 120 * np.exp(-t * 18)
    body = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 7.5)
    rng = np.random.default_rng(13)
    click = rng.standard_normal(n) * np.exp(-t * 120) * 0.35
    x = np.tanh(2.0 * (body + click))
    fade = min(n, int(0.004 * SR))
    x[:fade] *= np.linspace(0, 1, fade)
    x *= 0.85 / max(1e-9, np.abs(x).max())
    return x


def riser(seconds=0.9):
    """A rising noise sweep that lands on the cold open."""
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float64) / SR
    rng = np.random.default_rng(17)
    noise = rng.standard_normal(n)
    # One-pole lowpass whose cutoff opens with the sweep; the top end arrives
    # just before the cut.
    cutoff = 0.01 + 0.22 * (t / max(seconds, 1e-6)) ** 2
    out = np.empty(n)
    prev = 0.0
    for i, value in enumerate(noise):
        prev += cutoff[i] * (value - prev)
        out[i] = prev
    sweep = 90 + 900 * (t / max(seconds, 1e-6)) ** 1.8
    out += 0.18 * np.sin(2 * np.pi * np.cumsum(sweep) / SR)
    env = np.linspace(0.0, 1.0, n) ** 2
    out *= env
    out *= 0.6 / max(1e-9, np.abs(out).max())
    return out


def pop(seconds=0.12):
    """A tiny text/caption pop."""
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float64) / SR
    x = np.sin(2 * np.pi * 880 * t) * np.exp(-t * 55)
    x += 0.35 * np.sin(2 * np.pi * 1760 * t) * np.exp(-t * 80)
    fade = min(n, int(0.002 * SR))
    x[:fade] *= np.linspace(0, 1, fade)
    x *= 0.5 / max(1e-9, np.abs(x).max())
    return x


if __name__ == "__main__":
    OUT = os.path.join(os.path.dirname(__file__), "..", "assets", "sfx")
    os.makedirs(OUT, exist_ok=True)
    write(os.path.join(OUT, "boom.wav"), boom())
    write(os.path.join(OUT, "impact.wav"), impact())
    write(os.path.join(OUT, "riser.wav"), riser())
    write(os.path.join(OUT, "pop.wav"), pop())
