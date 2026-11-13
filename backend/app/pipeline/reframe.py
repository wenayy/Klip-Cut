"""Face-aware reframing for podcast footage.

A podcast is shot in landscape for a landscape screen: two people sitting a
metre apart, framed wide, with a lot of table and wall between them. Every
generic vertical treatment does badly by it. Centre-cropping cuts one person
out. Letterboxing keeps both but shrinks them into a strip with two thirds of
the canvas left over. Neither is what a human editor would do -- an editor
crops to the PEOPLE, and if there are two of them, gives each their own frame.

This module works out where the people are, and hands `render` a shot-by-shot
set of crop windows to composite. It does not itself touch ffmpeg. A podcast
clip is not one layout for forty seconds: edited footage moves between singles,
two-shots and occasional wide/B-roll shots. The plan mirrors those cuts.

The whole design is about STABILITY. Detection output is jittery -- on real
footage the same seated speaker's box moves 200px between samples as they lean
and gesture -- and a crop wired straight to raw coordinates produces the
hand-held-camera look that reads as broken rather than as energetic. So every
number that reaches the renderer has been through smoothing, a dead zone, a
minimum shot length and a speed limit, in that order.
"""

import json
import os
import subprocess

# The detector's own asset. Small (230 KB) and loaded once per render.
MODEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "..", "assets", "models",
                     "face_detection_yunet_2023mar.onnx")

# Analysis runs on a downscaled copy of the source, built once per job and
# reused by every clip. This is the single biggest cost saving in the framing
# path: decoding 4K frames to run a 640-wide detector on them spends most of
# its time on pixels that are immediately thrown away, and seeking through a
# 4K file at 2 fps is several times slower than through a 480p one. Face
# coordinates are normalised, so a box found on the proxy is the same box on
# the master -- only the pixel sizes in the plan come from the real source.
ANALYSIS_WIDTH = 480
ANALYSIS_CRF = 30

# --- detection -------------------------------------------------------------

SAMPLE_FPS = 2.0          # detections per second of footage. Faces do not move
                          # meaningfully faster than this, and the smoothing
                          # below would throw away the extra precision anyway.
                          # At 25fps this is ~8% of the work of every-frame.
DETECT_WIDTH = 640        # detect on a downscale; boxes are scaled back up.
MIN_SCORE = 0.55          # below this the box is usually a hand or a poster.

# Increment whenever the JSON plan shape or its composition policy changes.
# The editor includes this in its on-disk cache name, so an old global stacked
# plan cannot survive a deployment and keep producing the bug that was fixed.
PLAN_VERSION = 5

# --- tracking --------------------------------------------------------------

# Two detections belong to the same person if their centres are within this
# fraction of frame width. Podcast speakers are seated and far apart, so this
# can be generous without ever merging the two of them.
TRACK_RADIUS = 0.18
MIN_TRACK_COVERAGE = 0.35  # a track seen in fewer than this fraction of samples
                           # is a passer-by, a reflection, or a poster face.

# Fraction of samples that must show two faces AT THE SAME INSTANT before the
# stacked layout is allowed.
#
# Two tracks is not the same claim as two people. A podcast edit that cuts
# between two camera angles on ONE speaker puts their face at a different screen
# position after each cut, and position-based tracking reads that as a second
# person -- so the stacked layout showed the same man twice, in two angles, side
# by side. Genuinely two-shot footage has both faces in nearly every frame
# (co-occurrence near 1.0); cut-based footage has them in none (near 0.0). The
# gap between those cases is enormous, so this threshold is not delicate.
MIN_CO_OCCURRENCE = 0.45

# A layout must survive this many consecutive samples before it is allowed to
# change the composition. At 2fps, five samples is 2.5 seconds: the minimum shot
# length used by the podcast references, and enough to stop a brief reaction or
# missed detection from turning into a frantic layout switch.
MIN_LAYOUT_SAMPLES = 5
MAX_PEOPLE = 4

# Two tiles whose source windows overlap by more than this share of the narrower
# one show the same person twice -- the "clashing frames" failure on panels of
# three or four people seated close together. Such a layout is not used.
CLASH_OVERLAP = 0.12

# A panel of three or more is cut like a multicam edit: the speaker alone and
# full-frame, the group between lines. Every panelist does not have to talk --
# a debate where two of four do the talking is still a debate.
GROUP_MIN_SPEECH = 0.20      # share of the scene the speaker shots must cover
GROUP_HOLD_SECONDS = 4.0     # a silence shorter than this keeps the last speaker
FEATURE_MIN_FACE = 0.14      # face height (share of frame) worth a close-up
FEATURE_DOMINANCE = 1.8      # face AREA over the next one before a shot is "of" them

# Speaker switching, as multicam podcast editors cut and as automated
# switchers are tuned: a close-up is earned by holding the floor, no shot is
# too short to read, the picture follows a new voice by a beat instead of
# anticipating it, and fast back-and-forth stays on the home shot rather than
# ping-ponging. Breaking these is what reads as constant zoom-in, zoom-out.
FLOOR_MIN = 3.0              # seconds of one person's talk that earns a close-up
SHOT_MIN = 2.5               # no shot, close-up or home, shorter than this
CUT_LAG = 0.2                # picture follows the new voice by ~6 frames (J-cut)
TAIL_HOLD = 0.35             # stay on a speaker a beat past their last word
SAME_TURN_GAP = 0.6          # pauses this short inside one person's talk
STEADY_SPREAD = 0.45         # of the crop: wander allowed before a close-up tracks

# When one diarized speaker owns most of a two-person scene and the source also
# gives one face more visual emphasis, make that person the main panel and keep
# the other as a reaction. Audio chooses the editorial mode; vision chooses the
# face because diarization labels do not identify image coordinates.
REACTION_SPEECH_SHARE = 0.72
REACTION_AREA_RATIO = 1.08

# Two people stay in one natural frame only when a true portrait crop can hold
# both faces with headroom. Otherwise pretending they fit just cuts off both.
TOGETHER_FILL = 0.86

# A jump larger than this, in one sample step, is a camera CUT rather than a
# person moving -- nobody crosses a fifth of the frame in half a second. Cuts
# must snap, not glide: smoothly sliding the crop across a hard cut is the one
# motion that looks unmistakably like a bug rather than like camerawork.
SNAP_DISTANCE = 0.20

# --- smoothing and shot discipline ----------------------------------------

SMOOTHING = 0.12          # EMA weight on each new sample. Low = heavy damping.
DEAD_ZONE = 0.035         # fraction of frame width the target must move before
                          # the crop follows at all. Kills the constant micro
                          # drift that makes a shot look unmoored.
MAX_SPEED = 0.10          # max crop travel per second, as a fraction of frame
                          # width. A hard ceiling on how fast a move can be,
                          # independent of how far the subject jumped.
MIN_SHOT = 1.4            # seconds. Once the crop settles it stays put at least
                          # this long, so a speaker leaning back and forward
                          # cannot start a rocking motion.

# --- composition -----------------------------------------------------------

# Headroom: a face box is the face only -- no hair above, no shoulders below.
# Framing on the box centre puts the eyeline dead centre, which looks like a
# passport photo. Real framing puts the eyes on the upper third.
HEAD_ROOM = 0.55          # of a face height, reserved above the box
EYE_LINE = 0.40           # target position of the face centre down the crop

# --- active speaker --------------------------------------------------------
#
# Who is TALKING, not who is biggest. Diarization already tells us when each
# speaker holds the floor, but it labels voices, not pixels: "speaker 1" is not
# a coordinate. Mouth movement is the bridge. A closed mouth is still; a talking
# one is not, and the face box gives a mouth region to measure.
#
# So: diarization gives WHO, lip motion gives WHERE, and the two are joined by
# voting -- for every diarized word, whichever track's mouth moved most votes
# for that speaker's position. The result is a track per speaker.
#
# This is deliberately not a neural active-speaker model. ASD networks cost a
# model download, a GPU-shaped dependency and seconds per clip to answer a
# question this answers from data the pipeline already has. If the vote is
# ambiguous the code falls back to the existing framing rather than guessing.

MOUTH_FPS = 7.0           # enough to see a mouth open and close; 7 samples a
                          # second is ~2x the syllable rate that matters here.
MOUTH_MIN = 0.055         # normalised frame difference below which a mouth is
                          # considered still (per 1/7s step).
MOUTH_MARGIN = 1.45       # the active mouth must beat the other by this factor
PUNCH_MIN = 2.4           # shortest lip-only speech span worth keeping; the
                          # close-up itself needs FLOOR_MIN (see _speaker_cuts)
CONVERSATION_MIN_SHARE = 0.12   # both participants need a real share before
                                # the camera starts cutting between them
MAX_ACTIVE_SECONDS = 240  # do not run the extra pass on very long clips; a
                          # clip is short by construction, this is a guard.

