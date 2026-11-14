"""Works out WHAT a clip is, and therefore how it should be framed.

The old pipeline asked the user to pick a template before any footage had been
looked at, then applied that one treatment to every clip. That is fine for a
single-format channel and wrong for everybody else: a podcast episode and a
gameplay VOD need opposite framings, and even inside one video the source cuts
between a wide two-shot, a single close-up and a screen. Picking per VIDEO also
ignores that an editor decides per SHOT.

This module answers the question the renderer actually needs answered -- "what
is on screen, and where is the interesting part of it" -- and returns the same
plan dict `reframe.plan` already returns, so `render._adaptive_filter` composes
all of them with one code path.

The four treatments
-------------------
podcast / talking head
    There are faces. Hand over to `reframe`, which tracks them, stacks
    simultaneous speakers and follows the active one.

gameplay with a facecam
    A small, fixed, persistently-placed face sits over fast-moving content --
    the shape of every stream layout. The full gameplay frame goes in a band at
    the top (so the HUD, killfeed and minimap survive) and the facecam gets the
    space underneath, cropped around the face. This is the composition viewers
    already recognise, and cropping the game to 9:16 instead would throw away
    exactly the information a viewer watches for.

gameplay (no facecam)
    Fast content, no stable face. Follow the motion: a centre-weighted
    frame-difference saliency map gives the crop something to chase, smoothed
    with the same damping the face tracker uses so it does not read as a drone.

screencast
    Slow, text-heavy, no faces. Nothing should move: letterbox the frame
    undistorted and leave it alone. Tracking text is how you make a tutorial
    unwatchable.

Everything here is a heuristic on purpose. The alternative -- a second model
call per clip to say "this is a game" -- costs money and latency to answer a
question that frame statistics answer reliably, and the failure mode is benign:
a wrong guess falls back to a treatment that is merely not the best one.
"""

import os
import subprocess

from app.pipeline import hud as hud_mod
from app.pipeline import reframe

# --------------------------------------------------------------------------
# Detection tuning
# --------------------------------------------------------------------------

MOTION_FPS = 5.0          # saliency samples per second. Action does not need
                          # more; the smoothing below removes anything faster.
MOTION_WIDTH = 320        # analysis resolution. Frame differences are a
                          # coarse signal and full resolution costs 10x.
SCENE_THRESHOLD = 0.32    # ffmpeg scene score for "this is a cut".

# A face is a facecam face when it is small, alone for most of the clip, and
# parked in the same place. The size ceiling is what separates a stream webcam
# from a vlog close-up: the latter fills a third of the frame.
FACECAM_MAX_FACE = 0.16
FACECAM_MIN_COVERAGE = 0.45
FACECAM_CENTRE_X = 0.15     # |x - 0.5| below this and ...
FACECAM_CENTRE_Y = (0.2, 0.7)  # ... y inside this band = a vlog face, not a webcam
FACECAM_MAX_DRIFT = 0.045   # std-dev of centre, as a fraction of the frame
# The rest of the frame has to be moving like a game, not like a person
# gesturing. At 0.010 a medium talking-head shot qualified: the presenter's
# hands move, the face is small enough to pass the size gate, and the result was
# a "webcam" crop of the guest plus a blurred copy of him underneath. A real
# game moves an order of magnitude more than a seated interview.
FACECAM_MIN_MOTION = 0.035

# Co-occurrence above this means "two people are on screen together", which is
# the one unambiguous signal for a conversation rather than a facecam.
PODCAST_CO_OCCURRENCE = 0.30

# Motion score (mean absolute frame difference, 0-1) above which footage is
# considered dynamic. Measured on the analysis downscale.
MOTION_GAMEPLAY = 0.018
MOTION_TALKING = 0.035     # talking head above this is probably a game webcam

# How much of a clip's frames contain a face before it counts as "has faces".
FACE_RATE_MIN = 0.45

# Fraction of the output height the gameplay band occupies when stacking. The
# full 16:9 frame would take only 0.316 of a 1080x1920 canvas, which leaves the
# facecam two thirds of the screen; a little over a third keeps 94% of the
# gameplay width (the HUD and killfeed are at the edges) while giving the
# reactions real presence.
GAMEPLAY_BAND = 0.37

# Captions sit over the lower part of the facecam tile, below the eyeline of
# the crop above it -- and clear of the platform's own chrome.
GAMEPLAY_CAPTION_POS = 0.86

# The webcam gets its own 16:9 band under the gameplay. Matching the band to
# the webcam's shape means the picture fills it without a portrait crop or big
# letterbox bars; the remaining third is the caption surface.
FACECAM_BAND = 0.37
SCREENCAST_CAPTION_POS = 0.86

# --- gameplay crop --------------------------------------------------------

# The action crop chases motion, but weighted toward the centre: games flash at
# the edges constantly (killfeed, ability icons, a scoreboard update) and a crop
# that snaps to every one of those is unwatchable.
ACTION_CENTRE_BIAS = 0.30
ACTION_MIN_MOTION = 0.047  # normalised (0-1) frame difference. Below this the
                           # frame is noise, not movement.


# --------------------------------------------------------------------------
# Silent footage
# --------------------------------------------------------------------------
#
# Not every clippable video has speech. A gameplay VOD with no commentary, a
# music montage, a screen recording of a build: the spoken pipeline has nothing
# to read and used to refuse the job outright. The same audio-visual signals
# that make the spoken path's prosody useful -- loudness and motion -- are
# enough to find the moments in footage like this.

# The curve every highlight is read from is sampled on this grid.
HIGHLIGHT_HOP = 0.5        # seconds
SILENT_SAMPLE_FPS = 2.0    # motion samples per second across the whole video
SILENT_EDGE_GUARD = 2.0    # ignore the first/last seconds: intros and outros

# What a moment is made of. Loudness says something is going on; ONSET -- how
# suddenly it got loud -- is what separates a kill, a crash or a scream from a
# soundtrack that is simply loud throughout; motion is the picture agreeing.
# Motion leads when there is picture to read, because game audio is often music.
HIGHLIGHT_WEIGHTS = {"motion": 0.45, "level": 0.25, "onset": 0.30}
AUDIO_ONLY_WEIGHTS = {"level": 0.45, "onset": 0.55}

