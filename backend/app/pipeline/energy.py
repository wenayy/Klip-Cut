"""Prosody signal: how something is SAID, not just what is said.

A transcript-only selector is deaf. It cannot tell a mumbled aside from a
shouted reveal, and in short-form that difference is most of what decides
whether a clip works. Laughter, shouting, a sudden hush before a punchline, a
sped-up rant -- none of it appears in text, and all of it drives retention.

So we measure it directly from the audio we already downloaded, with no extra
API cost and no added latency worth mentioning:

  loudness     RMS per utterance, normalised against the whole video
  dynamics     how much loudness swings inside the utterance (flat = monotone)
  pace         words per second, normalised (fast = excited, slow = weighty)
  pause_before silence in front of the line (a setup beat)
  reaction     sound with NO words right after the line -- laughter, cheering,
               a shout. Transcription drops non-speech, so a gap that is as
               loud as talking is the room reacting. A clip followed by
               laughter is the classic label for a funny moment.

These become annotations on the transcript the model reads, so it can see where
the room actually got loud.
"""

# import audioop
import numpy as np
import subprocess

SAMPLE_RATE = 8000            # plenty for loudness; keeps the buffer small
SAMPLE_WIDTH = 2              # s16le
FRAME_MS = 100                # loudness resolution


def _decode_pcm(audio_path: str) -> bytes:
    """Decodes to 8kHz mono signed-16 PCM. Returns b'' if ffmpeg fails."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", audio_path,
             "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
            capture_output=True, timeout=300,
        )
        return out.stdout if out.returncode == 0 else b""
    except (subprocess.SubprocessError, OSError):
        return b""


# def _frame_rms(pcm: bytes) -> list:
#     """RMS per FRAME_MS window across the whole file."""
#     frame_bytes = int(SAMPLE_RATE * SAMPLE_WIDTH * FRAME_MS / 1000)
#     return [
#         audioop.rms(pcm[i:i + frame_bytes], SAMPLE_WIDTH)
#         for i in range(0, len(pcm) - frame_bytes, frame_bytes)
#     ]
def _frame_rms(pcm: bytes) -> list[float]:
    """RMS per FRAME_MS window across the whole file.

    Equivalent to audioop.rms(), but implemented with NumPy so it works on
    Python 3.13+ where audioop has been removed.
    """
    frame_bytes = int(SAMPLE_RATE * SAMPLE_WIDTH * FRAME_MS / 1000)

    if not pcm:
        return []

    # Interpret the raw PCM bytes as signed 16-bit little-endian samples.
    samples = np.frombuffer(pcm, dtype="<i2")

    samples_per_frame = frame_bytes // SAMPLE_WIDTH

    if samples_per_frame == 0:
        return []

    frame_count = len(samples) // samples_per_frame

    if frame_count == 0:
        return []

    # Ignore the tiny leftover tail that doesn't make a full frame.
    samples = samples[: frame_count * samples_per_frame]

    # Shape:
    # (number_of_frames, samples_per_frame)
    frames = samples.reshape(frame_count, samples_per_frame)

    # Convert once to float64 to avoid integer overflow during squaring.
    frames = frames.astype(np.float64)

    # RMS = sqrt(mean(x²))
    rms = np.sqrt(np.mean(frames * frames, axis=1))

    return rms.tolist()


REACT_MIN_GAP = 0.4        # seconds without words that can hold a laugh
REACT_MAX_GAP = 3.0        # only the moment right after the line counts
REACT_SPEECH_RATIO = 0.8   # the gap must be nearly as loud as the talking


def _mean_rms(frames: list, start: float, end: float) -> float:
    first = max(0, int(start * 1000 / FRAME_MS))
    last = min(int(end * 1000 / FRAME_MS), len(frames))
    window = [f for f in frames[first:last] if f > 0]
    return sum(window) / len(window) if window else 0.0


def _reaction_gaps(utterances: list, idx: int) -> list:
    """(start, end) spans with no transcribed words inside or right after a line."""
    utt = utterances[idx]
    words = utt.get("words") or []
    gaps = []
    for w0, w1 in zip(words, words[1:]):
        if w1["start"] - w0["end"] >= REACT_MIN_GAP:
            gaps.append((w0["end"], min(w1["start"], w0["end"] + REACT_MAX_GAP)))
    following = (utterances[idx + 1]["start"] if idx + 1 < len(utterances)
                 else utt["end"] + REACT_MAX_GAP)
    if following - utt["end"] >= REACT_MIN_GAP:
        gaps.append((utt["end"], min(following, utt["end"] + REACT_MAX_GAP)))
    return gaps


def _percentile(sorted_vals: list, pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(len(sorted_vals) * pct)))
    return float(sorted_vals[idx])


def analyze(audio_path: str, utterances: list) -> list:
    """Attaches loudness/dynamics/pace/pause_before (0-10) to each utterance.

    Degrades to a no-op on any failure: prosody is a bonus signal, never a
    reason to fail a job.
    """
    pcm = _decode_pcm(audio_path)
    frames = _frame_rms(pcm) if pcm else []

    if frames:
        ranked = sorted(f for f in frames if f > 0)
        # Percentiles, not max: one door slam should not flatten the whole scale.
        floor = _percentile(ranked, 0.10) or 1.0
        ceiling = _percentile(ranked, 0.95) or (floor * 2)
        span = max(ceiling - floor, 1.0)

    rates = []
    for utt in utterances:
        span_s = max(utt["end"] - utt["start"], 0.1)
        rates.append(len(utt.get("words") or []) / span_s)
    ranked_rates = sorted(r for r in rates if r > 0)
    rate_lo = _percentile(ranked_rates, 0.10) or 1.0
    rate_hi = _percentile(ranked_rates, 0.90) or (rate_lo * 2)
    rate_span = max(rate_hi - rate_lo, 0.1)

    # Raw measurements first; they get converted to percentile ranks below.
    raw_loud, raw_dyn = [], []
    for utt in utterances:
        if frames:
            first = int(utt["start"] * 1000 / FRAME_MS)
            last = min(int(utt["end"] * 1000 / FRAME_MS), len(frames))
            window = [f for f in frames[first:last] if f > 0]
        else:
            window = []
        if window:
            ordered = sorted(window)
            raw_loud.append(sum(window) / len(window))
            raw_dyn.append(_percentile(ordered, 0.95) - _percentile(ordered, 0.15))
        else:
            raw_loud.append(0.0)
            raw_dyn.append(0.0)

    # Percentile rank, not absolute scale. Absolute scaling saturates -- ordinary
    # speech already spans near-silence to peak inside a single line, so almost
    # everything scored "dynamic". Ranking each line against the rest of THIS
    # video keeps the tags rare and therefore meaningful, and it self-calibrates
    # across quiet podcasts and shouty gaming videos alike.
    loud_rank = _ranks(raw_loud)
    dyn_rank = _ranks(raw_dyn)
    pace_rank = _ranks(rates)

    pauses = []
    previous_end = 0.0
    for utt in utterances:
        pauses.append(max(0.0, utt["start"] - previous_end))
        previous_end = utt["end"]
    pause_rank = _ranks(pauses)

    # Reaction: the loudest word-free gap inside or right after each line. Only
    # gaps about as loud as the talking count -- an ordinary pause is room
    # tone, a laugh or a cheer is not.
    raw_react = []
    for idx in range(len(utterances)):
        level = 0.0
        if frames:
            level = max((_mean_rms(frames, a, b)
                         for a, b in _reaction_gaps(utterances, idx)), default=0.0)
        raw_react.append(level)
    spoken = sorted(v for v in raw_loud if v > 0)
    speech_level = _percentile(spoken, 0.5) if spoken else 0.0
    react_rank = _ranks(raw_react)

    annotated = []
    for idx, utt in enumerate(utterances):
        info = dict(utt)
        info["loudness"] = loud_rank[idx]
        info["dynamics"] = dyn_rank[idx]
        info["pace"] = pace_rank[idx]
        info["pause_before"] = round(pauses[idx], 2)
        info["pause_rank"] = pause_rank[idx]
        info["reaction"] = (react_rank[idx]
                            if speech_level and raw_react[idx]
                            >= speech_level * REACT_SPEECH_RATIO else 0.0)
        annotated.append(info)

    return annotated


def _ranks(values: list) -> list:
    """Maps each value to its percentile rank within the list, as 0-10."""
    if not values:
        return []
    ordered = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    for position, original_index in enumerate(ordered):
        out[original_index] = round(position / max(len(values) - 1, 1) * 10.0, 1)
    return out


def summarize(utt: dict) -> str:
    """Compact tag for the prompt, e.g. '[LOUD dynamic fast pause:1.4s]'.

    Only notable values are mentioned -- tagging every line would be noise the
    model learns to ignore.
    """
    # Thresholds are percentile ranks, so these tag roughly the top/bottom decile
    # of THIS video. Loosening them makes nearly every line carry a tag, which
    # the model then correctly learns to ignore.
    parts = []
    loud = utt.get("loudness", 5.0)
    if loud >= 9.5:
        parts.append("LOUD")
    elif loud <= 0.5:
        parts.append("quiet")
    if utt.get("dynamics", 5.0) >= 9.5:
        parts.append("dynamic")

    # Words-per-second is meaningless on a two-word transcription fragment, and
    # tagging those as "slow" was the single biggest source of false signal.
    if len(utt.get("words") or []) >= 5:
        pace = utt.get("pace", 5.0)
        if pace >= 9.5:
            parts.append("fast")
        elif pace <= 0.5:
            parts.append("slow")
    # Ranked, not absolute: conversational gaps differ hugely between a tight
    # edit and a rambling stream, and only an unusual pause is a dramatic beat.
    if utt.get("pause_rank", 0) >= 9.5 and utt.get("pause_before", 0) >= 1.0:
        parts.append(f"pause:{utt['pause_before']:.1f}s")
    if utt.get("reaction", 0) >= 9.0:
        parts.append("REACT")
    return f"[{' '.join(parts)}] " if parts else ""