# Escape hatch for a deployment that would rather not pay the extra decode.
ACTIVE_SPEAKER = os.environ.get("CLIPPER_ACTIVE_SPEAKER", "1").strip().lower() \
    not in ("0", "false", "no", "off")


def probe_dimensions(video_path):
    """(width, height) of the first video stream, or (0, 0)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", video_path],
            check=True, capture_output=True, text=True, timeout=30,
        )
        stream = json.loads(out.stdout)["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except Exception:
        return 0, 0


def media_duration(video_path):
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


def analysis_clip(video_path, start=0.0, end=None):
    """A small copy of THIS CLIP for detection.

    Returns (path, time_offset): detection on the copy is relative to 0, so the
    caller adds `time_offset` to get source timestamps again.

    Scoped to the clip rather than the source on purpose. A two-hour podcast is
    exactly the footage this product is for, and transcoding the whole thing to
    480p before the first clip can be framed costs minutes for footage no clip
    will ever look at. A 30-60 second window costs under a second to seek and
    encode, and the copy is cached for the editor's preview request.
    """
    if not video_path or not os.path.exists(video_path):
        return video_path, 0.0
    try:
        start = max(0.0, float(start or 0.0))
    except (TypeError, ValueError):
        start = 0.0
    total = media_duration(video_path)
    try:
        end = float(end) if end is not None else (total or 0.0)
    except (TypeError, ValueError):
        end = total or 0.0
    if total and end >= total - 0.5 and start <= 0.5:
        return analysis_source(video_path), 0.0

    span = max(0.5, end - start)
    base, _ = os.path.splitext(video_path)
    cached = f"{base}.clip_{int(start * 10)}_{int(end * 10)}.mp4"
    try:
        if (os.path.exists(cached)
                and os.path.getmtime(cached) >= os.path.getmtime(video_path)
                and os.path.getsize(cached) > 512):
            return cached, start
    except OSError:
        return video_path, 0.0

    tmp = cached + ".tmp.mp4"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}",
             "-t", f"{span:.3f}", "-i", video_path, "-an",
             "-vf", f"scale={ANALYSIS_WIDTH}:-2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(ANALYSIS_CRF),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0 or not os.path.exists(tmp):
            return video_path, 0.0
        os.replace(tmp, cached)
        return cached, start
    except (subprocess.SubprocessError, OSError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return video_path, 0.0


def analysis_source(video_path):
    """A small, seek-friendly copy of the whole source, cached on disk.

    Kept for whole-video analysis (silent highlight picking); per-clip framing
    uses `analysis_clip` so a long source is never transcoded in full.
    """
    if not video_path or not os.path.exists(video_path):
        return video_path
    base, _ = os.path.splitext(video_path)
    cached = base + ".analysis.mp4"
    try:
        if (os.path.exists(cached)
                and os.path.getmtime(cached) >= os.path.getmtime(video_path)
                and os.path.getsize(cached) > 1024):
            return cached
    except OSError:
        return video_path

    tmp = cached + ".tmp.mp4"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", video_path,
             "-an", "-vf", f"scale={ANALYSIS_WIDTH}:-2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(ANALYSIS_CRF),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp],
            capture_output=True, text=True, timeout=900,
        )
        if proc.returncode != 0 or not os.path.exists(tmp):
            return video_path
        os.replace(tmp, cached)
        return cached
    except (subprocess.SubprocessError, OSError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return video_path


def _detector(width, height):
    """YuNet, or None when the model is missing.

    Missing model is not an error: the caller falls back to a fixed frame, so a
    checkout without the asset still renders -- just without the smart crop.
    """
    try:
        import cv2
    except ImportError:
        return None
    if not os.path.exists(MODEL):
        return None
    try:
        det = cv2.FaceDetectorYN.create(MODEL, "", (width, height),
                                        score_threshold=MIN_SCORE)
        det.setInputSize((width, height))
        return det
    except Exception:
        return None


def detect_faces(video_path, start=0.0, end=None, sample_fps=SAMPLE_FPS):
    """Sample the clip and return [(t, [(cx, cy, w, h, score), ...]), ...].

    Coordinates are fractions of frame width/height, so everything downstream is
    resolution-independent and the same numbers work for a proxy or a master.
    """
    try:
        import cv2
    except ImportError:
        return [], (0, 0)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return [], (0, 0)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if not src_w or not src_h:
        cap.release()
        return [], (0, 0)

    # Detect on a downscale. YuNet is scale-sensitive in cost but not much in
    # accuracy at these face sizes, and this is ~9x less pixel work at 1080p.
    scale = min(1.0, DETECT_WIDTH / src_w)
    det_w, det_h = int(src_w * scale), int(src_h * scale)
    det = _detector(det_w, det_h)
    if det is None:
        cap.release()
        return [], (src_w, src_h)

    if end is None:
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        end = frames / fps if frames else start + 60.0

    out = []
    step = 1.0 / sample_fps
    t = start
    while t < end:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if not ok:
            break
        if scale < 1.0:
            frame = cv2.resize(frame, (det_w, det_h))
        try:
            _, faces = det.detect(frame)
        except Exception:
            faces = None
        found = []
        if faces is not None:
            for f in faces:
                # float() on every component, not just the score. OpenCV hands
                # back numpy scalars, which propagate all the way into the plan
                # and format fine into an ffmpeg expression -- but are not JSON
                # serialisable, so the editor's preview endpoint 500s on a plan
                # the renderer was perfectly happy with.
                x, y, w, h = (float(f[0]), float(f[1]), float(f[2]), float(f[3]))
                found.append((
                    (x + w / 2) / det_w, (y + h / 2) / det_h,
                    w / det_w, h / det_h, float(f[-1]),
                ))
        out.append((t, found))
        t += step
    cap.release()
    return out, (src_w, src_h)


def build_tracks(samples):
    """Group per-frame detections into per-person tracks.

    Greedy nearest-centre association. Sufficient because the subjects are
    seated and metres apart -- the hard cases this would fail on (crossing
    paths, occlusion, similar-looking people adjacent) do not happen at a
    podcast table.
    """
    tracks = []
    total = len(samples) or 1

    for t, faces in samples:
        # Biggest first: the near speaker should claim their track before a
        # smaller background face can steal it.
        for cx, cy, w, h, score in sorted(faces, key=lambda f: -f[2] * f[3]):
            best, best_d = None, TRACK_RADIUS
            for tr in tracks:
                if tr["last_t"] == t:
                    continue                    # already matched this frame
                lx, ly = tr["points"][-1][1], tr["points"][-1][2]
                d = ((cx - lx) ** 2 + (cy - ly) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = tr, d
            if best is None:
                tracks.append({"points": [(t, cx, cy, w, h, score)], "last_t": t})
            else:
                best["points"].append((t, cx, cy, w, h, score))
                best["last_t"] = t

    # Drop anything too intermittent to be a participant.
    keep = [tr for tr in tracks if len(tr["points"]) / total >= MIN_TRACK_COVERAGE]
    # Left to right, so "speaker 1" is stable between renders of the same clip.
    keep.sort(key=lambda tr: sum(p[1] for p in tr["points"]) / len(tr["points"]))
    return keep


def smooth(points, key=1):
    """EMA + dead zone + speed limit + minimum shot, applied in that order.

    Each stage exists for a failure seen in the raw signal:
      EMA          -- per-sample detector noise
      dead zone    -- slow drift when the subject is effectively still
      min shot     -- oscillation when they rock between two positions
      speed limit  -- a lurch when detection briefly lands on something else
    """
    if not points:
        return []
    out = []
    cur = points[0][key]
    committed = cur
    last_move_t = points[0][0]

    prev_raw = points[0][key]

    for i, p in enumerate(points):
        t, v = p[0], p[key]

        # A cut is not motion, and none of the damping below should apply to it.
        # Every stage of this function exists to stop the crop reacting to a
        # subject who moved; when the SHOT changed instead, the crop has to
        # arrive at the new framing immediately or it spends a second sliding
        # across a frame the viewer already sees as a new angle.
        if abs(v - prev_raw) > SNAP_DISTANCE:
            # Two points a hair apart in time: the interpolation in `expr` then
            # renders this as a step rather than a ramp, without that function
            # needing to know cuts exist.
            if out:
                out.append((max(t - 0.04, out[-1][0] + 1e-3), committed))
            cur = committed = v
            last_move_t = t
            out.append((t, committed))
            prev_raw = v
            continue
        prev_raw = v

        cur += (v - cur) * SMOOTHING                     # EMA

        want = cur
        moved = abs(want - committed)
        held = t - last_move_t
        if moved > DEAD_ZONE and held >= MIN_SHOT:       # dead zone + min shot
            dt = t - (out[-1][0] if out else t)
            limit = MAX_SPEED * max(dt, 1e-3)            # speed limit
            step = max(-limit, min(limit, want - committed))
            committed += step
            last_move_t = t
        out.append((t, committed))
    return out


def co_occurrence(samples):
    """Fraction of sampled frames showing two or more faces at once.

    The question this answers is "are there two people in this shot", which is
    not the same question as "did tracking produce two tracks" -- see
    MIN_CO_OCCURRENCE.
    """
    seen = [s for s in samples if s[1]]
    if not seen:
        return 0.0
    return sum(1 for _, faces in seen if len(faces) >= 2) / len(seen)


def primary_timeline(samples):
    """The dominant face in each sample: who this shot is of, cut by cut.

    Deliberately not a track. When an edit cuts between angles the subject is
    the same person at a new position, and following the largest face per frame
    keeps them framed across the cut -- where following one positional track
    would hold on empty chair for every second shot.
    """
    out = []
    for t, faces in samples:
        if not faces:
            continue
        cx, cy, w, h, score = max(faces, key=lambda f: f[2] * f[3])
        out.append((t, cx, cy, w, h, score))
    return out


def _clamp_window(centre, size, extent=1.0):
    """Keep a window of `size` fully inside [0, extent]."""
    half = size / 2
    return min(max(centre, half), extent - half) if size < extent else extent / 2


def _clamp_region(centre, size, low, high, extent):
    """Keep a crop inside a participant's horizontal region where possible."""
    if high <= low:
        return _clamp_window(centre, size, extent)
    if size >= high - low:
        return _clamp_window((low + high) / 2, size, extent)
    half = size / 2
    return min(max(centre, low + half), high - half)