# A highlight is an EVENT with a shape, not a fixed-size window dropped on a
# peak. Every clip opens on the approach and closes on the aftermath: the
# seconds before the crash are what make it a crash rather than a loud noise,
# and cutting on the impact itself throws away the reaction.
EVENT_LEAD_IN = 5.0        # seconds always kept before the peak
EVENT_TAIL = 3.0           # seconds always kept after it
EVENT_FLOOR = 0.40         # the window grows while the curve stays above this
                           # fraction of the way from baseline up to the peak
EVENT_GAP = 4.0            # dead space kept between two picked events
EVENT_MIN_RISE = 0.10      # a peak this close to the baseline is not an event

# Clip length by preference: (shortest, longest) in seconds. "any" is the range
# gameplay shorts actually run at -- long enough to carry a setup, short enough
# that nothing has to be padded to get there.
EVENT_LENGTHS = {
    "any": (12.0, 30.0),
    "short": (8.0, 20.0),
    "medium": (25.0, 55.0),
    "long": (50.0, 85.0),
    "extended": (80.0, 120.0),
}


def probe_duration(video_path):
    """Seconds, or 0.0 when it cannot be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30,
        )
        return max(0.0, float(out.stdout.strip().splitlines()[0]))
    except Exception:
        return 0.0


def _rms_frames(audio_path):
    """RMS per `energy.FRAME_MS`, or [] if there is no usable audio."""
    if not audio_path or not os.path.exists(audio_path):
        return []
    try:
        from app.pipeline import energy
        return energy._frame_rms(energy._decode_pcm(audio_path)) or []
    except Exception:
        return []


def audio_energy(audio_path, hop=HIGHLIGHT_HOP):
    """Mean RMS per `hop` seconds, or [] if there is no usable audio."""
    return audio_events(audio_path, hop)[0]


def audio_events(audio_path, hop=HIGHLIGHT_HOP):
    """(level, onset) per `hop` seconds; two empty lists without usable audio.

    level  mean loudness of the hop
    onset  the sharpest RISE inside the hop, measured against the half second
           before it and in log energy, so a gunshot over quiet footsteps and a
           gunshot over a loud soundtrack both register as the same jump.
    """
    import math
    from app.pipeline import energy

    frames = _rms_frames(audio_path)
    if not frames:
        return [], []
    per = max(1, int(hop * 1000 / energy.FRAME_MS))
    logs = [math.log1p(max(0.0, f)) for f in frames]
    back = 5                                   # frames of history: 0.5s
    rise = [0.0] * len(logs)
    for i in range(1, len(logs)):
        lo = max(0, i - back)
        rise[i] = max(0.0, logs[i] - sum(logs[lo:i]) / (i - lo))

    level, onset = [], []
    for i in range(0, len(frames) - per + 1, per):
        level.append(sum(frames[i:i + per]) / per)
        onset.append(max(rise[i:i + per]))
    return level, onset


def _normalise(values, percentile=0.95):
    """Scale to 0-1 between the footage at rest and its strongest moments.

    Measured from the MEDIAN up, not from zero: a highlight is rare by
    definition, so on a long video the 95th percentile is still the background
    and dividing by it pinned the whole curve at 1.0 -- every second looked like
    a peak and the picker chose at random. The ceiling is the higher of a top
    percentile and half the maximum, so one gunshot cannot flatten everything
    else and a video with only two real events still tells them apart.
    """
    if not values:
        return []
    ordered = sorted(values)
    rest = ordered[len(ordered) // 2]
    top = max(ordered[min(len(ordered) - 1, int(len(ordered) * percentile))],
              0.5 * ordered[-1])
    span = top - rest
    if span <= 1e-9:
        return [0.0] * len(values)
    return [max(0.0, min(1.0, (v - rest) / span)) for v in values]


def _moving_average(values, radius=1):
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - radius), min(len(values), i + radius + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _fit(values, grid):
    """`values` padded or cut to exactly `grid` samples."""
    return (list(values) + [0.0] * grid)[:grid]


def highlight_curve(duration, video_path=None, audio_path=None,
                    hop=HIGHLIGHT_HOP):
    """How eventful each `hop` of the source is, 0-1. [] when nothing is readable.

    Works from audio alone when there is no picture to read yet -- which is the
    normal case for a link job, where the video is only downloaded after the
    moments are chosen. Motion is added when a file is already on disk.
    """
    if duration <= 1:
        return []
    grid = max(1, int(duration / hop))
    parts = {}

    if video_path:
        probe = reframe.analysis_source(video_path)
        motion, _ = frame_motion(probe, 0.0, duration, fps=SILENT_SAMPLE_FPS)
        total, count = [0.0] * grid, [0] * grid
        for t, _cx, _cy, value in motion:
            index = int(t / hop)
            if 0 <= index < grid:
                total[index] += value
                count[index] += 1
        if any(count):
            parts["motion"] = _normalise(
                [total[i] / count[i] if count[i] else 0.0 for i in range(grid)])

    level, onset = audio_events(audio_path, hop) if audio_path else ([], [])
    if level and max(level) > 0:
        parts["level"] = _normalise(_fit(level, grid))
        parts["onset"] = _normalise(_fit(onset, grid), percentile=0.98)

    if not parts:
        return []
    weights = HIGHLIGHT_WEIGHTS if "motion" in parts else AUDIO_ONLY_WEIGHTS
    used = {k: w for k, w in weights.items() if k in parts}
    scale = sum(used.values()) or 1.0
    combined = [sum(used[k] * parts[k][i] for k in used) / scale
                for i in range(grid)]
    return _moving_average(combined, radius=1)          # ~1.5s smoothing


def _event_window(curve, peak, baseline, hop, duration, min_len, max_len):
    """(start, end) seconds for the event whose peak is at index `peak`.

    Grows outward from the peak while the curve holds above a floor set between
    the baseline and the peak, always keeping a lead-in and a tail, then fits
    the result to the allowed length. Extra length is added mostly in FRONT:
    a longer clip of an event wants more approach, not more aftermath.
    """
    floor = baseline + EVENT_FLOOR * (curve[peak] - baseline)
    lead, tail = int(EVENT_LEAD_IN / hop), int(EVENT_TAIL / hop)
    most = int(max_len / hop)

    lo = peak
    while lo > 0 and peak - lo < most and (peak - lo < lead or curve[lo - 1] >= floor):
        lo -= 1
    hi = peak
    while (hi < len(curve) - 1 and hi - lo < most
           and (hi - peak < tail or curve[hi + 1] >= floor)):
        hi += 1

    start, end = lo * hop, min(duration, (hi + 1) * hop)
    short = min_len - (end - start)
    if short > 0:
        start -= short * 0.65
        end += short * 0.35
    if end - start > max_len:
        # Keep the peak inside what is left, a third of the way from the end.
        end = min(end, peak * hop + max_len / 3)
        start = end - max_len
    if start < 0:
        end, start = end - start, 0.0
    if end > duration:
        start, end = max(0.0, start - (end - duration)), duration
    return round(start, 2), round(end, 2)


def highlight_moments(video_path, duration, n_clips, audio_path=None,
                      length_pref="any", words=None, avoid=None):
    """Pick moments from motion and sound when the transcript cannot.

    video_path   may be None: link jobs choose their moments before the video is
                 downloaded, so this has to work from the audio track alone.
    words        every transcribed word in the source, if there are any. The
                 ones inside a picked window ride along, so sparse commentary
                 over gameplay is still captioned.
    avoid        [(start, end)] already taken by other picks; new moments keep
                 clear of them.

    Returns clip dicts in the same shape `analyzer.resolve_clip_times` produces,
    so the rest of the pipeline -- billing, framing, rendering, editing -- does
    not need to know this path exists.
    """
    hop = HIGHLIGHT_HOP
    curve = highlight_curve(duration, video_path, audio_path, hop)
    if not curve or n_clips <= 0:
        return []
    min_len, max_len = EVENT_LENGTHS.get(length_pref or "any", EVENT_LENGTHS["any"])
    max_len = min(max_len, max(min_len, duration))
    min_len = min(min_len, max_len)

    ordered = sorted(curve)
    baseline = ordered[len(ordered) // 2]                # the footage at rest
    guard = int(SILENT_EDGE_GUARD / hop)
    gap = int(EVENT_GAP / hop)

    blocked = [False] * len(curve)
    for i in list(range(0, min(guard, len(curve)))) + \
            list(range(max(0, len(curve) - guard), len(curve))):
        blocked[i] = True
    for s, e in (avoid or []):
        for i in range(max(0, int(s / hop) - gap), min(len(curve), int(e / hop) + gap + 1)):
            blocked[i] = True

    picked = []
    while len(picked) < n_clips:
        free = [i for i in range(len(curve)) if not blocked[i]]
        if not free:
            break
        peak = max(free, key=lambda i: curve[i])
        rise = curve[peak] - baseline
        # Past the first pick, a "peak" barely above the footage at rest is the
        # picker running out of events, and padding the set with it would hand
        # back a clip of nothing. The first pick is exempt so that flat footage
        # still yields its least-flat moment rather than an error.
        if picked and rise < EVENT_MIN_RISE:
            break
        start, end = _event_window(curve, peak, baseline, hop, duration,
                                   min_len, max_len)
        lo, hi = int(start / hop), min(len(curve), int(end / hop) + 1)
        inside = curve[lo:hi] or [curve[peak]]
        picked.append({
            "start": start, "end": end,
            # The peak that earns the clip, and how well the rest holds up.
            "strength": 0.6 * curve[peak] + 0.25 * (sum(inside) / len(inside))
                        + 0.15 * max(0.0, rise),
        })
        for i in range(max(0, lo - gap), min(len(curve), hi + gap)):
            blocked[i] = True

    clips = []
    for pick in picked:
        s, e = pick["start"], pick["end"]
        inside = [w for w in (words or []) if s <= w.get("start", -1) and w.get("end", 0) <= e]
        clips.append({
            "start": s,
            "end": e,
            "duration": round(e - s, 1),
            "title": "",
            "hook": "",
            "keywords": [],
            "words": inside,
            "scores": {},
            "score": round(min(9.5, 5.0 + pick["strength"] * 4.5), 2),
            # `silent` is what downstream uses to skip caption work; a window
            # that caught commentary is not silent even though it was found by
            # sound and motion rather than by reading.
            "silent": not inside,
            "highlight": True,
        })
    clips.sort(key=lambda c: -c["score"])
    return clips


# --------------------------------------------------------------------------
# Genre
# --------------------------------------------------------------------------
# Which rubric the moment picker should use, decided from what is already known
# before any video is downloaded: the title and how much of the runtime is
# speech. Deliberately coarse -- three answers, and the wrong one costs a
# slightly less suited prompt, not a failed job.

GAMING_TITLE_WORDS = (
    "gameplay", "walkthrough", "playthrough", "let's play", "lets play",
    "speedrun", "no commentary", "longplay", "gta", "fortnite", "minecraft",
    "warzone", "call of duty", "valorant", "apex legends", "elden ring",
    "roblox", "fifa", "fc 25", "fc 26", "pubg", "cs2", "counter-strike",
    "league of legends", "overwatch", "red dead", "cyberpunk", "spider-man",
    "god of war", "zelda", "mario", "pokemon", "ps5", "ps4", "xbox",
    "nintendo switch", "rtx", "4k 60fps", "boss fight", "ranked", "1v1",
    "full game", "stream highlights", "funny moments",
)
# Checked before the podcast words: "INDIA'S GOT LATENT S2 EP5" is a comedy
# show, not an interview, even though it says "ep".
COMEDY_TITLE_WORDS = (
    "latent", "roast", "stand-up", "standup", "stand up", "comedy", "comedian",
    "open mic", "crowd work", "sketch", "prank",
)
VLOG_TITLE_WORDS = (
    "vlog", "trip", "travel", "tour", "day in", "24 hours", "exploring",
    "visiting", "went to", "road trip", "surprise", "family",
)
# Untitled uploads: words a vlogger says to the camera.
VLOG_SPEECH_CUES = ("vlog", "trip", "guys", "packing", "flight", "airport",
                    "hotel", "reach", "pahunch")
PODCAST_TITLE_WORDS = (
    "podcast", "interview", "episode", " ep.", " ep ", "talk", "conversation",
    "q&a", "ama", "keynote", "lecture", "debate", "fireside", "explains",
)
# Below this share of the runtime being speech, the talking is commentary over
# something else rather than the content itself. A two-person podcast sits near
# 0.9 and a solo essay near 0.8; a streamer reacting to a game lands at 0.2-0.5.
GAMING_SPEECH_COVERAGE = 0.45
VLOG_CUE_RATE = 0.004       # ~1 travel/camera word in 250


def speech_coverage(utterances, duration):
    """Fraction of the runtime someone is talking, 0-1."""
    if not utterances or duration <= 0:
        return 0.0
    spoken = sum(max(0.0, u.get("end", 0) - u.get("start", 0)) for u in utterances)
    return max(0.0, min(1.0, spoken / duration))


def guess_genre(utterances, duration, title=""):
    """"gaming", "comedy", "vlog", "podcast" or "general", from title and speech."""
    text = f" {(title or '').lower()} "
    if any(word in text for word in GAMING_TITLE_WORDS):
        return "gaming"
    coverage = speech_coverage(utterances, duration)
    if any(word in text for word in COMEDY_TITLE_WORDS):
        return "comedy"
    if any(word in text for word in VLOG_TITLE_WORDS):
        return "vlog"
    if any(word in text for word in PODCAST_TITLE_WORDS) and coverage >= 0.5:
        return "podcast"
    if coverage < GAMING_SPEECH_COVERAGE:
        return "gaming"
    # Sustained speech from more than one voice is a conversation. One voice
    # could be an essay, a tutorial or a vlog, which the general rubric fits.
    said = " ".join((u.get("transcript") or u.get("text") or "") for u in utterances).lower()
    spoken = said.split()
    cues = sum(spoken.count(w) for w in VLOG_SPEECH_CUES)
    if spoken and cues / len(spoken) >= VLOG_CUE_RATE:
        return "vlog"
    speakers = {u.get("speaker") for u in utterances if u.get("speaker") is not None}
    return "podcast" if len(speakers) >= 2 else "general"


# --------------------------------------------------------------------------
# Probing / decoding helpers
# --------------------------------------------------------------------------

def _probe(video_path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", video_path],
            check=True, capture_output=True, text=True, timeout=30,
        )
        import json
        stream = json.loads(out.stdout)["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except Exception:
        pass
    # ffprobe missing is a configuration problem, not a reason to analyse the
    # 480p proxy as if it were the master and write a plan in proxy pixels.
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        if cap.isOpened():
            size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            cap.release()
            return size
    except Exception:
        pass
    return 0, 0


def scene_cuts(video_path, start, end, threshold=SCENE_THRESHOLD):
    """Timestamps (clip-relative, seconds) of hard cuts via ffmpeg's scene score.

    ffmpeg already computes this for every frame, so it is one decode pass and
    no extra dependency. A cut matters to framing because smoothing across one
    produces the single most obviously broken move in the toolkit: the crop
    sliding from last shot's subject to this shot's over half a second.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info",
             "-ss", str(max(0.0, start)), "-to", str(max(0.0, end)),
             "-i", video_path,
             # showinfo prints one line per SELECTED frame, which is exactly
             # the cuts; passthrough keeps them one per timestamp. NOT -vsync:
             # ffmpeg 9 removed it, the command then failed to start, and every
             # clip silently reported zero cuts.
             "-vf", f"select='gt(scene,{threshold})',showinfo",
             "-an", "-fps_mode", "passthrough", "-f", "null", "-"],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.SubprocessError, OSError):
        return []

    cuts = []
    for line in proc.stderr.splitlines():
        marker = "pts_time:"
        if marker not in line:
            continue
        try:
            value = float(line.split(marker, 1)[1].split()[0])
        except (IndexError, ValueError):
            continue
        # showinfo reports the source timestamps after -ss. Some ffmpeg builds
        # keep the seek offset, so accept only values inside the window.
        if 0.0 <= value <= (end - start) + 0.5:
            cuts.append(round(value, 3))
        elif start <= value <= end:
            cuts.append(round(value - start, 3))
    return cuts


