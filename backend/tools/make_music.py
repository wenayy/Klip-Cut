"""Generate the three original music beds shipped with KlipCut.

No samples, no licensing, no network: a few sine/saw voices, a kick, a snare
and a hat. They are mixed at -20 dBFS and used under speech with a sidechain,
so the job is a bed that sets a mood, not a track that competes with the voice.
"""

import math
import os
import wave

import numpy as np

SR = 44100


def adsr(n, attack=0.02, decay=0.15, sustain=0.6, release=0.2):
    a = max(1, int(attack * SR))
    d = max(1, int(decay * SR))
    r = max(1, int(release * SR))
    env = np.ones(n, dtype=np.float32)
    env[:a] = np.linspace(0, 1, a)
    if d < n:
        env[a:a + d] = np.linspace(1, sustain, min(d, n - a))
    env[a + d:] = sustain
    if r < n:
        env[-r:] *= np.linspace(1, 0, r)
    return env


def voice(freq, n, amp=1.0, kind="sine", detune=0.0):
    t = np.arange(n, dtype=np.float32) / SR
    f = freq * (1 + detune)
    if kind == "sine":
        x = np.sin(2 * np.pi * f * t)
    elif kind == "saw":
        x = 2 * ((f * t) % 1.0) - 1
        x = 0.6 * x + 0.4 * np.sin(2 * np.pi * f * t)
    else:
        x = np.sign(np.sin(2 * np.pi * f * t))
    return (amp * x).astype(np.float32)


def lowpass(x, alpha=0.08):
    """One-pole lowpass; cheap and enough to take the edge off a saw."""
    out = np.empty_like(x)
    prev = 0.0
    for i, sample in enumerate(x):
        prev += alpha * (sample - prev)
        out[i] = prev
    return out


def place(track, start, samples):
    i = int(start * SR)
    j = min(len(track), i + len(samples))
    if j > i:
        track[i:j] += samples[:j - i]


def kick(dur=0.24, amp=0.9):
    n = int(dur * SR)
    t = np.arange(n) / SR
    freq = 120 * np.exp(-t * 28) + 42
    x = np.sin(2 * np.pi * np.cumsum(freq) / SR)
    return (amp * x * np.exp(-t * 12)).astype(np.float32)


def snare(dur=0.18, amp=0.45):
    n = int(dur * SR)
    rng = np.random.default_rng(7)
    x = rng.normal(0, 1, n).astype(np.float32)
    x = np.diff(x, prepend=0)          # cheap highpass
    return (amp * x * np.exp(-np.arange(n) / SR * 24)).astype(np.float32)


def hat(dur=0.06, amp=0.18):
    n = int(dur * SR)
    rng = np.random.default_rng(11)
    x = rng.normal(0, 1, n).astype(np.float32)
    x = np.diff(x, prepend=0)
    return (amp * x * np.exp(-np.arange(n) / SR * 70)).astype(np.float32)


def write(path, left, right):
    # Trim the loop edges so -stream_loop has no sample discontinuity. A 25ms
    # dip at -34 dB is inaudible; a click is not.
    fade = min(len(left), len(right), int(0.025 * SR))
    if fade:
        ramp = np.linspace(0, 1, fade, dtype=np.float32)
        left[:fade] *= ramp; right[:fade] *= ramp
        left[-fade:] *= ramp[::-1]; right[-fade:] *= ramp[::-1]
    peak = max(1e-6, float(np.max(np.abs(np.concatenate([left, right])))))
    gain = 0.72 / peak
    data = np.stack([left * gain, right * gain], axis=1)
    data = np.clip(data, -1, 1)
    pcm = (data * 32767).astype("<i2")
    with wave.open(path, "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(SR)
        fh.writeframes(pcm.tobytes())
    print(path, round(len(left) / SR, 2), "s", round(20 * math.log10(gain), 1), "dB gain")


def lofi():
    bpm, beats = 84, 16
    dur = beats * 60 / bpm
    n = int(dur * SR)
    left = np.zeros(n, dtype=np.float32)
    right = np.zeros(n, dtype=np.float32)
    chords = [
        (220.0, [0, 3, 7, 10]),   # Am7
        (174.6, [0, 4, 7, 11]),   # Fmaj7
        (130.8, [0, 4, 7, 11]),   # Cmaj7
        (196.0, [0, 4, 7, 10]),   # G7
    ]
    bar = 4 * 60 / bpm
    for b, (root, intervals) in enumerate(chords):
        start = b * bar
        for k, semi in enumerate(intervals):
            freq = root * (2 ** (semi / 12))
            pad = voice(freq, int(bar * SR), amp=0.16, kind="sine", detune=0.001 * (k + 1))
            pad = lowpass(pad, 0.05) * adsr(len(pad), 0.5, 0.4, 0.7, 0.6)
            place(left, start, pad)
            place(right, start, np.roll(pad, 37) * 0.9)
        bass = voice(root / 2, int(bar * SR), amp=0.20, kind="sine")
        bass *= adsr(len(bass), 0.05, 0.3, 0.5, 0.4)
        place(left, start, bass)
        place(right, start, bass)
    for beat in range(beats):
        t = beat * 60 / bpm
        if beat % 4 in (0, 2):
            k = kick()
            place(left, t, k); place(right, t, k * 0.95)
        if beat % 4 in (1, 3):
            sn = snare()
            place(left, t, sn); place(right, t, np.roll(sn, 19) * 0.8)
        h = hat()
        place(left, t, h); place(right, t, h * 0.8)
        h2 = hat(amp=0.1)
        place(left, t + 0.5 * 60 / bpm, h2)
        place(right, t + 0.5 * 60 / bpm, h2)
    # vinyl-ish low noise
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 0.004, n).astype(np.float32)
    left += noise; right += np.roll(noise, 101)
    write(os.path.join(OUT, "lofi.wav"), left, right)