def portrait_window(track, src_w, src_h, out_w, out_h):
    """Crop keyframes framing ONE speaker for a vertical canvas.

    Returns (win_w, win_h, [(t, cx, cy), ...]) in source-pixel units.

    The window is as tall as the source allows, because the extra height is
    what turns a face into a person -- shoulders, hands, the microphone. A
    tighter crop reads as a webcam.
    """
    aspect = out_w / out_h
    win_h = src_h
    win_w = win_h * aspect
    if win_w > src_w:                       # source too narrow for a full-height
        win_w = src_w                       # portrait window; take what we have
        win_h = win_w / aspect

    xs = smooth(track["points"], key=1)
    ys = smooth(track["points"], key=2)
    face_h = sum(p[4] for p in track["points"]) / len(track["points"]) * src_h

    keys = []
    for (t, cx), (_, cy) in zip(xs, ys):
        px, py = cx * src_w, cy * src_h
        # Sit the face on the eye line rather than dead centre: for the face to
        # land EYE_LINE down the window, the window's centre has to be BELOW the
        # face by the remaining fraction. Then refuse to go so low that the top
        # of the head leaves the frame.
        py = py + (0.5 - EYE_LINE) * win_h
        py = max(py, face_h * (0.5 + HEAD_ROOM))
        keys.append((t,
                     _clamp_window(px, win_w, src_w),
                     _clamp_window(py, win_h, src_h)))
    return win_w, win_h, keys


def stacked_windows(tracks, src_w, src_h, out_w, out_h):
    """Two crop windows, one per speaker, for a vertically stacked layout.

    This is the shape podcast clips actually take on the platforms, and the
    reason is not aesthetic: two people a metre apart in a landscape frame
    cannot both be large in one vertical crop. Given each of them their own
    tile, both are large. The cost is that the two are no longer in a shared
    space -- which viewers accept completely, because every podcast clip they
    have ever seen is cut this way.
    """
    tile_h = out_h / 2
    aspect = out_w / tile_h                 # each tile is wide, not tall

    # Each speaker gets at most their half of the frame, or the crop windows
    # would overlap and show the same person twice.
    win_w = min(src_w / 2, src_h * aspect)
    win_h = win_w / aspect
    if win_h > src_h:
        win_h = src_h
        win_w = win_h * aspect

    out = []
    for tr in tracks[:2]:
        xs = smooth(tr["points"], key=1)
        ys = smooth(tr["points"], key=2)
        face_h = sum(p[4] for p in tr["points"]) / len(tr["points"]) * src_h
        keys = []
        for (t, cx), (_, cy) in zip(xs, ys):
            px, py = cx * src_w, cy * src_h
            py = max(py, face_h * (0.5 + HEAD_ROOM))
            keys.append((t,
                         _clamp_window(px, win_w, src_w),
                         _clamp_window(py, win_h, src_h)))
        out.append(keys)
    return win_w, win_h, out


def _runs(values):
    """[(start_index, end_index_exclusive, value), ...]."""
    if not values:
        return []
    out = []
    start = 0
    for i in range(1, len(values)):
        if values[i] != values[start]:
            out.append((start, i, values[start]))
            start = i
    out.append((start, len(values), values[start]))
    return out


def stabilize_layouts(counts, minimum=MIN_LAYOUT_SAMPLES):
    """Remove brief face-count flickers without delaying real shot changes.

    A detector missing one of two faces for half a second must not make the
    canvas jump split -> single -> split. Likewise, one false second face in the
    middle of a single must not duplicate the speaker. Runs shorter than the
    minimum inherit the surrounding layout; at an edge they inherit the only
    neighbour. The loop repeats because replacing one run may join two others.
    """
    values = [max(0, min(int(v), MAX_PEOPLE)) for v in counts]
    if not values:
        return []

    for _ in range(4):
        runs = _runs(values)
        changed = False
        for n, (start, end, value) in enumerate(runs):
            if end - start >= minimum or len(runs) == 1:
                continue
            left = runs[n - 1] if n else None
            right = runs[n + 1] if n + 1 < len(runs) else None
            if left and right and left[2] == right[2]:
                replacement = left[2]
            elif left and right:
                left_len = left[1] - left[0]
                right_len = right[1] - right[0]
                replacement = left[2] if left_len >= right_len else right[2]
            elif left:
                replacement = left[2]
            elif right:
                replacement = right[2]
            else:
                continue
            values[start:end] = [replacement] * (end - start)
            changed = True
        if not changed:
            break
    return values


def _track_mean_x(track):
    return sum(p[1] for p in track["points"]) / len(track["points"])


def _track_area(track):
    return sum(p[3] * p[4] for p in track["points"]) / len(track["points"])


def _track_mean_width(track):
    return sum(p[3] for p in track["points"]) / len(track["points"])


def _speaker_share(words, start, end):
    """How much of this scene belongs to its most frequent diarized speaker."""
    totals = {}
    for word in words or []:
        speaker = word.get("speaker")
        if speaker is None:
            continue
        overlap = max(0.0, min(end, word["end"]) - max(start, word["start"]))
        if overlap:
            totals[speaker] = totals.get(speaker, 0.0) + overlap
    total = sum(totals.values())
    return max(totals.values()) / total if total else 0.0


def _layout_rects(count, mode=None):
    """Tile rectangles as fractions of the final canvas.

    Three people use one featured full-width tile and two supporting tiles. Four
    use a balanced grid. These are intentionally a small set of familiar podcast
    compositions rather than a novel layout for every face count.
    """
    if count <= 1:
        return [(0.0, 0.0, 1.0, 1.0)]
    if count == 2:
        if mode == "reaction":
            return [(0.0, 0.0, 1.0, 0.62), (0.0, 0.62, 1.0, 0.38)]
        return [(0.0, 0.0, 1.0, 0.5), (0.0, 0.5, 1.0, 0.5)]
    if count == 3:
        return [
            (0.0, 0.0, 1.0, 0.5),
            (0.0, 0.5, 0.5, 0.5),
            (0.5, 0.5, 0.5, 0.5),
        ]
    return [
        (0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 0.5, 0.5),
        (0.0, 0.5, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5),
    ]


def _participant_regions(tracks, src_w):
    """A horizontal ownership region per simultaneously visible participant."""
    ordered = sorted(tracks, key=_track_mean_x)
    centres = [_track_mean_x(tr) * src_w for tr in ordered]
    regions = {}
    for i, (track, centre) in enumerate(zip(ordered, centres)):
        low = 0.0 if i == 0 else (centres[i - 1] + centre) / 2
        high = float(src_w) if i == len(ordered) - 1 else (centre + centres[i + 1]) / 2
        regions[id(track)] = (low, high)
    return regions


def _together_tile(tracks, start, end, src_w, src_h, out_w, out_h):
    """One natural two-person frame when a real portrait crop can contain both."""
    aspect = out_w / max(out_h, 1)
    win_w = min(float(src_w), src_h * aspect)
    win_h = win_w / aspect
    left = min(_track_mean_x(track) - _track_mean_width(track) * 0.7
               for track in tracks) * src_w
    right = max(_track_mean_x(track) + _track_mean_width(track) * 0.7
                for track in tracks) * src_w
    if right - left > win_w * TOGETHER_FILL:
        return None

    centre_x = _clamp_window((left + right) / 2, win_w, src_w)
    # A portrait crop is normally source-height. Keeping its vertical centre
    # steady preserves the table, microphones and body language that make a
    # genuine two-person frame worth choosing over a manufactured split.
    centre_y = _clamp_window(src_h / 2, win_h, src_h)
    return {
        "rect": (0.0, 0.0, 1.0, 1.0),
        "win": (int(win_w), int(win_h)),
        "keys": [(start, centre_x, centre_y), (end, centre_x, centre_y)],
    }