def frame_motion(video_path, start, end, fps=MOTION_FPS, width=MOTION_WIDTH):
    """Per-sample action saliency.

    Returns (samples, mean_energy) where each sample is
    (t, cx, cy, energy) with cx/cy the centre of the moving mass (0-1) and
    energy the mean frame difference (0-255 scale).

    This is the signal for footage a face tracker cannot help with: no faces, but
    a screen full of movement. Games, sports, action b-roll. The centroid is
    weighted by a Gaussian around the centre of frame because the edges of a
    game HUD change constantly without anything the viewer cares about moving.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return [], 0.0

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return [], 0.0
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if not src_w or not src_h:
        cap.release()
        return [], 0.0

    scale = min(1.0, width / float(src_w))
    det_w, det_h = max(2, int(src_w * scale)), max(2, int(src_h * scale))
    step = max(1, int(round(src_fps / max(fps, 0.1))))
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start) * 1000.0)

    # Gaussian centre weight, built once. The exponent is broad: the middle
    # ~70% of the frame keeps most of its weight, the corners keep little.
    ys = (np.arange(det_h) / max(det_h - 1, 1) - 0.5) / 0.34
    xs = (np.arange(det_w) / max(det_w - 1, 1) - 0.5) / 0.34
    weight = np.exp(-0.5 * (ys[:, None] ** 2 + xs[None, :] ** 2)).astype(np.float32)

    prev = None
    samples = []
    energies = []
    frame_no = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_no += 1
        if frame_no % step:
            continue
        t = float(cap.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0 - start
        # A seek during the sequential read can leave POS_MSEC unreliable in
        # some containers; fall back to an estimate from the frame index.
        if t < -0.5 or t > (end - start) + 1.0:
            t = frame_no / src_fps - start
        if t > (end - start) + 0.5:
            break

        small = cv2.resize(frame, (det_w, det_h))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if prev is not None:
            diff = cv2.absdiff(gray, prev).astype(np.float32)
            # Normalised to 0-1 so the thresholds above are readable and do not
            # depend on which bit depth a decoder hands back.
            energy = float(diff.mean()) / 255.0
            energies.append(energy)
            if energy >= ACTION_MIN_MOTION:
                # Blur the difference before taking its centroid: individual
                # noisy pixels would drag the mass around at video rates.
                mass = cv2.GaussianBlur(diff, (0, 0), 3.0) * weight
                total = float(mass.sum())
                if total > 1e-3:
                    mx = (mass.sum(axis=0) * np.arange(det_w)).sum() / total
                    my = (mass.sum(axis=1) * np.arange(det_h)).sum() / total
                    cx, cy = float(mx / det_w), float(my / det_h)
                else:
                    cx = cy = 0.5
            else:
                cx, cy = 0.5, 0.5
            samples.append((round(max(0.0, t), 2), cx, cy, energy))
        prev = gray

    cap.release()
    mean_energy = sum(energies) / len(energies) if energies else 0.0
    return samples, mean_energy


# --------------------------------------------------------------------------
# Facecam
# --------------------------------------------------------------------------

CORNER_EDGE = 0.22          # a cam overlay sits within this of two frame edges
CORNER_MAX_FACE = 0.22      # ...and is small
CORNER_COVERAGE = 0.7       # ...and is there almost the whole time


def corner_facecam(samples, src_w, src_h):
    """A streamer's webcam overlay in a watch-along, or None.

    `find_facecam` wants one small face over moving content. A watch-along has
    other faces too -- the people in the video being watched -- so it reads as
    a conversation and finds no cam. The streamer's cam is still recognisable:
    a small face, present in nearly every sample, pinned in a CORNER of the
    frame while the other faces move around the middle.
    Returns find_facecam's shape: {"centre": (x, y), "win": (w, h)} in pixels.
    """
    if not samples or not src_w or not src_h:
        return None
    tracks = reframe.build_tracks(samples)
    if len(tracks) < 2:
        return None                     # one face is a talking head, not a cam
    total = len(samples)
    best = None
    for tr in tracks:
        pts = tr["points"]
        if len(pts) / total < CORNER_COVERAGE:
            continue
        cx = sum(p[1] for p in pts) / len(pts)
        cy = sum(p[2] for p in pts) / len(pts)
        fh = sum(p[4] for p in pts) / len(pts)
        near_x = cx < CORNER_EDGE or cx > 1 - CORNER_EDGE
        near_y = cy < CORNER_EDGE or cy > 1 - CORNER_EDGE
        if fh > CORNER_MAX_FACE or not (near_x and near_y):
            continue
        if best is None or len(pts) > best[0]:
            best = (len(pts), cx, cy, fh)
    if not best:
        return None
    _, cx, cy, fh = best
    # Where the watched video is: the other faces, weighted by how long they
    # are on screen. An editor pushes in there.
    others = [tr for tr in tracks
              if abs(sum(p[1] for p in tr["points"]) / len(tr["points"]) - cx) > 0.05]
    weight = sum(len(tr["points"]) for tr in others)
    focus = None
    if weight:
        focus = (sum(sum(p[1] for p in tr["points"]) for tr in others) / weight * src_w,
                 sum(sum(p[2] for p in tr["points"]) for tr in others) / weight * src_h)
    # Head and shoulders of the overlay around the face, 4:3, inside the frame.
    h = min(src_h, fh * src_h * 2.4)
    w = min(src_w, h * 4 / 3)
    x = min(max(cx * src_w, w / 2), src_w - w / 2)
    y = min(max(cy * src_h + 0.15 * fh * src_h, h / 2), src_h - h / 2)
    return {"centre": (x, y), "win": (w, h), "focus": focus}


def find_facecam(samples, src_w, src_h):
    """Locate a stream's webcam overlay, or None.

    A facecam is not just "a face". It is a face that is
    - always the same face (small, because the game owns the frame),
    - always in the same place (the overlay does not move),
    - present for most of the clip,
    - and sitting on top of something else that is moving.

    The last condition is what stops a video call -- one small face on a static
    background -- from being mistaken for a game with a webcam, and it is why
    this takes `motion` separately.

    Returns {"points": [(t, cx, cy)], "win": (w, h), "centre": (cx, cy)}
    in source pixels, or None.
    """
    if not samples or not src_w or not src_h:
        return None
    total = len(samples)
    points = [
        (t, f[0], f[1], f[2], f[3])
        for t, faces in samples for f in faces
        if f[2] <= FACECAM_MAX_FACE
    ]
    if len(points) < max(4, total * 0.3):
        return None

    # Densest 4% spatial cell, then everything within one cell of it. Cheap
    # mean-shift: the overlay is static, so the mode of the positions is the
    # overlay, and one pass is enough.
    cell = 0.04
    hist = {}
    for _, cx, cy, _, _ in points:
        hist[(int(cx / cell), int(cy / cell))] = \
            hist.get((int(cx / cell), int(cy / cell)), 0) + 1
    (bx, by), _ = max(hist.items(), key=lambda kv: kv[1])
    seed_x, seed_y = (bx + 0.5) * cell, (by + 0.5) * cell
    cluster = [
        p for p in points
        if (p[1] - seed_x) ** 2 + (p[2] - seed_y) ** 2 <= (cell * 2.0) ** 2
    ]
    if len(cluster) < 4:
        return None

    mean_x = sum(p[1] for p in cluster) / len(cluster)
    mean_y = sum(p[2] for p in cluster) / len(cluster)
    std_x = (sum((p[1] - mean_x) ** 2 for p in cluster) / len(cluster)) ** 0.5
    std_y = (sum((p[2] - mean_y) ** 2 for p in cluster) / len(cluster)) ** 0.5
    mean_w = sum(p[3] for p in cluster) / len(cluster)
    mean_h = sum(p[4] for p in cluster) / len(cluster)

    times = {p[0] for p in cluster}
    coverage = len(times) / total
    if coverage < FACECAM_MIN_COVERAGE:
        return None
    if std_x > FACECAM_MAX_DRIFT or std_y > FACECAM_MAX_DRIFT:
        return None
    # A webcam overlay sits in a corner or along an edge, out of the game's
    # way. A face in the middle of a moving frame is a handheld vlog -- the
    # camera shakes, the face stays put -- and the facecam layout shrank it
    # into a box over a blurred strip.
    if (abs(mean_x - 0.5) < FACECAM_CENTRE_X
            and FACECAM_CENTRE_Y[0] < mean_y < FACECAM_CENTRE_Y[1]):
        return None

    # The overlay is roughly 2.5 face-widths across. Clamped so a distant face
    # on a wide gameplay shot cannot claim half the frame as its webcam.
    # A webcam is a landscape window, and a face fills roughly a fifth of its
    # width -- so the overlay is about 4.5 face-widths across. Estimating its
    # SHAPE (rather than cropping to the output tile's shape) is what lets the
    # renderer show the whole webcam image with a blurred backdrop instead of
    # upscaling a strip of forehead.
    win_w = min(max(mean_w * 4.6, 0.18), 0.55) * src_w
    win_h = win_w * 9.0 / 16.0
    if win_w < 120 or win_h < 68:
        return None
    if win_h > src_h:
        win_h = src_h
        win_w = win_h * 16.0 / 9.0

    # The face sits a little above centre in most webcam framings; nudge the
    # window up so hair and shoulders are inside it rather than cropped.
    centre_y = mean_y * src_h + (0.45 - 0.5) * win_h
    centre_y = _clamp(centre_y, win_h / 2, src_h - win_h / 2)
    centre_x = _clamp(mean_x * src_w, win_w / 2, src_w - win_w / 2)

    return {
        "points": [(samples[0][0], centre_x, centre_y)],
        "win": (int(round(win_w)), int(round(win_h))),
        "coverage": round(coverage, 2),
        "centre": (centre_x, centre_y),
    }


def _clamp(value, low, high):
    if high < low:
        return (low + high) / 2
    return min(max(value, low), high)


def _co_occurrence(samples):
    seen = [s for s in samples if s[1]]
    if not seen:
        return 0.0
    return sum(1 for _, faces in seen if len(faces) >= 2) / len(seen)


def _face_rate(samples):
    if not samples:
        return 0.0
    return sum(1 for _, faces in samples if faces) / len(samples)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def classify(samples, motion_energy, cuts, duration, src_w=0, src_h=0):
    """Label the clip and return (kind, facecam_or_None).

    Order matters. Two people on screen at once is the strongest evidence
    available and is checked first, because a two-shot also produces two small
    persistent faces that would otherwise look like two webcams. A facecam is
    only considered after that, and only when the rest of the frame is moving.
    """
    face_rate = _face_rate(samples)
    co = _co_occurrence(samples)
    cut_rate = len(cuts) / max(duration, 1.0)
    has_faces = face_rate >= FACE_RATE_MIN

    if has_faces and co >= PODCAST_CO_OCCURRENCE:
        return "podcast", None

    facecam = find_facecam(samples, src_w, src_h) if has_faces else None
    if facecam and motion_energy >= FACECAM_MIN_MOTION:
        return "gameplay_facecam", facecam

    if has_faces:
        # One stable face and a quiet frame is a talking head; the same face in
        # a busy frame is a streamer's webcam whose game we failed to detect.
        if motion_energy <= MOTION_TALKING:
            return "talking_head", None
        return "podcast", None

    # No faces at all. Dynamic footage is a game or action; static, sharp
    # footage is a screen.
    if motion_energy >= MOTION_GAMEPLAY or cut_rate >= 0.25:
        return "gameplay", None
    return "screencast", None


def analyze(video_path, start, end):
    """One pass over the clip: faces, motion, cuts, and what kind of thing it is."""
    real_w, real_h = _probe(video_path)
    probe, offset = reframe.analysis_clip(video_path, start, end)
    samples, (probe_w, probe_h) = reframe.detect_faces(
        probe, 0.0, max(0.5, end - start))
    # The clip copy starts at zero; shift detections back onto source time.
    samples = [(t + offset, faces) for t, faces in samples]
    src_w, src_h = (real_w, real_h) if real_w and real_h else (probe_w, probe_h)
    motion, motion_energy = frame_motion(probe, 0.0, max(0.5, end - start))
    # Motion stays CLIP-RELATIVE on purpose: it is consumed by the action-crop
    # planner, whose keyframes are written in output time (zero = clip start).
    # Detections are shifted to source time because scene boundaries and speaker
    # words are expressed there; motion feeds nothing that needs that base.
    cuts = scene_cuts(probe, 0.0, max(0.5, end - start))
    duration = max(0.5, float(end) - float(start))
    kind, facecam = classify(samples, motion_energy, cuts, duration,
                             src_w, src_h)
    return {
        "kind": kind,
        "facecam": facecam,
        "samples": samples,
        "motion": motion,
        "motion_energy": round(motion_energy, 4),
        "cuts": cuts,
        "src": (src_w, src_h),
        "duration": duration,
    }


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

def _static_keys(centre, start, end):
    """Two identical keys: the renderer interpolates between them for free."""
    return [(start, centre[0], centre[1]), (end, centre[0], centre[1])]


def _scene(start, end, tiles, mode="tile"):
    return {"start": start, "end": end, "mode": mode, "tiles": tiles}


def _gameplay_tile(src_w, src_h, out_w, out_h, top=0.0, band=GAMEPLAY_BAND):
    """The full gameplay frame, scaled into `band` at `top` without cropping.

    `win` is the whole source frame when it is already 16:9; on ultrawide
    captures it is cropped to the band's aspect, which is the only case where
    throwing away the sides is better than letterboxing.
    """
    tile_w = out_w
    tile_h = out_h * band
    aspect = tile_w / max(tile_h, 1.0)
    win_h = src_h
    win_w = win_h * aspect
    if win_w > src_w:
        win_w = src_w
        win_h = win_w / aspect
    centre = (src_w / 2.0, src_h / 2.0)
    return {
        "rect": (0.0, top, 1.0, band),
        "win": (int(win_w), int(win_h)),
        "keys": _static_keys(centre, 0.0, 1.0),
    }


def _facecam_tile(facecam, src_w, src_h, out_w, out_h, top, band):
    """The webcam crop, filling the space under the gameplay band.

    Static on purpose. A webcam is a fixed camera: moving the crop inside it
    would read as the presenter sliding around their own room. The framing is
    settled on the face's average position and held.
    """
    rect_h = band
    # The webcam's own shape, not the tile's. Rendering this tile with `fit`
    # puts a blurred copy behind it, so a landscape webcam on a tall tile is
    # shown whole rather than cropped to a forehead.
    win_w, win_h = facecam["win"]
    win_h = max(win_h, win_w * 9.0 / 16.0)
    win_w = min(win_w, src_w)
    win_h = min(win_h, src_h)
    cx, cy = facecam["centre"]
    cx = _clamp(cx, win_w / 2, src_w - win_w / 2)
    cy = _clamp(cy, win_h / 2, src_h - win_h / 2)
    return {
        "rect": (0.0, top, 1.0, rect_h),
        "win": (int(win_w), int(win_h)),
        "fit": True,
        "keys": _static_keys((cx, cy), 0.0, 1.0),
    }


def _plan(kind, src, duration, scenes, caption_pos):
    return {
        "version": reframe.PLAN_VERSION,
        "mode": "adaptive",
        "kind": kind,
        "src": (int(src[0]), int(src[1])),
        "duration": float(duration),
        # The renderer reads this as "put the caption baseline here", which is
        # how a gaming or screencast composition gets different caption
        # placement without the renderer knowing what a facecam is.
        "caption_pos": caption_pos,
        # One composition for the whole plan, but the caption placer still
        # reads this shape so every plan type goes through the same path.
        "captions": [{"start": 0.0, "end": float(duration),
                      "pos": caption_pos}],
        "scenes": scenes,
    }


def _caption_band_tile(src_w, src_h, top, height, duration):
    """A darkened, blurred surface for the captions to sit on."""
    return {
        "rect": (0.0, top, 1.0, height),
        "win": (int(src_w), int(src_h)),
        "backdrop": True,
        "keys": _static_keys((src_w / 2.0, src_h / 2.0), 0.0, duration),
    }


def _broll_windows(info, duration, count=2):
    """Motion peaks where a full-frame gameplay cutaway can breathe."""
    peaks = sorted((p for p in (info.get("motion") or [])
                    if 1.6 < float(p[0]) < duration - 1.6),
                   key=lambda p: float(p[3]), reverse=True)
    chosen = []
    for point in peaks:
        t = float(point[0])
        if any(abs(t - other) < 3.0 for other in chosen):
            continue
        chosen.append(t)
        if len(chosen) >= count:
            break
    return sorted(chosen)


def _gameplay_facecam_plan(info, out_w, out_h, variant=0, broll=True):
    src_w, src_h = info["src"]
    duration = info["duration"]
    # Gameplay band -> webcam band -> caption band. Three horizontal strips is
    # the composition stream clips already use, and each of the three needs a
    # different aspect, which is exactly why a single crop cannot serve them.
    #
    # Every other clip puts the facecam on top instead. Same three strips, same
    # crop sizes, different reading order: a batch of five identical stacks was
    # exactly the "these look boring" problem, and this costs one comparison.
    face_first = bool(variant % 2)
    face_top = 0.0 if face_first else GAMEPLAY_BAND
    game_top = FACECAM_BAND if face_first else 0.0
    caption_top = GAMEPLAY_BAND + FACECAM_BAND
    tiles = [
        _gameplay_tile(src_w, src_h, out_w, out_h, game_top, GAMEPLAY_BAND),
        _facecam_tile(info["facecam"], src_w, src_h, out_w, out_h, face_top,
                      FACECAM_BAND),
        _caption_band_tile(src_w, src_h, caption_top,
                           max(0.08, 1.0 - caption_top), duration),
    ]
    # Keys are clip-relative; the tile builders wrote 0..1 placeholders.
    for tile in tiles:
        tile["keys"] = _static_keys(
            (tile["keys"][0][1], tile["keys"][0][2]), 0.0, duration)

    planned = _plan("gameplay_facecam", info["src"], duration,
                    [_scene(0.0, duration, tiles)], GAMEPLAY_CAPTION_POS)
    if not broll:
        return planned

    # B-roll is an OVERLAY, not a scene split. The stack keeps rendering for the
    # whole clip and a full-frame wide shot covers it for ~1.4s at a motion
    # peak. That keeps the audio, subtitles and timeline untouched, and avoids
    # concatenating compositions with different aspect metadata.
    windows = []
    for t in _broll_windows(info, duration):
        a, b = max(0.0, t - 0.55), min(duration, t + 0.8)
        if b - a < 0.9 or any(a < other[1] and b > other[0] for other in windows):
            continue
        windows.append((round(a, 3), round(b, 3)))
    if windows:
        planned["broll"] = windows
    return planned


def _action_plan(info, out_w, out_h, start_offset=0.0):
    """Follow the action with a portrait crop of the source."""
    src_w, src_h = info["src"]
    duration = info["duration"]
    aspect = out_w / max(out_h, 1.0)

    # A full-height crop is what a 9:16 crop of 16:9 footage is. For square or
    # landscape outputs take the largest window the source allows.
    win_h = src_h
    win_w = win_h * aspect
    if win_w > src_w:
        win_w = src_w
        win_h = win_w / aspect
    win_w, win_h = max(2, int(win_w)), max(2, int(win_h))

    # Motion centroid -> crop centre, damped by the same smoother the face
    # tracker uses. Reusing `smooth` is deliberate: it already encodes dead
    # zone, min shot and speed limit, and a crop that follows motion with a
    # different feel from a crop that follows faces would look like two
    # different products.
    hud = info.get("hud") or {}
    points = [(t - start_offset, cx, cy, 1.0, 1.0, 1.0)
              for t, cx, cy, _ in info["motion"]
              # A minimap blip or ammo counter is motion, but following it is
              # how the crop ends up parked in the corner of the HUD instead of
              # on the action.
              if not hud_mod.contains(hud, cx, cy)]
    if points:
        xs = reframe.smooth(points, key=1)
        ys = reframe.smooth(points, key=2)
    else:
        xs = ys = [(0.0, 0.5)]

    keys = []
    for (t, cx), (_, cy) in zip(xs, ys):
        # Pull the target toward the centre: ACTION_CENTRE_BIAS of the way, so
        # a flash in the corner nudges the crop instead of taking it over.
        tx = (1.0 - ACTION_CENTRE_BIAS) * cx + ACTION_CENTRE_BIAS * 0.5
        ty = (1.0 - ACTION_CENTRE_BIAS) * cy + ACTION_CENTRE_BIAS * 0.5
        keys.append((max(0.0, t), tx, ty))

    if not keys:
        keys = [(0.0, 0.5, 0.5)]
    if keys[0][0] > 0:
        keys.insert(0, (0.0, keys[0][1], keys[0][2]))
    if keys[-1][0] < duration:
        keys.append((duration, keys[-1][1], keys[-1][2]))

    # Convert the 0-1 target into the crop window's centre in pixels, clamped
    # inside the source exactly as the face windows are.
    pixel_keys = []
    for t, cx, cy in keys:
        px = _clamp(cx * src_w, win_w / 2, src_w - win_w / 2)
        py = _clamp(cy * src_h, win_h / 2, src_h - win_h / 2)
        pixel_keys.append((t, round(px, 1), round(py, 1)))

    tile = {
        "rect": (0.0, 0.0, 1.0, 1.0),
        "win": (win_w, win_h),
        "keys": pixel_keys,
    }
    if hud.get("present"):
        # Keep the immersive action crop, but stamp the minimap back into the
        # corner as an inset. A full-frame letterbox preserves every HUD element
        # and shrinks the game to a third of the screen; this keeps the picture
        # filling the frame and preserves the one HUD element a GTA viewer
        # actually navigates by.
        rect = hud.get("minimap") or hud_mod.GTA_REGIONS["minimap"]
        planned = _plan("gameplay_hud", info["src"], duration,
                        [_scene(0.0, duration, [tile])], 0.78)
        planned["hud"] = hud
        planned["hud_overlay"] = {"rect": rect, "size": 0.22}
        return planned

    return _plan("gameplay", info["src"], duration,
                 [_scene(0.0, duration, [tile])], GAMEPLAY_CAPTION_POS)


def _screencast_plan(info):
    """Nothing moves. Letterbox the whole frame and leave it readable."""
    src_w, src_h = info["src"]
    duration = info["duration"]
    scene = {"start": 0.0, "end": duration, "mode": "wide", "tiles": []}
    return _plan("screencast", (src_w, src_h), duration, [scene],
                 SCREENCAST_CAPTION_POS)


def plan(video_path, start, end, out_w, out_h, speaker_words=None,
         force_kind=None, zoom_windows=None, variant=0, title="", genre="",
         broll=True, source_title=""):
    """Pick a framing treatment for this clip and return a renderer plan.

    Returns None only when there is genuinely nothing to work with (unreadable
    video). Every other outcome is a plan, because "no idea" on a gameplay
    video used to mean a letterboxed 16:9 strip on a phone.
    """
    info = analyze(video_path, start, end)
    kind = force_kind or info["kind"]
    if not info["src"][0]:
        return None
    if kind in ("gameplay", "broll", "gameplay_facecam", "screencast") or \
            (genre or "").lower() == "gaming":
        try:
            info["hud"] = hud_mod.detect(
                video_path, start, end, info["src"][0], info["src"][1],
                title=source_title or title, genre=genre)
            if info["hud"].get("present"):
                print(f"[clipper] HUD: profile={info['hud']['profile']} "
                      f"regions={len(info['hud'].get('regions') or [])} "
                      f"-> HUD-safe framing")
        except Exception as exc:
            print(f"[clipper] HUD detection skipped ({exc})")

    if kind in ("podcast", "talking_head"):
        planned = reframe.plan(video_path, start, end, out_w, out_h,
                               speaker_words=speaker_words,
                               src_size=info["src"],
                               samples=info["samples"],
                               zoom_windows=zoom_windows)
        if planned is not None:
            planned["kind"] = kind
            return planned
        # Detection found faces in the sample pass but could not build a plan.
        # A talking head with no usable track is still better as a letterbox
        # than as a wrong crop.
        return None

    def _zoomed(planned):
        # Auto Zoom applies to gameplay too, but not to a screencast: punching
        # into a screen recording crops the text someone is reading.
        # A HUD-safe fit frame is already the whole game; punching into it
        # would crop the minimap/ammo the treatment exists to preserve.
        if (planned and zoom_windows and not planned.get("hud")
                and not planned.get("broll")):
            windows = [(max(0.0, a - start), max(0.0, b - start))
                       for a, b in zoom_windows]
            planned["scenes"] = reframe._zoom_scenes(planned["scenes"], windows)
        return planned

    if kind == "gameplay_facecam" and info["facecam"]:
        return _zoomed(_gameplay_facecam_plan(
            info, out_w, out_h, variant=variant, broll=broll))

    if kind in ("gameplay", "broll"):
        return _zoomed(_action_plan(info, out_w, out_h))

    # A screen recording with a HUD in the corners is a game, not a tutorial.
    # The classifier sees no face and no camera motion and calls it a screencast;
    # the HUD check is the second opinion that turns it into a HUD-safe game plan.
    if (info.get("hud") or {}).get("present"):
        return _zoomed(_action_plan(info, out_w, out_h))

    return _screencast_plan(info)


def describe(plan_dict):
    """One line for the job log / UI: what this clip was treated as."""
    if not plan_dict:
        return "fixed frame"
    kind = plan_dict.get("kind") or plan_dict.get("mode") or "adaptive"
    scenes = plan_dict.get("scenes") or []
    modes = {s.get("mode") for s in scenes}
    tiles = sum(len(s.get("tiles") or []) for s in scenes)
    if kind == "gameplay_facecam":
        return "gameplay + facecam"
    if kind == "gameplay":
        return "action-tracked gameplay"
    if kind == "screencast":
        return "screencast"
    if "split" in modes or "reaction" in modes or "triple" in modes:
        return f"multi-speaker ({len(scenes)} scenes)"
    return f"{kind} ({len(scenes)} scenes, {tiles} tiles)"
