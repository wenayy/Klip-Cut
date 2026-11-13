"""Highlight video: the best parts of a long video, cut into one.

Clips and a highlight video are different products. Clips are short,
standalone posts, mostly vertical. A highlight video is ONE longer edit --
around 8-15 minutes for an hour or more of source -- of everything someone who
missed the stream or the episode should see, cut the way highlight editors cut
them:

    selection  scored moments from across the WHOLE video, filled to a time
               budget; every stretch of the video gets its best moment before
               the strongest stretch gets a second one
    teaser     the strongest moment's peak opens the video as a cold open
    order      after the teaser, everything plays in the order it happened
    joins      short, gentle transitions with a soft whoosh under each
    tags       "12 MIN LATER" where the video skips a long stretch -- the
               editor's context card, instead of minutes of setup
    memes      optional reaction beats after the biggest moments: a freeze
               with a punch-in, a reaction caption, an emoji and a boom

Everything in a meme beat is original (Twemoji is CC-BY, the boom comes from
tools/make_sfx.py). No downloaded meme clips: those are someone else's work.

The planning functions are pure and unit-tested; only `build` runs ffmpeg.
"""

import math
import os
import subprocess

from app.pipeline import content, emoji_overlay, render

HIGHLIGHT_FILE = "highlight.mp4"
WORK_PREFIX = "_hl_"             # removed by workspace.clear_intermediates

# --- length ------------------------------------------------------------------
MIN_SOURCE_SECONDS = 8 * 60      # below this there is nothing to condense
TARGET_SHARE = 0.15              # "auto": about 9 minutes from an hour
MIN_TARGET = 3 * 60
MAX_TARGET = 15 * 60
LENGTH_CHOICES = (5, 10, 15)     # minutes offered besides auto

# --- selection ---------------------------------------------------------------
SEGMENT_MIN = 10.0               # YouTube ignores chapters under 10 s
SEGMENT_MAX = 180.0
SEGMENT_GAP = 2.0                # picks closer than this would play as one
BUCKET_SECONDS = 600             # one stretch of the video per ten minutes
COVERAGE_PERCENTILE = 0.3        # a stretch's best below this is skipped

# --- assembly ----------------------------------------------------------------
TEASER_SECONDS = 7.0
FADE = 0.5                       # between two segments
MEME_FADE = 0.12                 # white flash into, quick fade out of, a meme
MEME_SECONDS = 1.4
MAX_MEMES = 4
MEME_EVERY = 3                   # at most one meme per this many segments
LATER_MIN_SECONDS = 300          # a skip this long is worth a tag
FPS = 30
WHOOSH_GAIN = 0.25
TITLE_SHARE = 0.075              # title card height, of the frame
DRIFT = 0.05                     # slow push in/out across a part (5%)
PUNCH = 0.12                     # punch-in on a part's reaction peak
PUNCH_RATIO = 2.0                # peak loudness over the part's mean that earns one
SECTION_SECONDS = 3.0            # how long a moment's lower-third title stays
SNAP_GAP = 0.25                  # a pause this long is a safe place to cut
TAIL_PAD = 0.12

# Long-form transitions: quick and quiet. The flashy set suits a 50 s reel; in
# a 12-minute video a wipe every minute reads as a gimmick.
TRANSITIONS = ("fade", "smoothleft", "fadeblack", "smoothright", "circleopen",
               "slideup")

REACTIONS = {
    "gaming": [("NO WAY", "😱"), ("CLUTCH", "🔥"), ("BRUH", "💀"),
               ("WHAT?!", "🤯"), ("GG", "🏆")],
    "podcast": [("WAIT WHAT", "🤯"), ("FACTS", "💯"), ("SAY THAT AGAIN", "👀"),
                ("BRUH", "💀"), ("NO WAY", "😱")],
    "general": [("NO WAY", "😱"), ("WAIT WHAT", "🤯"), ("BRUH", "💀"),
                ("FACTS", "💯"), ("LMAO", "😂")],
}


# ---------------------------------------------------------------------------
# Planning (pure)
# ---------------------------------------------------------------------------

def target_seconds(source_seconds: float, minutes: float = None) -> float:
    """How long the highlight video should run."""
    if minutes:
        return float(min(minutes * 60, source_seconds * 0.6))
    return float(max(MIN_TARGET, min(MAX_TARGET, source_seconds * TARGET_SHARE)))