def tile_window(track, rect, region, src_w, src_h, out_w, out_h):
    """Crop one participant into one output tile.

    The horizontal ownership region is the important guardrail: even when two
    crop windows are drawn from the same source frame, one cannot drift over and
    show the neighbouring participant again. This is what prevents duplicated
    faces and edge fragments in split layouts.
    """
    _, _, rect_w, rect_h = rect
    tile_w = out_w * rect_w
    tile_h = out_h * rect_h
    aspect = tile_w / max(tile_h, 1)

    face_h = sum(p[4] for p in track["points"]) / len(track["points"]) * src_h
    face_w = sum(p[3] for p in track["points"]) / len(track["points"]) * src_w
    low, high = region
    desired_w = min(src_w, src_h * aspect)
    # Do not cross the midpoint between participants. A little more height than
    # the face keeps shoulders, hands and microphone in frame instead of making
    # the result look like a webcam crop.
    region_w = max(2.0, high - low)
    win_w = min(desired_w, region_w)
    # A WIDE tile has no natural crop size: at 1080x960 the strongest window the
    # source allows is most of the source height, which framed each seated
    # speaker as a small figure above a landscape of table. An editor picks the
    # size from the subject, so derive it from the face: ~3.5 face-widths
    # across is head, shoulders and a microphone. Tall tiles keep the
    # full-height crop -- there the aspect already decides the shot.
    if aspect > 1.05:
        subject_w = max(face_w * 4.6, face_h * 3.3)
        win_w = min(win_w, subject_w)
        win_h = win_w / aspect
    else:
        # A tall tile on a landscape source defaults to a full-height crop,
        # which frames a seated person as a small figure at a long table. An
        # editor frames head and shoulders. Size the window from the face, then
        # refuse to go below a resolution floor: a crop narrower than ~0.55 of
        # the output width is more upscale than detail, and softness in the
        # subject is worse than a little extra room around them.
        subject_h = face_h * 3.6
        win_h = min(src_h, subject_h)
        win_w = win_h * aspect
        floor_w = min(src_w, out_w * 0.55)
        if win_w < floor_w:
            win_w = floor_w
            win_h = win_w / aspect
    if win_h > src_h:
        win_h = src_h
        win_w = min(src_w, win_h * aspect)
    min_h = min(src_h, face_h * 2.5)
    if win_h < min_h:
        win_w = min(src_w, region_w, min_h * aspect)
        win_h = win_w / aspect
    if win_h > src_h:
        win_h = src_h
        win_w = min(src_w, win_h * aspect)

    xs = smooth(track["points"], key=1)
    ys = smooth(track["points"], key=2)
    keys = []
    for (t, cx), (_, cy) in zip(xs, ys):
        px, py = cx * src_w, cy * src_h
        py += (0.5 - EYE_LINE) * win_h
        # Headroom is a hard promise, not a side effect of the eye line. On a
        # tight crop the eye-line rule alone can put the crown of the head
        # against the top edge, which reads as a mistake on every platform.
        face_top = cy * src_h - face_h / 2
        # 0.40 of a face height above the detector's box: the box starts at the
        # brow on most footage, and hair is above that. Under-reserving here is
        # what put a crown against the top edge on tight crops.
        head_top = face_top - face_h * 0.40
        ceiling = head_top - win_h * 0.08 + win_h / 2
        py = min(py, ceiling)
        py = max(py, face_h * (0.5 + HEAD_ROOM))
        keys.append((
            t,
            _clamp_region(px, win_w, low, high, src_w),
            _clamp_window(py, win_h, src_h),
        ))
    return {
        "rect": rect,
        "win": (int(win_w), int(win_h)),
        "keys": keys,
    }


