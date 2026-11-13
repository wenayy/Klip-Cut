"""Prosody REACT tag: loud sound with no words right after a line."""
import wave

import numpy as np

from app.pipeline import energy


def _write(path, signal, sr=16000):
    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())


def test_a_laugh_after_a_line_tags_that_line(tmp_path):
    sr, rng = 16000, np.random.default_rng(1)
    utterances, chunks, t = [], [], 0.0
    for i in range(20):
        speech = 0.25 * np.sin(2 * np.pi * 220 * np.arange(2 * sr) / sr)
        words = [{"word": "w", "start": t + k * 0.5, "end": t + k * 0.5 + 0.4}
                 for k in range(4)]
        utterances.append({"start": t, "end": t + 2.0, "words": words,
                           "transcript": "w w w w"})
        gap = (0.3 * rng.standard_normal(int(1.5 * sr)) if i == 7      # laughter
               else 0.003 * rng.standard_normal(int(1.5 * sr)))       # room tone
        chunks += [speech, gap]
        t += 3.5
    path = tmp_path / "talk.wav"
    _write(path, np.concatenate(chunks), sr)

    tagged = energy.analyze(str(path), utterances)
    reacted = [i for i, u in enumerate(tagged) if "REACT" in energy.summarize(u)]
    assert reacted == [7]