def select_segments(pool: list, target: float, duration: float) -> list:
    """The moments to keep: about `target` seconds, in story order.

    `pool` entries carry "start", "end" (seconds) and "score". Every stretch of
    the video first gets its best moment, unless that moment is among the
    weakest found -- a highlight of a three-hour stream that only covers its
    loudest twenty minutes has lost the story, but a break or an AFK stretch
    should not be forced in either. The rest of the budget then goes strictly
    by score.
    """
    def length(c):
        return c["end"] - c["start"]

    usable = [c for c in pool if SEGMENT_MIN <= length(c) <= SEGMENT_MAX]
    if not usable:
        return []
    ranked = sorted(usable, key=lambda c: c["score"], reverse=True)
    floor = sorted(c["score"] for c in usable)[int(len(usable) * COVERAGE_PERCENTILE)]
    chosen, total = [], 0.0

    def fits(c):
        return all(c["end"] + SEGMENT_GAP <= o["start"] or c["start"] >= o["end"] + SEGMENT_GAP
                   for o in chosen)

    buckets = max(3, min(12, round(duration / BUCKET_SECONDS)))
    width = duration / buckets
    for b in range(buckets):
        lo, hi = b * width, (b + 1) * width
        for c in ranked:
            if lo <= c["start"] < hi and c["score"] >= floor and fits(c) \
                    and total + length(c) <= target * 1.1:
                chosen.append(c)
                total += length(c)
                break
    for c in ranked:
        if total >= target * 0.95:
            break
        if any(c is o for o in chosen) or not fits(c):
            continue
        if total + length(c) > target * 1.15 and total >= target * 0.7:
            continue
        chosen.append(c)
        total += length(c)
    return sorted(chosen, key=lambda c: c["start"])