def lip_activity(video_path, start, end, tracks, sample_fps=MOUTH_FPS):
    """Per-track mouth movement over the clip: [(t, {track_index: motion}), ...].

    `motion` is the frame difference inside the mouth region, normalised by the
    region's brightness so a dark room and a bright studio both produce
    comparable numbers. The face detector runs at analysis resolution, so this
    is one pass over the proxy rather than the master.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []
    if not tracks:
        return []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    det = _detector(src_w, src_h)
    if det is None:
        cap.release()
        return []

    centres = [(_track_mean_x(tr), sum(p[2] for p in tr["points"]) /
                len(tr["points"])) for tr in tracks]
    previous = {}
    rows = []
    step = 1.0 / max(sample_fps, 0.1)
    t = max(0.0, start)
    while t < end:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if not ok:
            break
        try:
            _, faces = det.detect(frame)
        except Exception:
            faces = None
        found = {}
        for f in (faces if faces is not None else []):
            fx, fy, fw, fh = (float(f[0]), float(f[1]), float(f[2]), float(f[3]))
            cx, cy = (fx + fw / 2) / src_w, (fy + fh / 2) / src_h
            best, best_d = None, TRACK_RADIUS * 1.4
            for i, (tx, ty) in enumerate(centres):
                d = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = i, d
            if best is None:
                continue
            # Lower-middle of the face box: the mouth. Cropping generously
            # includes the jaw, which moves with speech too, and is more
            # robust than trying to isolate lips at this resolution.
            mx = int(fx + fw * 0.22)
            my = int(fy + fh * 0.55)
            mw = max(2, int(fw * 0.56))
            mh = max(2, int(fh * 0.38))
            mx = max(0, min(mx, src_w - mw))
            my = max(0, min(my, src_h - mh))
            patch = frame[my:my + mh, mx:mx + mw]
            if patch.size == 0:
                continue
            gray = cv2.resize(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), (32, 16))
            gray = cv2.GaussianBlur(gray, (3, 3), 0).astype(np.float32)
            found[best] = gray

        row = {}
        for i, gray in found.items():
            prev = previous.get(i)
            if prev is not None:
                diff = float(np.abs(gray - prev).mean())
                level = float(gray.mean()) + 8.0
                row[i] = diff / level
            previous[i] = gray
        if row:
            rows.append((round(t, 3), row))
        t += step

    cap.release()
    return rows


def _mean_motion(rows, index, start, end):
    values = [row[index] for t, row in rows
              if start <= t <= end and index in row]
    return sum(values) / len(values) if values else 0.0


def _merge(entries, gap=0.65):
    """[(start, end, value)] with entries closer than `gap` joined."""
    out = []
    for start, end, value in sorted(entries):
        if out and out[-1][2] == value and start - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], end), value)
        else:
            out.append((start, end, value))
    return out


def speaker_spans(tracks, rows, words, start, end):
    """Where each speaker is, as [(start, end, track_mean_x), ...].

    With diarization, words carry a speaker label; mouth motion maps each label
    to a position. Without it, the louder mouth is simply taken as the speaker.
    Returns source timestamps.
    """
    if not tracks or not rows:
        return []

    mapping = {}
    labelled = [w for w in (words or [])
                if w.get("speaker") is not None
                and w.get("end", 0) - w.get("start", 0) >= 0.12]
    if labelled:
        votes = {}
        for word in labelled:
            motions = [_mean_motion(rows, i, word["start"], word["end"])
                       for i in range(len(tracks))]
            if len(motions) < 2:
                continue
            order = sorted(range(len(motions)), key=lambda i: -motions[i])
            best, second = order[0], order[1]
            if (motions[best] >= MOUTH_MIN
                    and motions[best] >= motions[second] * MOUTH_MARGIN):
                votes.setdefault(word["speaker"], [0] * len(tracks))
                votes[word["speaker"]][best] += 1

        for speaker, tally in votes.items():
            total = sum(tally)
            best = max(range(len(tally)), key=lambda i: tally[i])
            # A split vote means the mapping is a coin flip, and a wrong
            # speaker cuts the clip to the person who is not talking. Demand a
            # clear winner instead.
            if total >= 3 and tally[best] / total >= 0.6:
                mapping[speaker] = best

        if mapping:
            entries = []
            for word in labelled:
                index = mapping.get(word["speaker"])
                if index is None:
                    continue
                entries.append((max(start, word["start"]),
                                min(end, word["end"]), index))
            spans = _merge(entries, gap=0.7)
            return [(s, e, _track_mean_x(tracks[i]))
                    for s, e, i in spans if e - s >= 1.2]

    # No usable diarization: follow whichever mouth is moving.
    entries = []
    for t, row in rows:
        if len(row) < 2:
            continue
        order = sorted(row.items(), key=lambda kv: -kv[1])
        (best, value), (_, second) = order[0], order[1]
        if value >= MOUTH_MIN and value >= second * MOUTH_MARGIN:
            entries.append((t, t + 1.0 / MOUTH_FPS, best))
    spans = _merge(entries, gap=0.6)
    return [(s, e, _track_mean_x(tracks[i]))
            for s, e, i in spans if e - s >= PUNCH_MIN]


def _spoken_shares(tracks, spans):
    """Seconds of speech attributed to each track, by position."""
    shares = [0.0] * len(tracks)
    for s, e, cx in spans or []:
        index = min(range(len(tracks)),
                    key=lambda i: abs(_track_mean_x(tracks[i]) - cx))
        shares[index] += max(0.0, e - s)
    return shares


def _turns(tracks, spans):
    """Speech spans as (start, end, track index); spans no face owns are dropped."""
    out = []
    for s, e, cx in spans or []:
        index = min(range(len(tracks)), key=lambda i: abs(
            _track_mean_x(tracks[i]) - cx))
        if abs(_track_mean_x(tracks[index]) - cx) <= 0.08:
            out.append((s, e, index))
    return out


def _enforce_min_hold(shots):
    """Fold every shot shorter than SHOT_MIN into a neighbour.

    A flash of the home shot between two close-ups becomes the earlier speaker
    held through the gap; a close-up too short to read stays on the home shot.
    """
    shots = [list(x) for x in shots if x[1] - x[0] > 1e-3]
    while len(shots) > 1:
        k = min(range(len(shots)), key=lambda j: shots[j][1] - shots[j][0])
        cur = shots[k]
        if cur[1] - cur[0] >= SHOT_MIN:
            break
        prev = shots[k - 1] if k > 0 else None
        nxt = shots[k + 1] if k + 1 < len(shots) else None
        if cur[2] is None and prev is not None and prev[2] is not None:
            target = prev
        elif cur[2] is not None and prev is not None and prev[2] is None:
            target = prev
        elif cur[2] is not None and nxt is not None and nxt[2] is None:
            target = nxt
        else:
            target = prev or nxt
        if target is prev:
            prev[1] = cur[1]
        else:
            nxt[0] = cur[0]
        del shots[k]
        joined = [shots[0]]
        for x in shots[1:]:
            if x[2] == joined[-1][2]:
                joined[-1][1] = x[1]
            else:
                joined.append(x)
        shots = joined
    return shots


def _speaker_cuts(turns, start, end, hold=GROUP_HOLD_SECONDS):
    """Who is on screen when: [(start, end, track index or None)] over the scene.

    None is the home shot (the two-shot, or the wide of a panel). Only a turn of
    FLOOR_MIN earns a close-up, so quick back-and-forth and one-word
    interjections stay on the home shot instead of ping-ponging; a pause shorter
    than `hold` keeps the last speaker on screen; and nothing is shorter than
    SHOT_MIN.
    """
    joined = []
    for s, e, index in sorted(turns):
        if joined and joined[-1][2] == index and s - joined[-1][1] <= SAME_TURN_GAP:
            joined[-1] = (joined[-1][0], max(joined[-1][1], e), index)
        else:
            joined.append((s, e, index))

    shots = []
    for s, e, index in joined:
        if e - s < FLOOR_MIN:
            continue
        s, e = max(start, s + CUT_LAG), min(end, e + TAIL_HOLD)
        if e <= s:
            continue
        if shots and shots[-1][2] == index and s - shots[-1][1] < hold:
            shots[-1][1] = max(shots[-1][1], e)       # same person again: one shot
            continue
        if shots:
            prev = shots[-1]
            if s - prev[1] >= hold:
                shots.append([prev[1], s, None])      # a long lull: back to home
            else:
                prev[1] = s                           # crosstalk or a short pause
        elif s - start >= SHOT_MIN:
            shots.append([start, s, None])
        else:
            s = start                                 # open on the speaker
        shots.append([s, e, index])
    if not shots:
        return [(start, end, None)]
    if end - shots[-1][1] >= hold:
        shots.append([shots[-1][1], end, None])
    else:
        shots[-1][1] = end
    return [tuple(x) for x in _enforce_min_hold(shots)]


def _steady(tile, start, end):
    """Lock a close-up off like a tripod shot while its subject sits still.

    A seated speaker barely moves, and a crop that re-centres on every head turn
    reads as someone fiddling with the camera. The shot holds the subject's
    typical position; only real movement (an interquartile wander past
    STEADY_SPREAD of the crop) keeps the smoothed tracking.
    """
    keys = tile.get("keys") or []
    inside = [k for k in keys if start - 1e-6 <= k[0] <= end + 1e-6] or keys
    if not inside:
        return tile
    xs = sorted(k[1] for k in inside)
    ys = sorted(k[2] for k in inside)

    def q(values, p):
        return values[min(len(values) - 1, int(p * len(values)))]

    win_w, win_h = tile.get("win", (0, 0))
    if (q(xs, 0.75) - q(xs, 0.25) > win_w * STEADY_SPREAD
            or q(ys, 0.75) - q(ys, 0.25) > win_h * STEADY_SPREAD):
        return tile
    x, y = xs[len(xs) // 2], ys[len(ys) // 2]
    return {**tile, "keys": [(start, x, y), (end, x, y)]}


def _punch_variants(tracks, regions, rects, mode, base, spans,
                    start, end, src_w, src_h, out_w, out_h):
    """Cut the camera to whoever is speaking, returning scenes or None.

    A podcast is a conversation, and a conversation has a subject per sentence.
    Holding a two-shot for forty seconds is not wrong so much as flat; the
    editors people compare against cut to the speaker. This returns the
    composition split into single-speaker scenes, or None to keep the base
    layout untouched. The switching rules are `_speaker_cuts`.
    """
    if not spans or len(tracks) < 2:
        return None
    turns = _turns(tracks, spans)
    if not turns:
        return None

    duration = max(end - start, 1e-3)
    shares = [0.0] * len(tracks)
    for s, e, index in turns:
        shares[index] += max(0.0, min(e, end) - max(s, start))
    if min(shares) / duration < CONVERSATION_MIN_SHARE:
        # One person owns the scene: that is a monologue with a listener, and
        # cutting away from the reaction shot is the wrong edit.
        return None

    shots = _speaker_cuts(turns, start, end)
    if all(index is None for _, _, index in shots):
        return None
    scenes = []
    for s, e, index in shots:
        if index is None:
            scenes.append(_gap_scene(tracks, regions, rects, mode, s, e,
                                     src_w, src_h, out_w, out_h))
            continue
        subject = tracks[index]
        tile = tile_window(subject, (0.0, 0.0, 1.0, 1.0), regions[id(subject)],
                           src_w, src_h, out_w, out_h)
        scenes.append({"start": s, "end": e, "mode": "single",
                       "tiles": [_steady(tile, s, e)]})
    # A scene list that is still one piece is not worth the layout churn.
    if len(scenes) <= 1:
        return None
    return scenes


def _window_span(tile):
    """(left, right) of a tile's source window, at its average position."""
    keys = tile.get("keys") or []
    if not keys:
        return None
    centre = sum(k[1] for k in keys) / len(keys)
    half = tile["win"][0] / 2.0
    return centre - half, centre + half


def tiles_clash(tiles):
    """True when two tiles crop overlapping source, i.e. a face appears twice."""
    spans = [s for s in (_window_span(t) for t in tiles) if s]
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            (a0, a1), (b0, b1) = spans[i], spans[j]
            overlap = min(a1, b1) - max(a0, b0)
            narrower = min(a1 - a0, b1 - b0)
            if narrower > 0 and overlap / narrower > CLASH_OVERLAP:
                return True
    return False


def _featured_scene(samples, start, end, src_w, src_h, out_w, out_h):
    """A group moment with no clear speaker: the panel's home shot.

    Decided from whole tracks, never per sample. Following the biggest face in
    each half-second sample hopped between people whose faces are about the
    same size, and every hop was a jump across the table. One face stands for
    the shot only when it clearly dominates -- the close-up feed of a
    multi-camera layout, someone right by the lens. Otherwise the group is
    shown whole, which is also what an editor goes to for crosstalk.
    """
    inside = [x for x in samples if start <= x[0] <= end] or samples
    tracks = sorted(build_tracks(inside), key=_track_area, reverse=True)
    if tracks:
        lead = tracks[0]
        face_h = sum(p[4] for p in lead["points"]) / len(lead["points"])
        dominant = (len(tracks) == 1 or _track_area(lead)
                    >= FEATURE_DOMINANCE * _track_area(tracks[1]))
        if face_h >= FEATURE_MIN_FACE and dominant:
            return _speaker_single(lead, start, end, src_w, src_h, out_w, out_h)
    return _group_scene(start, end)


