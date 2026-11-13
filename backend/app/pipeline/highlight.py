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
TITLE_SHARE = 0.065              # title card height, of the frame
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
               tag_files: dict = None) -> str:
    """filter_complex joining inputs 0..n-1 (one per segment) into [vout][aout].

    Every input is normalised first -- frame rate, size, timebase, audio format
    -- because xfade refuses inputs that differ, and a teaser, rendered
    segments and generated meme beats come from three different encoders.
    `audio_inputs` maps a segment to the input holding its sound (a silent
    segment borrows a generated silence).
    """
    parts = []
    for k in range(len(segments)):
        parts.append(f"[{k}:v]fps={FPS},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                     f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                     f"format=yuv420p,settb=AVTB[v{k}n]")
        parts.append(f"[{audio_inputs.get(k, k)}:a]aresample=48000,"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{k}n]")
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
    parts.append(f"{a}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
                 f"afade=t=in:st=0:d=0.04,"
                 f"afade=t=out:st={max(0.0, total - 0.4):.3f}:d=0.4[aout]")

    draws = []
    if font and title_file:
        draws.append(_drawtext(font, title_file, title_px or title_size("", width, height),
                               title_at + 0.2, title_at + 3.2, "h*0.08", box=0.4))
    if font:
        tag_px = max(22, int(height * 0.03))
        for k, path in sorted((tag_files or {}).items()):
            t0 = (segments[k]["offset"] or 0.0) + segments[k]["fade"]
            draws.append(_drawtext(font, path, tag_px, t0 + 0.1, t0 + 2.2,
                                   "h*0.08", box=0.55))
    parts.append(f"{v}{','.join(draws)}[vout]" if draws else f"{v}null[vout]")
    return ";".join(parts)


def _drawtext(font: str, text_file: str, size: int, t0: float, t1: float,
              y: str, box: float) -> str:
    """A centred caption that fades in and out over [t0, t1] of the video."""
    alpha = (f"if(lt(t,{t0 + 0.2:.2f}),(t-{t0:.2f})/0.2,"
             f"if(gt(t,{t1 - 0.2:.2f}),({t1:.2f}-t)/0.2,1))")
    return (f"drawtext=fontfile='{render._ff_escape(font)}'"
            f":textfile='{render._ff_escape(text_file)}'"
            f":fontsize={size}:fontcolor=white:borderw={max(3, size // 12)}"
            f":bordercolor=black:box=1:boxcolor=black@{box}:boxborderw={size // 3}"
            f":x=(w-text_w)/2:y={y}"
            f":enable='between(t,{t0:.2f},{t1:.2f})':alpha='{alpha}'")


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
          tags: bool = True, high_quality: bool = False) -> dict:
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

    # Cold open: the peak of the strongest moment, before the story starts.
    if len(parts) >= 3:
        best = max(parts, key=lambda p: p.get("score") or 0)
        s, e = teaser_window(best.get("words") or [], best["duration"],
                             content.audio_energy(best["path"]))
        teaser = os.path.join(job_dir, f"{WORK_PREFIX}teaser.mp4")
        _cut(best["path"], s, e, teaser)
        parts.insert(0, {"kind": "teaser", "path": teaser,
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

    graph = join_graph(plan, audio_inputs, width, height, whoosh_input, font,
                       title_file, title_at,
                       title_size(shown, width, height, share=TITLE_SHARE) if shown else 0,
                       tag_files)
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