def meme_slots(scores: list, limit: int = MAX_MEMES) -> set:
    """Indices of the segments a meme beat follows: the biggest moments.

    Never after the last segment (a highlight ends on content, not on a gag),
    never two in a row, and no more than one per MEME_EVERY segments.
    """
    n = len(scores)
    allowed = min(limit, n // MEME_EVERY)
    picked = set()
    for i in sorted(range(n - 1), key=lambda i: scores[i], reverse=True):
        if len(picked) >= allowed:
            break
        if i - 1 in picked or i + 1 in picked:
            continue
        picked.add(i)
    return picked


def timeline(parts: list, genre: str = "general", memes: bool = True) -> list:
    """Ordered segments with the transition INTO each one.

    `parts` are {"kind": "teaser"|"segment", "duration", ...}. Each returned
    entry gains "transition", "fade" and "offset" -- where its xfade starts on
    the output timeline (None for the first). Meme entries carry "text",
    "emoji" and "after" (the part index whose last frame they freeze).
    """
    reactions = REACTIONS.get(genre) or REACTIONS["general"]
    segments = [p for p in parts if p["kind"] == "segment"]
    slots = meme_slots([s.get("score", 0) for s in segments]) if memes else set()
    out, seen, used = [], -1, 0
    for i, part in enumerate(parts):
        out.append(dict(part))
        if part["kind"] == "segment":
            seen += 1
            if seen in slots:
                text, emoji = reactions[used % len(reactions)]
                out.append({"kind": "meme", "text": text, "emoji": emoji,
                            "after": i, "duration": MEME_SECONDS})
                used += 1

    length, rotation = 0.0, 0
    for k, seg in enumerate(out):
        if k == 0:
            seg.update(transition=None, fade=0.0, offset=None)
            length = seg["duration"]
            continue
        prev = out[k - 1]
        if seg["kind"] == "meme":
            transition, fade = "fadewhite", MEME_FADE
        elif prev["kind"] == "meme":
            transition, fade = "fade", MEME_FADE
        elif prev["kind"] == "teaser":
            transition, fade = "fadeblack", FADE         # a breath after the cold open
        else:
            transition, fade = TRANSITIONS[rotation % len(TRANSITIONS)], FADE
            rotation += 1
        fade = min(fade, seg["duration"] / 3, prev["duration"] / 3)
        seg.update(transition=transition, fade=round(fade, 3),
                   offset=round(length - fade, 3))
        length += seg["duration"] - fade
    return out


def total_duration(segments: list) -> float:
    return round(sum(s["duration"] for s in segments)
                 - sum(s["fade"] for s in segments), 3)


def context_tags(segments: list) -> dict:
    """{segment index: "N MIN LATER"} where the video skips a long stretch."""
    tags, previous = {}, None
    for k, seg in enumerate(segments):
        if seg["kind"] != "segment":
            continue
        if previous is not None:
            jump = seg["source_start"] - previous
            if jump >= LATER_MIN_SECONDS:
                minutes = int(round(jump / 60))
                hours, minutes = divmod(minutes, 60)
                tags[k] = (f"{hours} HR {minutes} MIN LATER" if hours and minutes
                           else f"{hours} HR LATER" if hours
                           else f"{minutes} MIN LATER")
        previous = seg["source_end"]
    return tags


def chapters(segments: list) -> list:
    """Where each moment starts in the finished video, for the results page."""
    out = []
    for seg in segments:
        if seg["kind"] != "segment":
            continue
        at = 0.0 if seg["offset"] is None else seg["offset"] + seg["fade"] / 2
        out.append({"title": seg.get("title") or "", "at": round(at, 1),
                    "source_start": round(seg["source_start"], 1),
                    "source_end": round(seg["source_end"], 1)})
    return out


def _cut_points(words: list, duration: float, length: float) -> list:
    if not words:
        latest = max(0.0, duration - length)
        return [i * 0.5 for i in range(int(latest / 0.5) + 1)]
    points = [0.0]
    for prev, word in zip(words, words[1:]):
        text = (prev.get("punctuated_word") or prev.get("word") or "").strip()
        if word["start"] - prev["end"] >= SNAP_GAP or text.endswith((".", "?", "!")):
            points.append(float(word["start"]))
    latest = max(0.0, duration - length * 0.75)
    return sorted({round(p, 3) for p in points if 0.0 <= p <= latest}) or [0.0]


def teaser_window(words: list, duration: float, energy: list = None,
                  length: float = TEASER_SECONDS,
                  hop: float = content.HIGHLIGHT_HOP) -> tuple:
    """(start, end) of the few seconds that open the video.

    The loudest stretch of the strongest moment, starting at a word boundary,
    with its peak placed after the start rather than on it -- a cold open
    needs a beat of setup to land.
    """
    if duration <= length + 0.5:
        return 0.0, round(duration, 3)
    best, best_score = (0.0, min(duration, length)), None
    for start in _cut_points(words, duration, length):
        end = min(duration, start + length)
        inside = [w for w in words if w["start"] >= start and w["end"] <= end + 0.15]
        if inside and inside[-1]["end"] - start >= length * 0.75:
            end = min(duration, inside[-1]["end"] + TAIL_PAD)
        lo, hi = int(start / hop), max(int(start / hop) + 1, int(end / hop))
        window = (energy or [])[lo:hi]
        if window:
            peak = window.index(max(window)) / max(1, len(window) - 1)
            score = sum(window) / len(window) * (1.0 if 0.3 <= peak <= 0.9 else 0.85)
        else:
            score = sum(1 for w in words if start <= w["start"] < end) / max(end - start, 1e-3)
        score -= 1e-6 * start
        if best_score is None or score > best_score:
            best, best_score = (start, end), score
    return round(best[0], 3), round(best[1], 3)


def title_size(text: str, width: int, height: int, share: float = 0.042) -> int:
    """Largest font size that keeps one line of `text` inside 88% of the width."""
    fit = int(width * 0.88 / (0.58 * max(1, len(text))))
    return max(24, min(int(height * share), fit))


def join_graph(segments: list, audio_inputs: dict, width: int, height: int,
               whoosh_input: int = None, font: str = None,
               title_file: str = None, title_at: float = 0.0, title_px: int = 0,
               tag_files: dict = None, section_files: dict = None,
               eyebrow_file: str = None, music_input: int = None,
               music_gain: float = 0.1) -> str:
    """filter_complex joining inputs 0..n-1 (one per segment) into [vout][aout].

    Every input is normalised first -- frame rate, size, timebase, audio format
    -- because xfade refuses inputs that differ, and a teaser, rendered
    segments and generated meme beats come from three different encoders.
    `audio_inputs` maps a segment to the input holding its sound (a silent
    segment borrows a generated silence).
    """
    parts = []
    for k, seg in enumerate(segments):
        # Both tracks of every part are pinned to the SAME length, from zero.
        # An encoded part's audio is never exactly its video's length (AAC
        # priming and padding, frame-rounded cuts), and the crossfades are
        # planned on one duration: each mismatch shifted every later part's
        # sound against its picture -- ~40 ms per join, half a second of lip
        # lag by the end of a 12-minute highlight.
        d = f"{seg['duration']:.3f}"
        base = (f"[{k}:v]trim=0:{d},setpts=PTS-STARTPTS,fps={FPS},"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        pip = seg.get("pip")
        if pip:
            # Watch-along: the stream's tiny webcam, cropped before the
            # camera moves and laid back on top enlarged and framed, so the
            # reaction is as readable as what is being reacted to.
            cx, cy, cw, ch = pip["crop"]
            bx, by, bw, bh = pip["box"]
            parts.append(f"{base},split=2[b{k}][f{k}]")
            parts.append(f"[f{k}]crop={cw}:{ch}:{cx}:{cy},scale={bw - 8}:{bh - 8},"
                         f"pad={bw}:{bh}:4:4:color=0x8B5CF6[p{k}]")
            zoom = ""
            if pip.get("zoom"):
                zx, zy, zw, zh = pip["zoom"]
                zoom = f"crop={zw}:{zh}:{zx}:{zy},scale={width}:{height},"
            parts.append(f"[b{k}]{zoom}{_motion(seg, width, height)}null[m{k}]")
            parts.append(f"[m{k}][p{k}]overlay=x={bx}:y={by},"
                         f"format=yuv420p,settb=AVTB[v{k}n]")
        else:
            parts.append(f"{base},{_motion(seg, width, height)}"
                         f"format=yuv420p,settb=AVTB[v{k}n]")
        parts.append(f"[{audio_inputs.get(k, k)}:a]aresample=48000:async=1:first_pts=0,"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"apad,atrim=0:{d},asetpts=PTS-STARTPTS[a{k}n]")
    v, a = "[v0n]", "[a0n]"
    whooshes = []
    for k, seg in enumerate(segments[1:], start=1):
        parts.append(f"{v}[v{k}n]xfade=transition={seg['transition']}"
                     f":duration={seg['fade']}:offset={seg['offset']}[v{k}]")
        parts.append(f"{a}[a{k}n]acrossfade=d={seg['fade']}:c1=tri:c2=tri[a{k}]")
        v, a = f"[v{k}]", f"[a{k}]"
        if seg["kind"] == "segment" and segments[k - 1]["kind"] == "segment":
            whooshes.append(max(0.0, seg["offset"] - 0.15))

    if whoosh_input is not None and whooshes:
        labels = "".join(f"[w{i}]" for i in range(len(whooshes)))
        split = (f",asplit={len(whooshes)}{labels}" if len(whooshes) > 1 else "[w0]")
        parts.append(f"[{whoosh_input}:a]aresample=48000,"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"volume={WHOOSH_GAIN}{split}")
        for i, at in enumerate(whooshes):
            ms = int(at * 1000)
            parts.append(f"[w{i}]adelay={ms}|{ms}[wd{i}]")
        mix = "".join(f"[wd{i}]" for i in range(len(whooshes)))
        parts.append(f"{a}{mix}amix=inputs={len(whooshes) + 1}"
                     f":duration=first:normalize=0[amixed]")
        a = "[amixed]"

    total = total_duration(segments)
    if music_input is not None:
        # A bed under the whole video, ducked whenever someone talks -- the
        # same sidechain the clips use, so speech always sits on top.
        parts.append(f"[{music_input}:a]aresample=48000,"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"volume={music_gain:.3f},atrim=0:{total:.3f},"
                     f"afade=t=in:st=0:d=1.5,afade=t=out:st={max(0.0, total - 2.5):.3f}:d=2.5[mus]")
        parts.append(f"{a}asplit=2[sp][sc]")
        parts.append("[mus][sc]sidechaincompress=threshold=0.04:ratio=10:attack=8:release=320[duck]")
        parts.append("[sp][duck]amix=inputs=2:duration=first:normalize=0[withmus]")
        a = "[withmus]"
    parts.append(f"{a}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
                 f"afade=t=in:st=0:d=0.04,"
                 f"afade=t=out:st={max(0.0, total - 0.4):.3f}:d=0.4[aout]")

    draws = []
    if font and title_file:
        draws.append(_drawtext(font, title_file, title_px or title_size("", width, height),
                               title_at + 0.2, title_at + 3.4, "h*0.10", box=0.45))
        if eyebrow_file:
            draws.append(_drawtext(font, eyebrow_file, max(20, int(height * 0.028)),
                                   title_at + 0.2, title_at + 3.4, "h*0.045", box=0.0,
                                   colour="0x8B5CF6"))
    if font:
        for k, path in sorted((section_files or {}).items()):
            t0 = (segments[k]["offset"] or 0.0) + segments[k]["fade"] + 0.4
            if k == next((i for i, s in enumerate(segments) if s["kind"] == "segment"), -1):
                t0 += 3.6            # the first part already carries the title card
            draws += _lower_third(font, path, max(26, int(height * 0.038)),
                                  t0, t0 + SECTION_SECONDS, width, height)
    if font:
        tag_px = max(22, int(height * 0.03))
        for k, path in sorted((tag_files or {}).items()):
            t0 = (segments[k]["offset"] or 0.0) + segments[k]["fade"]
            draws.append(_drawtext(font, path, tag_px, t0 + 0.1, t0 + 2.2,
                                   "h*0.06", box=0.55, x="w-text_w-w*0.04"))
    parts.append(f"{v}{','.join(draws)}[vout]" if draws else f"{v}null[vout]")
    return ";".join(parts)


def _drawtext(font: str, text_file: str, size: int, t0: float, t1: float,
              y: str, box: float, colour: str = "white",
              x: str = "(w-text_w)/2") -> str:
    """A centred caption that fades in and out over [t0, t1] of the video."""
    alpha = (f"if(lt(t,{t0 + 0.2:.2f}),(t-{t0:.2f})/0.2,"
             f"if(gt(t,{t1 - 0.2:.2f}),({t1:.2f}-t)/0.2,1))")
    return (f"drawtext=fontfile='{render._ff_escape(font)}'"
            f":textfile='{render._ff_escape(text_file)}'"
            f":fontsize={size}:fontcolor={colour}:borderw={max(3, size // 12)}"
            f":bordercolor=black:box={1 if box else 0}:boxcolor=black@{box}:boxborderw={size // 3}"
            f":x={x}:y={y}"
            f":enable='between(t,{t0:.2f},{t1:.2f})':alpha='{alpha}'")


PIP_WIDTH = 0.30                 # an enlarged facecam, as a share of the frame width
PIP_MARGIN = 0.03
PIP_CAPTION_CLEAR = 0.16         # a bottom-corner cam sits above the caption line
CONTENT_ZOOM = 1.3               # push into the video being watched


def pip_for(facecam: dict, src_w: int, src_h: int, out_w: int, out_h: int):
    """Where a watch-along's tiny webcam goes when an editor enlarges it.

    `facecam` is content.find_facecam's {"centre", "win"} in source pixels.
    Returns {"crop": (x, y, w, h) in OUTPUT pixels of the framed part, "box":
    (x, y, w, h) of the enlarged picture-in-picture}, or None when there is no
    usable cam. The cam goes to the corner it already lives in, so the layout
    stays the streamer's own -- just readable.
    """
    if not facecam or not src_w or not src_h:
        return None
    scale = min(out_w / src_w, out_h / src_h)
    ox, oy = (out_w - src_w * scale) / 2, (out_h - src_h * scale) / 2
    (cx, cy), (fw, fh) = facecam["centre"], facecam["win"]
    # A little room around the overlay's own window: head and shoulders.
    w, h = fw * scale, fh * scale
    x = max(0.0, min(out_w - w, ox + cx * scale - w / 2))
    y = max(0.0, min(out_h - h, oy + cy * scale - h / 2))
    if w < 40 or h < 40 or w * h > 0.2 * out_w * out_h:
        return None                         # not a small overlay: nothing to enlarge
    pw = out_w * PIP_WIDTH
    ph = pw * h / w
    right = x + w / 2 > out_w / 2
    bottom = y + h / 2 > out_h / 2
    px = out_w - pw - out_w * PIP_MARGIN if right else out_w * PIP_MARGIN
    py = (out_h - ph - out_h * PIP_CAPTION_CLEAR) if bottom else out_h * PIP_MARGIN
    even = lambda v: int(v) // 2 * 2
    out = {"crop": (even(x), even(y), even(w), even(h)),
           "box": (even(px), even(py), even(pw), even(ph))}
    if facecam.get("focus"):
        # The window an editor would push into, centred on the watched
        # video's faces and kept inside the frame.
        zw, zh = out_w / CONTENT_ZOOM, out_h / CONTENT_ZOOM
        fx, fy = ox + facecam["focus"][0] * scale, oy + facecam["focus"][1] * scale
        out["zoom"] = (even(max(0, min(out_w - zw, fx - zw / 2))),
                       even(max(0, min(out_h - zh, fy - zh / 2))), even(zw), even(zh))
    return out


def _motion(seg: dict, width: int, height: int) -> str:
    """Filter text moving the framing of one part, or "" for a still part.

    A slow push in (or out, alternating part by part) keeps a long talking
    shot alive, and a quick punch-in lands on the part's reaction peak -- the
    two moves highlight editors use most. Both are one smooth scale curve over
    the part's own clock, cropped back to the frame.
    """
    direction = seg.get("motion")
    if not direction:
        return ""
    d = max(seg["duration"], 0.1)
    z = (f"1+{DRIFT}*t/{d:.3f}" if direction > 0 else f"1+{DRIFT}*(1-t/{d:.3f})")
    if seg.get("punch") is not None:
        z += f"+{PUNCH}*exp(-pow((t-{seg['punch']:.2f})/0.3,2))"
    return (f"scale=w='2*trunc(iw*({z})/2)':h='2*trunc(ih*({z})/2)':eval=frame,"
            f"crop={width}:{height},")


def _lower_third(font: str, text_file: str, size: int, t0: float, t1: float,
                 width: int, height: int) -> list:
    """A moment's title: a violet bar and a dark plate sliding in, bottom left."""
    # Top left: the bottom of the frame belongs to the captions.
    x0, y = int(width * 0.04), int(height * 0.06)
    on = f"between(t,{t0:.2f},{t1:.2f})"
    slide = f"{x0 + size}-60*(1-min(1,max(0,(t-{t0:.2f})/0.35)))"
    alpha = (f"if(lt(t,{t0 + 0.25:.2f}),(t-{t0:.2f})/0.25,"
             f"if(gt(t,{t1 - 0.25:.2f}),({t1:.2f}-t)/0.25,1))")
    return [
        f"drawbox=x={x0}:y={y - size // 3}:w={max(6, size // 6)}:h={int(size * 1.66)}"
        f":color=0x8B5CF6@1:t=fill:enable='{on}'",
        f"drawtext=fontfile='{render._ff_escape(font)}':textfile='{render._ff_escape(text_file)}'"
        f":fontsize={size}:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw={size // 3}"
        f":x='{slide}':y={y}:enable='{on}':alpha='{alpha}'",
    ]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _run(what: str, cmd: list) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(render.ffmpeg_failure(what, cmd, proc))


_INTERMEDIATE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                 "-ar", "48000", "-ac", "2"]


def _cut(path: str, start: float, end: float, out: str) -> None:
    """A few seconds of a rendered segment, for the cold open."""
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", path]
    if render.has_audio(path):
        audio = ["-map", "0:a:0"]
    else:
        cmd += ["-f", "lavfi", "-t", f"{end - start:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        audio = ["-map", "1:a:0"]
    cmd += ["-map", "0:v:0", *audio, "-threads", str(render.FFMPEG_THREADS),
            *_INTERMEDIATE, out]
    _run("highlight teaser", cmd)


def _meme(source: str, out: str, width: int, height: int, text: str,
          emoji: str, font: str, boom: str) -> None:
    """Freeze a segment's last frame and turn it into a reaction beat."""
    frame = out + ".png"
    _run("highlight freeze frame", ["ffmpeg", "-y", "-sseof", "-0.2", "-i", source,
                                    "-frames:v", "1", frame])
    frames = int(MEME_SECONDS * FPS)
    shake = f"{max(6, height // 60)}*exp(-on/8)"
    graph = (
        f"[0:v]scale={width * 2}:{height * 2},"
        f"zoompan=z='min(1+0.05*on,1.3)'"
        f":x='iw/2-(iw/zoom/2)+{shake}*sin(on*2.1)'"
        f":y='ih/2-(ih/zoom/2)+{shake}*cos(on*2.7)'"
        f":d=1:s={width}x{height}:fps={FPS},"
        f"eq=contrast=1.2:saturation=1.5,vignette=PI/5"
    )
    inputs = ["-loop", "1", "-framerate", str(FPS), "-t", f"{MEME_SECONDS}", "-i", frame]
    size = title_size(text, width, height, share=0.075)
    if font:
        text_file = os.path.splitext(out)[0] + ".txt"
        with open(text_file, "w", encoding="utf-8") as handle:
            handle.write(text)
        graph += (f",drawtext=fontfile='{render._ff_escape(font)}'"
                  f":textfile='{render._ff_escape(text_file)}'"
                  f":fontsize={size}:fontcolor=white:borderw={max(4, size // 10)}"
                  f":bordercolor=black:x=(w-text_w)/2:y=h*0.30")
    graph += ",format=yuv420p[base]"

    video, next_input = "[base]", 1
    sticker = emoji_overlay.asset_path(emoji)
    if sticker:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{MEME_SECONDS}",
                   "-i", sticker]
        side = int(min(width, height) * 0.26)
        graph += (f";[{next_input}:v]scale={side}:{side},format=rgba[emo]"
                  f";[base][emo]overlay=x=(W-w)/2:y=H*0.30+{int(size * 1.5)}"
                  f":enable='gte(t,0.08)'[vid]")
        video = "[vid]"
        next_input += 1
    if boom:
        inputs += ["-i", boom]
        graph += (f";[{next_input}:a]aresample=48000,aformat=channel_layouts=stereo,"
                  f"apad,atrim=0:{MEME_SECONDS}[aud]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{MEME_SECONDS}", "-i", "anullsrc=r=48000:cl=stereo"]
        graph += f";[{next_input}:a]anull[aud]"

    _run("highlight meme beat", ["ffmpeg", "-y", *inputs, "-filter_complex", graph,
                                 "-map", video, "-map", "[aud]",
                                 "-frames:v", str(frames), "-t", f"{MEME_SECONDS}",
                                 "-threads", str(render.FFMPEG_THREADS),
                                 *_INTERMEDIATE, out])
    try:
        os.remove(frame)
    except OSError:
        pass


def _sfx(name: str):
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                        "assets", "sfx", name))
    return path if os.path.exists(path) else None