def _group_scene(start, end):
    """Everyone at once: the whole frame over a blurred copy of itself.

    Never tiles for three or more. Panels are seated shoulder to shoulder and
    are often already a multi-camera layout in the source, so a crop per person
    catches a neighbour or a seam between camera feeds, and the same person can
    sit in two feeds -- the duplicated faces. The whole frame shows each of
    them once.
    """
    return {"start": start, "end": end, "mode": "wide", "tiles": []}


def _speaker_single(track, start, end, src_w, src_h, out_w, out_h):
    """The speaker alone, full frame -- framed on them, not on their region.

    In a single there is no second tile to collide with, so the ownership
    region that guards split layouts only pushed the crop off-centre when the
    person sat near a neighbour. Whoever is at the edge of the frame is just
    the room.
    """
    tile = tile_window(track, (0.0, 0.0, 1.0, 1.0), (0.0, float(src_w)),
                       src_w, src_h, out_w, out_h)
    return {"start": start, "end": end, "mode": "single",
            "tiles": [_steady(tile, start, end)]}


def _group_variants(tracks, samples, spans, start, end, src_w, src_h, out_w, out_h):
    """A panel cut to its speakers, returning scenes or None.

    The two-person version (`_punch_variants`) demands that both people talk,
    which never holds for a panel of three or four. Here it is enough that the
    speaker shots cover a real share of the scene; everything between them is
    the home shot -- ONE home shot for the whole scene, so going back to it
    always looks the same.
    """
    turns = _turns(tracks, spans)
    if not turns:
        return None
    shots = _speaker_cuts(turns, start, end)
    covered = sum(e - s for s, e, index in shots if index is not None)
    if not covered or covered / max(end - start, 1e-3) < GROUP_MIN_SPEECH:
        return None

    home, scenes = None, []
    for s, e, index in shots:
        if index is not None:
            scenes.append(_speaker_single(tracks[index], s, e,
                                          src_w, src_h, out_w, out_h))
            continue
        if home is None:
            home = _featured_scene(samples, start, end, src_w, src_h, out_w, out_h)
        # Copies: plan() re-times every tile's keys in place, once per scene.
        scenes.append({**home, "start": s, "end": e,
                       "tiles": [dict(t) for t in home["tiles"]]})
    return scenes


def _gap_scene(tracks, regions, rects, mode, start, end,
               src_w, src_h, out_w, out_h):
    """The base two-shot layout for the stretches between punches."""
    if mode == "together":
        tile = _together_tile(tracks, start, end, src_w, src_h, out_w, out_h)
        if tile:
            return {"start": start, "end": end, "mode": "together", "tiles": [tile]}
        # The pair did not fit a portrait window after all; fall back to the
        # split geometry rather than returning a scene with no tiles.
        mode = "split"
        rects = _layout_rects(2, "split")
    tiles = [
        tile_window(track, rect, regions[id(track)], src_w, src_h, out_w, out_h)
        for track, rect in zip(tracks, rects)
    ]
    return {"start": start, "end": end, "mode": mode, "tiles": tiles}


# Where the caption block sits for each composition, as a fraction of frame
# height. A single is a lower third; a split puts text in the seam; a reaction
# puts it inside the lower reaction tile's chest area.
CAPTION_POS_FOR_MODE = {
    "single": 0.76,
    "split": 0.48,
    "reaction": 0.60,
    "together": 0.56,
    "triple": 0.48,
    "grid": 0.48,
    "wide": 0.55,
}


def caption_positions(plan, clip_start=0.0, segments=None):
    """Caption placements in OUTPUT time, or [] when the plan has none.

    `segments` are the pause-tightening spans. A scene boundary expressed in
    source time has to move with the cuts, exactly as the crop keyframes do, or
    the caption jumps a second before the picture does.
    """
    caps = (plan or {}).get("captions") or []
    if not caps:
        return []
    out = []
    for entry in caps:
        start = float(entry.get("start", 0.0))
        end = float(entry.get("end", start))
        if segments:
            from app.pipeline import pacing
            t0 = pacing.remap_time(clip_start + start, segments)
            t1 = pacing.remap_time(clip_start + end, segments)
        else:
            t0, t1 = start, end
        if t1 - t0 <= 0.02:
            continue
        out.append({"start": round(t0, 3), "end": round(t1, 3),
                    "pos": float(entry.get("pos", 0.76))})
    return out


def _layout_signature(scene):
    return (scene.get("mode"),
            tuple(t.get("rect") for t in (scene.get("tiles") or [])))


def cut_times(plan, clip_start=0.0, segments=None, min_gap=1.1):
    """LAYOUT-change times in output time, for SFX and transitions.

    A punch-in is also a scene boundary, but a whoosh and a dip on every zoom
    would turn a 19s clip into nine little stutters. Only a real composition
    change qualifies -- single to split, a different tile grid, and so on.
    """
    plans = plan or {}
    broll = plans.get("broll") or []
    scenes = plans.get("scenes") or []
    if len(scenes) <= 1 and not broll:
        return []
    out = []
    if broll:
        for a, _ in broll:
            t = float(a)
            if segments:
                from app.pipeline import pacing
                t = pacing.remap_time(clip_start + t, segments)
            if t >= 0.25 and (not out or t - out[-1] >= min_gap):
                out.append(round(t, 3))
    for prev, scene in zip(scenes, scenes[1:]):
        if scene.get("zoom") or prev.get("zoom"):
            continue
        if _layout_signature(scene) == _layout_signature(prev):
            continue
        t = float(scene.get("start", 0.0))
        if segments:
            from app.pipeline import pacing
            t = pacing.remap_time(clip_start + t, segments)
        if t < 0.25:
            continue
        if out and t - out[-1] < min_gap:
            continue
        out.append(round(t, 3))
    return out


def _scene(samples, requested_count, start, end, src_w, src_h, out_w, out_h,
           speaker_words=None, active_spans=None):
    """Build one visual scene, lowering confidence instead of inventing a crop."""
    if requested_count <= 0:
        return {"start": start, "end": end, "mode": "wide", "tiles": []}

    if requested_count == 1:
        points = primary_timeline(samples)
        if not points:
            return {"start": start, "end": end, "mode": "wide", "tiles": []}
        subject = {"points": points}
        tile = tile_window(subject, _layout_rects(1)[0], (0.0, float(src_w)),
                           src_w, src_h, out_w, out_h)
        return {"start": start, "end": end, "mode": "single", "tiles": [tile]}

    tracks = build_tracks(samples)
    count = min(requested_count, len(tracks), MAX_PEOPLE)
    if count < 2:
        return _scene(samples, 1, start, end, src_w, src_h, out_w, out_h,
                      speaker_words)

    # Keep the most persistent participants, then restore screen order. For a
    # three-person layout, feature the largest visible participant in the wide
    # top tile; the others stay in left-to-right order underneath.
    tracks = sorted(tracks, key=lambda tr: len(tr["points"]), reverse=True)[:count]
    tracks.sort(key=_track_mean_x)

    if count == 2:
        together = _together_tile(
            tracks, start, end, src_w, src_h, out_w, out_h)
        if together:
            base = {"start": start, "end": end,
                    "mode": "together", "tiles": [together]}
            rects = _layout_rects(2)
            mode = "together"
        else:
            by_area = sorted(tracks, key=_track_area)
            area_ratio = _track_area(by_area[-1]) / max(_track_area(by_area[0]), 1e-6)
            if (_speaker_share(speaker_words, start, end) >= REACTION_SPEECH_SHARE
                    and area_ratio >= REACTION_AREA_RATIO):
                # Main speaker first because the first reaction rectangle is the
                # larger top panel. The second track remains visible as context.
                #
                # WHICH face is the main one matters. A larger face used to win,
                # which is wrong whenever the guest sits closer to the camera
                # than the host: the clip then featured the listener. Spoken
                # time, mapped to a position by lip motion, is the editorial
                # answer; size is only the fallback when that mapping is absent.
                main = by_area[-1]
                if active_spans:
                    spoken = _spoken_shares(tracks, active_spans)
                    if max(spoken) > 0:
                        main = tracks[spoken.index(max(spoken))]
                tracks = [main] + [tr for tr in tracks if tr is not main]
                mode = "reaction"
            else:
                mode = "split"
            rects = _layout_rects(2, mode)
            base = None
    elif count == 3:
        featured = max(tracks, key=_track_area)
        tracks = [featured] + [tr for tr in tracks if tr is not featured]
        mode = "triple"
        rects = _layout_rects(count, mode)
        base = None
    else:
        mode = "grid"
        rects = _layout_rects(count, mode)
        base = None

    regions = _participant_regions(tracks, src_w)
    if count >= 3:
        # Panels: speaker shots where someone holds the floor, the group in
        # between -- and a group shot that never shows a face twice.
        variants = _group_variants(tracks, samples, active_spans,
                                   start, end, src_w, src_h, out_w, out_h)
        return variants or _featured_scene(samples, start, end,
                                           src_w, src_h, out_w, out_h)
    if base is None:
        tiles = [
            tile_window(track, rect, regions[id(track)],
                        src_w, src_h, out_w, out_h)
            for track, rect in zip(tracks, rects)
        ]
        if tiles_clash(tiles):
            # Two people close enough that their crops overlap: one shared
            # frame if it fits, otherwise the whole frame -- never a split
            # that shows someone twice.
            together = _together_tile(tracks, start, end,
                                      src_w, src_h, out_w, out_h)
            base = ({"start": start, "end": end, "mode": "together",
                     "tiles": [together]} if together else
                    {"start": start, "end": end, "mode": "wide", "tiles": []})
            mode = base["mode"]
        else:
            base = {"start": start, "end": end, "mode": mode, "tiles": tiles}

    # Two people in a real conversation: let the edit cut to whoever is
    # speaking. This is what separates a framed podcast from a framed podcast
    # that looks edited.
    if count == 2 and active_spans:
        variants = _punch_variants(
            tracks, regions, rects, mode, base, active_spans,
            start, end, src_w, src_h, out_w, out_h)
        if variants:
            return variants
    return base