def pulse():
    bpm, beats = 120, 16
    dur = beats * 60 / bpm
    n = int(dur * SR)
    left = np.zeros(n, dtype=np.float32)
    right = np.zeros(n, dtype=np.float32)
    roots = [110.0, 87.31, 130.81, 98.0]      # A F C G
    bar = 4 * 60 / bpm
    for b, root in enumerate(roots):
        start = b * bar
        for step in range(8):
            freq = root * (2 ** ([0, 7, 12, 7, 3, 10, 12, 7][step] / 12))
            note = voice(freq, int(bar / 8 * SR * 0.9), amp=0.14, kind="saw")
            note = lowpass(note, 0.12) * adsr(len(note), 0.01, 0.08, 0.4, 0.06)
            place(left, start + step * bar / 8, note)
            place(right, start + step * bar / 8, np.roll(note, 23) * 0.85)
        bass = voice(root / 2, int(bar * SR), amp=0.22, kind="sine")
        bass *= adsr(len(bass), 0.01, 0.2, 0.5, 0.2)
        place(left, start, bass); place(right, start, bass)
    for beat in range(beats):
        t = beat * 60 / bpm
        k = kick(amp=0.85)
        place(left, t, k); place(right, t, k)
        h = hat(amp=0.14)
        place(left, t, h); place(right, t, h * 0.9)
        if beat % 2 == 1:
            sn = snare(amp=0.35)
            place(left, t, sn); place(right, t, np.roll(sn, 11) * 0.9)
    write(os.path.join(OUT, "pulse.wav"), left, right)


def cinematic():
    bpm, beats = 60, 12
    dur = beats * 60 / bpm
    n = int(dur * SR)
    left = np.zeros(n, dtype=np.float32)
    right = np.zeros(n, dtype=np.float32)
    chords = [
        (130.81, [0, 3, 7]),      # Cm
        (103.83, [0, 4, 7]),      # Ab
        (155.56, [0, 3, 7]),      # Eb
        (116.54, [0, 4, 7]),      # Bb
    ]
    bar = 3 * 60 / bpm
    for b, (root, intervals) in enumerate(chords):
        start = b * bar
        for k, semi in enumerate(intervals):
            freq = root * (2 ** (semi / 12))
            pad = voice(freq, int(bar * SR), amp=0.18, kind="sine", detune=0.0015 * (k + 1))
            pad = lowpass(pad, 0.03) * adsr(len(pad), 0.8, 0.4, 0.8, 1.0)
            place(left, start, pad)
            place(right, start, np.roll(pad, 53) * 0.9)
        sub = voice(root / 2, int(bar * SR), amp=0.16, kind="sine")
        sub *= adsr(len(sub), 0.3, 0.5, 0.6, 0.8)
        place(left, start, sub); place(right, start, sub)
    for beat in range(0, beats, 2):
        t = beat * 60 / bpm
        k = kick(dur=0.5, amp=0.45)
        place(left, t, k); place(right, t, k)
    write(os.path.join(OUT, "cinematic.wav"), left, right)


if __name__ == "__main__":
    OUT = os.path.join(os.path.dirname(__file__), "..", "assets", "music")
    os.makedirs(OUT, exist_ok=True)
    lofi(); pulse(); cinematic()
