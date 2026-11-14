"""Tightening dead air out of a clip.

Raw footage breathes: someone thinks mid-sentence, reaches for a word, waits for
a laugh. On a long-form timeline that reads as natural. In a 30-second short it
reads as a slow edit, and slow edits lose the viewer -- the clip stops feeling
*made* and starts feeling merely *cut out*.

Detection is done from word timestamps rather than audio silence detection. The
transcript already knows exactly when speech stops and starts, so a gap between
one word ending and the next beginning IS the pause -- no second ffmpeg pass, no
threshold tuning against background noise, and no risk of mistaking a quiet
delivery for silence.

Cutting time out means every caption after the cut is now early, so this module
also returns the words with timings remapped onto the shortened timeline. The
two must be produced together or the captions desync.
"""

# Pauses shorter than this are natural speech rhythm and get left alone.
DEFAULT_MAX_PAUSE = 0.45

# Podcasts need breathing room. A 350 ms gap in a monologue is often emphasis,
# and cutting every one of those gaps creates a machine-gun sequence of jump
# cuts. Only remove clear dead air here; the editor still lets the user choose
# the untouched source timeline per clip.
PODCAST_MAX_PAUSE = 1.50

# Kept either side of a cut so consonants are not clipped and the join does not
# sound abrupt. Speech onsets in particular start slightly before the word's
# nominal timestamp.
HEAD_PAD = 0.08
TAIL_PAD = 0.12

# Below this, a "segment" is an artefact of noisy timings, not a piece of speech.
MIN_SEGMENT = 0.20


def max_pause_for_frame(frame: str = None) -> float:
    """The pacing profile that matches a template's editorial rhythm.

    "auto" is between the two: the planner has not chosen a layout yet when the
    pause budget is decided, and the middle value is right for both conversational
    podcasts and gameplay commentary. Cutting a podcast too tight sounds
    machine-gunned; leaving a gameplay clip loose loses the reaction.
    """
    if frame == "podcast":
        return PODCAST_MAX_PAUSE
    if frame == "auto":
        return (PODCAST_MAX_PAUSE + DEFAULT_MAX_PAUSE) / 2
    return DEFAULT_MAX_PAUSE


# Laughter, cheering and applause have no words, so word gaps alone call them
# dead air. A gap at least this loud relative to the talking is a reaction and
# stays; the clip's end also rings out through one instead of stopping dead.
LAUGH_RATIO = 0.55
RING_OUT_MAX = 3.0
RING_DIP = 0.6
FRAME = 0.1


def _speech_level(rms: list, origin: float, words: list) -> float:
    vals = sorted(rms[int((w["start"] - origin) / FRAME)]
                  for w in words
                  if 0 <= int((w["start"] - origin) / FRAME) < len(rms))
    return vals[len(vals) // 2] if vals else 0.0


def _loud(rms, origin, a, b, level) -> bool:
    i, j = int((a - origin) / FRAME), int((b - origin) / FRAME)
    window = rms[max(0, i):max(0, j)]
    return bool(window) and level > 0 and sum(window) / len(window) >= LAUGH_RATIO * level


def ring_out(rms: list, origin: float, words: list, end: float) -> float:
    """The clip end pushed past the last word while a laugh is still going."""
    level = _speech_level(rms, origin, words)
    if not level:
        return end
    # A laugh often starts a beat after the line, so a short dip is bridged.
    last, t = end, end
    while t - end < RING_OUT_MAX and t - last <= RING_DIP:
        i = int((t - origin) / FRAME)
        if i >= len(rms):
            break
        if rms[i] >= LAUGH_RATIO * level:
            last = t + FRAME
        t += FRAME
    return round(last + (0.3 if last > end else 0.0), 3)


def plan_segments(words: list, max_pause: float = DEFAULT_MAX_PAUSE,
                  rms: list = None, origin: float = 0.0) -> list:
    """Speech spans to keep, as [(start, end)] in SOURCE time.

    Consecutive words separated by more than max_pause start a new segment; the
    dead air between them is what gets dropped.
    """
    if not words:
        return []

    level = _speech_level(rms, origin, words) if rms else 0.0
    segments = []
    seg_start = words[0]["start"] - HEAD_PAD
    prev_end = words[0]["end"]

    for word in words[1:]:
        gap = word["start"] - prev_end
        if gap > max_pause and not (level and _loud(rms, origin, prev_end,
                                                     word["start"], level)):
            segments.append((max(0.0, seg_start), prev_end + TAIL_PAD))
            seg_start = word["start"] - HEAD_PAD
        prev_end = word["end"]

    segments.append((max(0.0, seg_start), prev_end + TAIL_PAD))

    # Drop slivers, and merge neighbours whose padding made them overlap.
    merged = []
    for start, end in segments:
        if end - start < MIN_SEGMENT:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def remap_time(t: float, segments: list) -> float:
    """A source timestamp's position on the shortened timeline.

    The scalar counterpart of `remap_words`, for things that are keyed to time
    but are not words -- reframing keyframes, most of all. Those are built
    against source time while the filter that consumes them runs AFTER the
    tightening concat, so without this they lag by every second removed before
    them and drift further out as the clip goes on.

    A time inside a removed gap collapses to the start of the next kept
    segment, which is where the viewer actually arrives.
    """
    if not segments:
        return t
    elapsed = 0.0
    for start, end in segments:
        if t < start:
            return elapsed
        if t <= end:
            return elapsed + (t - start)
        elapsed += end - start
    return elapsed


def remap_words(words: list, segments: list) -> list:
    """Rewrites word timings onto the shortened timeline.

    A word inside segment i moves back by all the dead air removed before it.
    Words falling in a removed gap are dropped -- by definition nothing was
    spoken there.
    """
    if not segments:
        return list(words)

    # Where each segment begins once earlier gaps are gone.
    offsets = []
    elapsed = 0.0
    for start, end in segments:
        offsets.append(elapsed - start)
        elapsed += end - start

    remapped = []
    for word in words:
        for i, (start, end) in enumerate(segments):
            if start <= word["start"] <= end:
                shift = offsets[i]
                remapped.append({
                    **word,
                    "start": max(0.0, word["start"] + shift),
                    "end": max(0.0, word["end"] + shift),
                })
                break
    return remapped


def removed_seconds(segments: list, clip_start: float, clip_end: float) -> float:
    kept = sum(end - start for start, end in segments)
    return max(0.0, (clip_end - clip_start) - kept)


def tighten(words: list, clip_start: float, clip_end: float,
            max_pause: float = DEFAULT_MAX_PAUSE, rms: list = None):
    """Returns (segments, remapped_words, seconds_removed).

    Segments are clamped to the clip's own range so a shared transcript cannot
    pull in audio from outside the clip.
    """
    if not words:
        return [], [], 0.0

    segments = [
        (max(start, clip_start), min(end, clip_end))
        for start, end in plan_segments(words, max_pause, rms, clip_start)
    ]
    # Keep the ring-out after the last word: the clip end may sit past it.
    if segments and rms and clip_end - segments[-1][1] <= RING_OUT_MAX + 0.3:
        segments[-1] = (segments[-1][0], clip_end)
    segments = [(s, e) for s, e in segments if e - s >= MIN_SEGMENT]
    if not segments:
        return [], list(words), 0.0

    return segments, remap_words(words, segments), removed_seconds(segments, clip_start, clip_end)