def expr(keys, index, span):
    """A piecewise-linear ffmpeg expression for one crop coordinate over time.

    ffmpeg's `crop` takes an expression in `t`, so the whole move is described
    once at filter-build time rather than by re-invoking anything per frame.
    Keyframes that did not move are collapsed first -- after the dead zone most
    of them are identical, and emitting all of them would build a needlessly
    enormous nested expression for a shot that mostly sits still.
    """
    trimmed = motion_segments(keys)
    if all(v == trimmed[0][1] for _, v in trimmed):
        return f"{trimmed[0][1]:.1f}"

    # Built from the last segment backwards so each if() nests into the else.
    # Every move is a smoothstep, not a line: it leaves its hold at zero speed
    # and arrives at the next one at zero speed, the way an operator eases a
    # camera. A linear ramp starts and stops dead, which is most of what made
    # the crop read as robotic.
    e = f"{trimmed[-1][1]:.1f}"
    for (t0, v0), (t1, v1) in zip(reversed(trimmed[:-1]), reversed(trimmed[1:])):
        if abs(v1 - v0) <= MOTION_EPSILON:
            ramp = f"{v0:.1f}"
        else:
            dt = max(t1 - t0, 1e-3)
            s = f"((t-{t0:.2f})/{dt:.2f})"
            ramp = f"{v0:.1f}+({v1:.1f}-{v0:.1f})*{s}*{s}*(3-2*{s})"
        e = f"if(lt(t\\,{t1:.2f})\\,{ramp}\\,{e})"
    return e


# Changes smaller than this (in source pixels) are treated as standing still.
MOTION_EPSILON = 0.5


def motion_segments(keys):
    """Keyframes reduced to the holds and moves a camera operator would make.

    Two things the raw samples get wrong. A hold must keep BOTH its ends: when
    only the first sample of a still stretch was kept, the next move was drawn
    from the start of the hold, so "sit still for 3s, then reframe" rendered as
    a 3-second drift. And one move spans many samples, so consecutive samples
    heading the same way are merged into a single segment -- easing each
    0.5s piece separately would make the crop stutter to a stop twice a second.
    """
    pts = [(float(t), round(float(v), 1)) for t, v in keys]
    if len(pts) <= 2:
        return pts

    def direction(a, b):
        d = b[1] - a[1]
        return 0 if abs(d) <= MOTION_EPSILON else (1 if d > 0 else -1)

    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        if direction(pts[i - 1], pts[i]) != direction(pts[i], pts[i + 1]):
            out.append(pts[i])          # a hold starts or ends, or a move turns
    out.append(pts[-1])
    # Collapse a hold whose ends drifted by less than the threshold into a flat.
    flat = [out[0]]
    for p in out[1:]:
        if abs(p[1] - flat[-1][1]) <= MOTION_EPSILON:
            p = (p[0], flat[-1][1])
        flat.append(p)
    return flat


_TITLE_STOP = {
    "the", "and", "for", "with", "this", "that", "from", "your", "about",
    "what", "when", "where", "which", "how", "why", "are", "was", "were",
    "will", "would", "can", "could", "just", "like", "have", "has", "had",
    "not", "you", "his", "her", "their", "our", "its", "all", "but", "they",
    "them", "then", "than", "into", "over", "under", "more", "most", "some",
    "any", "one", "two", "get", "got", "out", "now", "new", "really",
}


def _keyword_windows(words, keywords, title=""):
    """[(start, end)] for the clip's key words, in source time.

    Each window covers the keyword plus a little hold, which is where a
    punch-in lands. Overlapping windows are merged so a repeated word cannot
    produce two zooms a tenth of a second apart.

    When the selection model returns no keywords, fall back to the words in the
    clip's own title. The title is generated FROM the clip, so its distinctive
    words are usually spoken inside it -- which is enough to make Auto Zoom do
    something instead of silently nothing.
    """
    if not words:
        return []
    keywords = [k for k in (keywords or []) if str(k).strip()]
    if not keywords and title:
        keywords = [w for w in str(title).replace("?", " ").replace("!", " ").split()
                    if len(w.strip(".,!?;:\"'()[]")) >= 4
                    and w.strip(".,!?;:\"'()[]").lower() not in _TITLE_STOP]
    if not keywords:
        return []
    wanted = {str(k).strip().lower().strip(".,!?;:") for k in keywords if str(k).strip()}
    found = []
    for word in words:
        token = (word.get("punctuated_word") or word.get("word") or "")
        token = token.lower().strip(".,!?;:")
        if token and token in wanted:
            s = float(word.get("start", 0.0))
            e = float(word.get("end", s + 0.3))
            found.append((max(0.0, s - 0.12), e + 0.55))
    merged = []
    for s, e in sorted(found):
        if merged and s <= merged[-1][1] + 0.35:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged[:4]


PUNCH_SCENE_MIN = 5.0      # only a hold this long gets a keyword punch-in
PUNCH_EDGE = 1.2           # never this close to the shot's own cut
PUNCH_SPACING = 6.0        # at most one punch-in per this many seconds


def _zoom_scenes(scenes, windows, factor=0.90):
    """Split scenes around keyword windows, shrinking the crop for the window.

    A punch-in cut, not a continuous zoom: the camera stays exactly where it is
    and the framing tightens for a beat, which is the way short-form editors use
    it. Shrinking the crop window is also the only version that costs nothing in
    quality -- the renderer already crops and scales every tile.

    Used sparingly, because it is a reframe on top of every speaker cut. A
    punch-in lands only inside a long hold, clear of that hold's own cuts, and
    never within PUNCH_SPACING of the last one: a cut, a punch and a cut back a
    second apart is exactly the zoom-in, zoom-out the edit must not have.
    """
    if not windows:
        return scenes
    out, last_punch = [], float("-inf")
    for scene in scenes:
        s0, s1 = scene["start"], scene["end"]
        cuts = []
        if scene.get("tiles") and s1 - s0 >= PUNCH_SCENE_MIN:
            for a, b in sorted(windows):
                a, b = max(a, s0 + PUNCH_EDGE), min(b, s1 - PUNCH_EDGE)
                if b - a >= 0.45 and a - last_punch >= PUNCH_SPACING:
                    cuts.append((a, b))
                    last_punch = b
        if not cuts:
            out.append(scene)
            continue
        cursor = s0
        for a, b in cuts:
            out.append(_scene_segment(scene, cursor, a, 1.0))
            out.append(_scene_segment(scene, a, b, factor))
            cursor = b
        out.append(_scene_segment(scene, cursor, s1, 1.0))
    return out


def _scene_segment(scene, start, end, factor):
    tiles = []
    for tile in scene.get("tiles") or []:
        win_w, win_h = tile.get("win", (0, 0))
        tiles.append({
            **tile,
            "win": (max(2, int(round(win_w * factor))),
                    max(2, int(round(win_h * factor)))),
        })
    return {**scene, "start": round(start, 3), "end": round(end, 3),
            "zoom": factor < 0.999, "tiles": tiles}


def _source_cuts(probe, offset, start, end):
    """Hard cuts in the source, in source time. [] when undetectable."""
    try:
        from app.pipeline import content       # imports reframe at load time
        return sorted(offset + c for c in content.scene_cuts(probe, 0.0, end - start)
                      if 0.15 < c < (end - start) - 0.15)
    except Exception:
        return []


def _split_runs_at_cuts(runs, samples, cuts):
    """Layout runs, also broken wherever the source cuts between two samples."""
    if not cuts:
        return runs
    out = []
    for first, last, count in runs:
        piece = first
        for i in range(first + 1, last):
            if any(samples[i - 1][0] < c <= samples[i][0] for c in cuts):
                out.append((piece, i, count))
                piece = i
        out.append((piece, last, count))
    return out