def build(job_dir: str, segments: list, width: int, height: int,
          genre: str = "general", title: str = "", memes: bool = True,
          tags: bool = True, high_quality: bool = False,
          music_path: str = None, music_gain: float = 0.1) -> dict:
    """Join rendered segments into `highlight.mp4`.

    `segments` are in story order: {"file", "title", "score", "source_start",
    "source_end", "words" (on the segment's own timeline)}. Raises on an ffmpeg
    failure.
    """
    parts = []
    for seg in segments:
        path = os.path.join(job_dir, seg["file"])
        parts.append({**seg, "kind": "segment", "path": path,
                      "duration": content.probe_duration(path)})
    parts = [p for p in parts if p["duration"] > 0.5]
    if not parts:
        raise RuntimeError("No highlight segments rendered.")
    # Camera work per part: alternate push in / push out, and a punch-in on
    # a clear reaction peak (a loud spike well above the part's own level).
    for i, part in enumerate(parts):
        part["motion"] = 1 if i % 2 == 0 else -1
        energy = content.audio_energy(part["path"])
        if energy and part["duration"] > 6:
            peak = max(range(len(energy)), key=energy.__getitem__)
            at = peak * content.HIGHLIGHT_HOP
            mean = sum(energy) / len(energy)
            if mean > 0 and energy[peak] >= PUNCH_RATIO * mean and 1.0 < at < part["duration"] - 1.0:
                part["punch"] = at

    # Cold open: the peak of the strongest moment, before the story starts.
    if len(parts) >= 3:
        best = max(parts, key=lambda p: p.get("score") or 0)
        s, e = teaser_window(best.get("words") or [], best["duration"],
                             content.audio_energy(best["path"]))
        teaser = os.path.join(job_dir, f"{WORK_PREFIX}teaser.mp4")
        _cut(best["path"], s, e, teaser)
        parts.insert(0, {"kind": "teaser", "path": teaser, "pip": best.get("pip"),
                         "duration": content.probe_duration(teaser)})

    plan = timeline(parts, genre, memes=memes)
    font = render._watermark_font()
    boom = _sfx("boom.wav")

    # Inputs 0..n-1 are the parts in order; generated silence and the whoosh
    # come after them. Counted explicitly: a lavfi input is six arguments.
    inputs, count, audio_inputs, silent = [], 0, {}, []
    for k, seg in enumerate(plan):
        if seg["kind"] == "meme":
            seg["path"] = os.path.join(job_dir, f"{WORK_PREFIX}meme_{k}.mp4")
            _meme(plan[k - 1]["path"], seg["path"], width, height, seg["text"],
                  seg["emoji"], font, boom)
            seg["duration"] = content.probe_duration(seg["path"]) or MEME_SECONDS
        inputs += ["-i", seg["path"]]
        count += 1
        if not render.has_audio(seg["path"]):
            silent.append(k)
    plan = _retime(plan)            # the joins, on every part's real duration
    for k in silent:
        audio_inputs[k] = count
        inputs += ["-f", "lavfi", "-t", f"{plan[k]['duration']:.3f}",
                   "-i", "anullsrc=r=48000:cl=stereo"]
        count += 1

    whoosh = render.whoosh_path()
    whoosh_input = None
    if whoosh:
        whoosh_input = count
        inputs += ["-i", whoosh]
        count += 1
    music_input = None
    if music_path and os.path.exists(music_path):
        music_input = count
        inputs += ["-stream_loop", "-1", "-i", music_path]
        count += 1

    title_file, shown = None, " ".join((title or "").split())
    if shown:
        if len(shown) > 48:
            shown = shown[:47].rstrip() + "…"
        title_file = os.path.join(job_dir, f"{WORK_PREFIX}title.txt")
        with open(title_file, "w", encoding="utf-8") as handle:
            handle.write(shown)
    first = next((k for k, s in enumerate(plan) if s["kind"] == "segment"), 0)
    title_at = 0.0 if plan[first]["offset"] is None else plan[first]["offset"] + plan[first]["fade"]

    tag_files = {}
    if tags:
        for k, text in context_tags(plan).items():
            path = os.path.join(job_dir, f"{WORK_PREFIX}tag_{k}.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            tag_files[k] = path

    eyebrow_file = None
    if title_file:
        eyebrow_file = os.path.join(job_dir, f"{WORK_PREFIX}eyebrow.txt")
        with open(eyebrow_file, "w", encoding="utf-8") as handle:
            handle.write("HIGHLIGHTS")
    section_files = {}
    for k, seg in enumerate(plan):
        name = " ".join((seg.get("title") or "").split())
        if seg["kind"] == "segment" and name and seg["duration"] >= 8:
            if len(name) > 52:
                name = name[:51].rstrip() + "…"
            path = os.path.join(job_dir, f"{WORK_PREFIX}section_{k}.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(name)
            section_files[k] = path

    graph = join_graph(plan, audio_inputs, width, height, whoosh_input, font,
                       title_file, title_at,
                       title_size(shown, width, height, share=TITLE_SHARE) if shown else 0,
                       tag_files, section_files, eyebrow_file, music_input, music_gain)
    out_path = os.path.join(job_dir, HIGHLIGHT_FILE)
    _run("highlight video", [
        "ffmpeg", "-y", *inputs, "-filter_complex", graph,
        "-map", "[vout]", "-map", "[aout]",
        *render._hw_encode_flags(high_quality, None),
        "-threads", str(render.FFMPEG_THREADS),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", out_path,
    ])
    return {
        "file": HIGHLIGHT_FILE,
        "duration": total_duration(plan),
        "segments": sum(1 for s in plan if s["kind"] == "segment"),
        "memes": sum(1 for s in plan if s["kind"] == "meme"),
        "chapters": chapters(plan),
    }


def _retime(plan: list) -> list:
    """Recompute offsets after the real durations of every part are known."""
    length = 0.0
    for k, seg in enumerate(plan):
        if k == 0:
            seg.update(offset=None, fade=0.0)
            length = seg["duration"]
            continue
        wanted = MEME_FADE if "meme" in (seg["kind"], plan[k - 1]["kind"]) else FADE
        fade = min(wanted, seg["duration"] / 3, plan[k - 1]["duration"] / 3)
        seg.update(fade=round(fade, 3), offset=round(length - fade, 3))
        length += seg["duration"] - fade
    return plan


def credits_minutes(seconds: float) -> int:
    """Billable minutes of a finished highlight video."""
    return max(1, math.ceil(seconds / 60))