# Inside one camera shot a layout must hold this long. Face counts flicker as
# heads turn; without this a 0.8s three-up flashed between two wide frames of
# the same angle. Across a real cut the shot is new and anything goes.
MIN_LAYOUT_SECONDS = 1.6


def _merge_short_runs(runs, samples, cuts):
    """Absorb layout runs too short to read into a neighbour in the same shot."""
    def seconds(run):
        first, last, _ = run
        t0 = samples[first][0]
        t1 = samples[last][0] if last < len(samples) else samples[-1][0] + 0.5
        return t1 - t0

    def cut_between(a, b):
        """A source cut separates run a from the run b right after it."""
        lo, hi = samples[a[1] - 1][0], samples[b[0]][0]
        return any(lo < c <= hi for c in cuts)

    runs = list(runs)
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i, run in enumerate(runs):
            if seconds(run) >= MIN_LAYOUT_SECONDS:
                continue
            left = runs[i - 1] if i > 0 and not cut_between(runs[i - 1], run) else None
            right = (runs[i + 1] if i + 1 < len(runs)
                     and not cut_between(run, runs[i + 1]) else None)
            target = max((r for r in (left, right) if r), key=seconds, default=None)
            if target is None:
                continue                       # a whole shot: keep it
            if target is left:
                runs[i - 1:i + 1] = [(left[0], run[1], left[2])]
            else:
                runs[i:i + 2] = [(run[0], right[1], right[2])]
            changed = True
            break
    # Neighbours that now share a layout inside one shot are one scene.
    joined = [runs[0]] if runs else []
    for run in runs[1:]:
        last = joined[-1]
        if run[2] == last[2] and not cut_between(last, run):
            joined[-1] = (last[0], run[1], last[2])
        else:
            joined.append(run)
    return joined


def _whole_shots(runs, samples, cuts):
    """One composition per camera shot when the shot is plainly a conversation.

    Face counts flicker inside a shot -- heads turn into profile, a hand covers
    a face -- and each flicker long enough to survive `_merge_short_runs` used
    to become a new layout of the same unmoving camera: a two-shot, then the
    whole table, then a close-up. That is the zoom-in, zoom-out churn. The
    people at a table do not change between samples, so a shot whose
    persistent tracks show two or more people is composed once, and only who
    is speaking moves the edit. A shot that is mostly one face is left alone:
    that is more likely a close-up the cut detector missed.
    """
    if len(runs) <= 1:
        return runs
    out, shot = [], [runs[0]]

    def flush():
        first, last = shot[0][0], shot[-1][1]
        if len(shot) > 1:
            part = samples[first:last]
            people = min(len(build_tracks(part)), MAX_PEOPLE)
            lone = sum(1 for _, faces in part if len(faces) <= 1) / max(1, len(part))
            if (people >= 3 and lone < 0.25) or (people == 2 and lone < 0.5):
                out.append((first, last, people))
                return
        out.extend(shot)

    for run in runs[1:]:
        lo, hi = samples[shot[-1][1] - 1][0], samples[run[0]][0]
        if any(lo < c <= hi for c in cuts):
            flush()
            shot = [run]
        else:
            shot.append(run)
    flush()
    return out


def plan(video_path, start, end, out_w, out_h, speaker_words=None,
         src_size=None, samples=None, zoom_windows=None):
    """Work out how this clip should be framed.

    Returns a dict the renderer can act on, or None to mean "no idea, use the
    fixed frame". Returning None is a normal outcome, not a failure: footage
    with no clear faces (a screen recording, a b-roll montage, a wide stage
    shot) genuinely has nothing to track, and a confident wrong crop there is
    far worse than the plain treatment.

    Detection runs on a downscaled analysis copy (see `analysis_source`), so
    every coordinate is normalised and applied to the REAL source dimensions.
    `samples` lets a caller that has already detected faces hand them over and
    skip a second decode pass.
    """
    real_w, real_h = src_size or probe_dimensions(video_path)
    probe, offset = analysis_clip(video_path, start, end)
    if samples is None:
        samples, (probe_w, probe_h) = detect_faces(probe, 0.0, end - start)
        # Detection ran on a clip copy whose zero is this clip's start; shift it
        # back onto the source timeline the rest of planning assumes.
        samples = [(t + offset, faces) for t, faces in samples]
        if not real_w or not real_h:
            real_w, real_h = probe_w, probe_h
    src_w, src_h = real_w, real_h
    if not samples or not src_w:
        return None
    if not any(faces for _, faces in samples):
        return None

    # Active-speaker pass. Optional because it is the one part of planning that
    # costs an extra decode: switch it off with CLIPPER_ACTIVE_SPEAKER=0.
    active_spans = None
    if ACTIVE_SPEAKER and (end - start) <= MAX_ACTIVE_SECONDS:
        try:
            all_tracks = build_tracks(samples)
            if len(all_tracks) >= 2:
                rows = lip_activity(probe, 0.0, end - start, all_tracks)
                rows = [(t + offset, row) for t, row in rows]
                if rows:
                    active_spans = speaker_spans(
                        all_tracks, rows, speaker_words, start, end)
                    if active_spans:
                        print(f"[clipper] active speaker: "
                              f"{len(active_spans)} speech span(s) across "
                              f"{len(all_tracks)} face(s)")
        except Exception as exc:
            print(f"[clipper] active speaker skipped ({exc})")

    counts = stabilize_layouts([len(faces) for _, faces in samples])
    # The source's own camera cuts. A face count alone misses most of them: a
    # multicam podcast cuts from one three-person angle to another, the count
    # stays three, and a crop computed across both angles frames whoever sits
    # at that position in the OTHER shot -- the duplicated and half-cut faces.
    source_cuts = _source_cuts(probe, offset, start, end)
    scenes = []
    runs = _merge_short_runs(_split_runs_at_cuts(_runs(counts), samples, source_cuts),
                             samples, source_cuts)
    runs = _whole_shots(runs, samples, source_cuts)

    def boundary(i):
        """Where the shot changes between sample i-1 and sample i."""
        lo, hi = samples[i - 1][0], samples[i][0]
        inside = [c for c in source_cuts if lo < c <= hi]
        return inside[0] if inside else (lo + hi) / 2

    for first, last, count in runs:
        # Camera cuts happen between samples: on a detected cut when there is
        # one, halfway between the samples otherwise.
        scene_start = start if first == 0 else boundary(first)
        scene_end = end if last == len(samples) else boundary(last)
        scene_samples = samples[first:last]
        built = _scene(scene_samples, count, scene_start, scene_end,
                       src_w, src_h, out_w, out_h, speaker_words,
                       active_spans)
        scenes.extend(built if isinstance(built, list) else [built])

    # Detection runs in source time while the renderer seeks to the clip start.
    # Store one clip-relative timebase for scene boundaries and every crop key.
    for scene in scenes:
        scene["start"] -= start
        scene["end"] -= start
        for tile in scene["tiles"]:
            tile["keys"] = [(t - start, x, y) for t, x, y in tile["keys"]]

    # Auto Zoom: a punch-in on the clip's key words. Applied before the caption
    # placement is written, so the caption ranges still describe the finished
    # scene list.
    if zoom_windows:
        scenes = _zoom_scenes(scenes, [(max(0.0, a - start), max(0.0, b - start))
                                       for a, b in zoom_windows])

    # Captions cannot sit in one band for every composition. A split has a
    # gutter at its seam where text covers a chest and some headroom; a single
    # has a lower third. The old single lower-third baseline put text straight
    # across the lower tile's face the moment the layout became a split.
    modes = {scene.get("mode") for scene in scenes}
    if modes & {"split", "together", "triple", "grid"}:
        caption_pos = 0.48
    elif "reaction" in modes:
        caption_pos = 0.60
    else:
        caption_pos = 0.76

    # Per-scene placement, because a clip that cuts between a single and a
    # split needs the text to move with the cut -- one baseline for the whole
    # clip is either over a face in one layout or stranded in the gutter in the
    # other. `caption_pos` stays as the fallback for events outside any scene.
    captions = []
    for scene in scenes:
        if scene.get("end", 0) - scene.get("start", 0) <= 0.02:
            continue
        captions.append({
            "start": round(max(0.0, scene["start"]), 3),
            "end": round(scene["end"], 3),
            "pos": CAPTION_POS_FOR_MODE.get(scene.get("mode"), caption_pos),
        })

    return {
        "version": PLAN_VERSION,
        "mode": "adaptive",
        "src": (src_w, src_h),
        "duration": max(0.0, end - start),
        "caption_pos": caption_pos,
        "captions": captions,
        "scenes": scenes,
    }
