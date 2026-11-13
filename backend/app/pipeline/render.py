"""Cuts a clip from the source video, reframes it to 9:16 without cropping
content (blurred background fill + centered full frame), burns in captions,
and keeps the original audio track by default.
"""

import json
import os
import re
import signal
import subprocess

OUT_W, OUT_H = 1080, 1920

def _container_cpus() -> int:
    """How many CPUs this process may actually use, not how many the host has.

    os.cpu_count() reports the HOST's processors, which inside a container is
    the wrong number in the expensive direction: a 2-vCPU service on a 48-core
    host sees 48. cgroups is where the real quota lives, so read it.
    """
    for quota_path, period_path in (
        ("/sys/fs/cgroup/cpu.max", None),                    # v2: "200000 100000"
        ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us",              # v1: two files
         "/sys/fs/cgroup/cpu/cpu.cfs_period_us"),
    ):
        try:
            raw = open(quota_path).read().split()
            if period_path:
                quota, period = int(raw[0]), int(open(period_path).read().strip())
            else:
                if raw[0] == "max":                          # v2, explicitly uncapped
                    continue
                quota, period = int(raw[0]), int(raw[1])
            if quota > 0 and period > 0:
                # Round up: a 1.5-CPU quota should still use 2 threads.
                return max(1, -(-quota // period))
        except Exception:
            continue
    return os.cpu_count() or 1


# Ceiling on encoder threads. Left unset, x264 scales its thread count -- and so
# its memory -- to whatever CPU count it can see, which inside a container is the
# host's rather than the one the memory limit was sized for.
#
# Defaulting this to a constant had the same bug in miniature: 4 threads on a
# 2-vCPU service buys no throughput (there are only 2 cores to run on) while
# still paying for 4 sets of x264 frame buffers, against a memory limit sized
# for the smaller box. Track the actual quota instead, and let the env var win
# for the cases where a human knows better.
FFMPEG_THREADS = max(1, int(os.environ.get("CLIPPER_FFMPEG_THREADS", "0"))
                     or _container_cpus())


def _detect_hw_encoder() -> str | None:
    """Return a hardware H.264 encoder name if one is available, else None.

    Checked once at import. VideoToolbox (macOS) is the only one tested; add
    NVENC / VAAPI here when a GPU worker exists.
    """
    if os.environ.get("CLIPPER_NO_HW_ENCODE", ""):
        return None
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        )
        if "h264_videotoolbox" in proc.stdout:
            return "h264_videotoolbox"
    except Exception:
        pass
    return None


HW_ENCODER = _detect_hw_encoder()
if HW_ENCODER:
    print(f"[render] hardware encoder available: {HW_ENCODER}")


# The background is blurred at quarter size and then scaled back up. Blurring is
# per-pixel work, so doing it at 540x960 instead of 1080x1920 is ~4x cheaper --
# and since the result is heavily blurred anyway, the upscale is invisible.
REFRAME_FILTER = (
    "[0:v]scale=540:960:force_original_aspect_ratio=increase,"
    "crop=540:960,gblur=sigma=12,scale=1080:1920[bg];"
    "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2[framed]"
)


def probe_dimensions(video_path: str) -> tuple:
    """Returns (width, height) of the first video stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", video_path],
        check=True, capture_output=True, text=True,
    )
    stream = json.loads(out.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def has_audio(video_path: str) -> bool:
    """Whether the source carries an audio stream at all.

    Gameplay captures, screen recordings and stock b-roll frequently do not.
    The renderer used to map `0:a` unconditionally, which makes ffmpeg fail the
    whole export with "Stream map '0:a' matches no streams" -- a clip that could
    have been rendered fine, lost to a missing music track.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=20,
        )
        return bool(out.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return True          # unknown: keep the old behaviour rather than dropping audio


def caption_margin_from_position(out_h: int, position: float) -> int:
    """Margin for a caption block whose CENTRE sits at `position` (0..1).

    ASS margins are measured from the bottom, so a position of 0.9 (near the
    bottom of frame) becomes a small margin. Clamped so captions cannot be
    dragged off the canvas entirely.
    """
    position = max(0.05, min(0.95, float(position)))
    return int(round(out_h * (1.0 - position)))


# Reels/TikTok/Shorts paint their own UI over the bottom of the video -- the
# caption, the audio strip, the like/comment/share rail. Anything below this is
# read by nobody.
PLATFORM_UI_FRACTION = 0.17

# How far up from the platform-UI floor captions sit. Captions parked at the
# very bottom of a letterboxed frame read as a separate thing stuck under the
# video; lifting them toward it makes the two read as one composition, and on a
# phone they land closer to where the eye already is.
CAPTION_LIFT = 0.05


# YouTube's controls cover a thin strip at the bottom, only while hovered --
# not the ~22% that TikTok and Reels reserve. Applying the vertical floor to a
# 16:9 frame lifted captions to mid-height, straight across the mic and chest.
LANDSCAPE_CAPTION_FRACTION = 0.08


def _landscape_margin(out_w: int, out_h: int):
    return int(out_h * LANDSCAPE_CAPTION_FRACTION) if out_w > out_h else None


def caption_margin_v(src_w: int, src_h: int, out_w: int = OUT_W, out_h: int = OUT_H) -> int:
    """Distance from the bottom of the output frame to the caption baseline.

    Two constraints, in order: captions must clear the platform's own UI, and
    they should sit under the video rather than across a face. Clearing the UI
    wins -- a perfectly placed caption hidden behind an Instagram like button is
    worth nothing.
    """
    if _landscape_margin(out_w, out_h) is not None:
        return _landscape_margin(out_w, out_h)
    floor = int(out_h * (PLATFORM_UI_FRACTION + CAPTION_LIFT))

    fg_h = content_height(src_w, src_h, out_w, out_h)
    band_height = (out_h - fg_h) / 2

    if band_height >= 200:
        # Sit in the lower blur band, nudged toward the video, but never below
        # the platform UI line.
        return max(floor, int(band_height * 0.40))

    # Little or no letterbox: ride just above the platform chrome.
    return floor


# Output canvas per aspect ratio. 9:16 for Shorts/Reels/TikTok, 1:1 for feed
# posts, 16:9 for YouTube proper.
RATIOS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}


# A 16:9 source letterboxed into 9:16 fills only 32% of the frame -- two thirds
# of a phone screen is blurred wallpaper. Zooming in slightly (which crops the
# sides) buys a lot of content area cheaply, because the subject in talking-head
# footage is centred and the edges are usually room.
#
# 0.44 was still too timid: it left 550px bars above and below, so the thing the
# viewer actually came to watch got 43% of their screen and blurred wallpaper got
# the rest. These numbers put the video band at ~55% -- the size it was drawn at
# in the layout this was matched against -- which is where a Reel starts feeling
# like a video rather than a video in a frame.
#
# The cap is the real safety: it bounds how much of the sides can be thrown away
# for a wide shot, where the edges are scenery rather than room.
MIN_CONTENT_FRACTION = 0.55     # aim for the video covering >= 55% of frame height
MAX_ZOOM = 1.60                 # ~43% off the sides. Talking-head footage centres
                                # the subject, so this is nearly always room -- but
                                # it is a hard stop so a wide shot cannot lose its
                                # subject entirely.
                                #
                                # This has to be >= the zoom the fraction above
                                # actually needs, or the cap silently becomes the
                                # real policy: at 1.50 a 16:9 source reached only
                                # 47% of frame height and MIN_CONTENT_FRACTION's
                                # 0.55 was a number the code never honoured.

# "Fit video" promises nothing is cropped, so it gets no zoom at all -- that is
# the whole reason to choose it over Classic. What it CAN do is stop wasting the
# bars: they are a design surface, and the caption band belongs in the lower one.
FIT_CONTENT_BIAS = 0.42         # centre of the video block, as a fraction of
                                # height. Below 0.5 = sat above centre, which
                                # leaves a deliberate caption band underneath
                                # instead of two equal dead margins.


def content_height(src_w: int, src_h: int, out_w: int, out_h: int) -> int:
    """Height the source occupies after the zoom policy, in output pixels."""
    natural = out_w * src_h / src_w              # height at full output width
    if natural >= out_h:
        return out_h
    wanted = out_h * MIN_CONTENT_FRACTION
    zoom = min(max(wanted / natural, 1.0), MAX_ZOOM)
    return int(round(min(natural * zoom, out_h)))


def _narrower(src_w: int, src_h: int, width: int, height: int) -> bool:
    """Whether the source is narrower than the frame (a tall phone recording in
    9:16, a vertical or square video in 16:9).

    The zoom policy below assumes the opposite: it scales the picture to its
    target height and crops the overflowing SIDES back to the frame width. A
    narrower source has no overflowing sides -- scaled to the frame height it is
    narrower than the frame, and the crop asked ffmpeg for more pixels than
    exist ("Invalid too big ... size"), failing the whole render. Such a
    source fits by height instead, with the backdrop at the sides.
    """
    return bool(src_w and src_h) and src_w * height < width * src_h


def blur_filter(width: int, height: int, src_w: int = None, src_h: int = None) -> str:
    """Subject centred on a blurred fill of itself, for any canvas size.

    Keeps the quarter-size blur trick: gblur is per-pixel work, so blurring at
    half dimensions and upscaling is ~4x cheaper and invisible once blurred.
    """
    half_w, half_h = width // 2, height // 2
    bg = (f"[0:v]scale={half_w}:{half_h}:force_original_aspect_ratio=increase,"
          f"crop={half_w}:{half_h},gblur=sigma=12,scale={width}:{height}[bg];")

    if _narrower(src_w, src_h, width, height):
        fg = f"[0:v]scale=-2:{height}[fg];"
    elif src_w and src_h:
        fg_h = content_height(src_w, src_h, width, height)
        # scale to that height, then crop the overflowing sides back to width
        fg = (f"[0:v]scale=-2:{fg_h}:force_original_aspect_ratio=increase,"
              f"crop={width}:{fg_h}[fg];")
    else:
        fg = f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"

    return bg + fg + "[bg][fg]overlay=(W-w)/2:(H-h)/2[framed]"


def fit_filter(width: int, height: int, background: str = "black",
               src_w: int = None, src_h: int = None) -> str:
    """Video block on flat bars -- no blur, and the bars are a real colour.

    `background` is any ffmpeg colour ('black', '#1e1e2e', 'white'). The bars are
    a design surface, not dead space: a brand colour there reads as intentional
    where black reads as a mistake.

    The block sits ABOVE centre (FIT_CONTENT_BIAS). Centred, it split the
    leftover height into two identical dead margins and pushed the captions to
    the very bottom of the frame, under the platform's own UI. Biased up, the
    same pixels become a title band above and a caption band below.

    It also takes the SAME zoom as Classic. This mode used to refuse to crop at
    all, which sounds principled until you see it: a 16:9 source in a 9:16 frame
    came out 31.6% of the height, and two thirds of the clip was flat colour.
    The difference from Classic is now the backdrop -- flat and brandable rather
    than a blurred copy -- not the size of the picture. Callers that pass no
    source size still get the old uncropped behaviour.
    """
    colour = (background or "black").replace("#", "0x")
    # y of the block's top edge = centre - half its height, clamped into frame.
    y = f"max(0\\,min(oh-ih\\,{FIT_CONTENT_BIAS}*oh-ih/2))"
    if _narrower(src_w, src_h, width, height):
        # Full height, centred, with the bars at the sides.
        return (f"[0:v]scale=-2:{height},"
                f"pad={width}:{height}:(ow-iw)/2:0:{colour}[framed]")
    if src_w and src_h:
        fg_h = content_height(src_w, src_h, width, height)
        scale = (f"[0:v]scale=-2:{fg_h}:force_original_aspect_ratio=increase,"
                 f"crop={width}:{fg_h},")
    else:
        scale = f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
    return f"{scale}pad={width}:{height}:(ow-iw)/2:{y}:{colour}[framed]"


# Podcast footage is the one case where the zoom policy is actively wrong. A
# two-shot puts the guest near one edge and the host near the other, so cropping
# 43% off the sides to make the picture bigger removes a person from the
# conversation. This mode therefore never crops -- it buys its screen presence
# from layout instead, sitting the full-width frame high and giving the space
# underneath to captions, which is the shape every podcast clip on the platforms
# already has.
PODCAST_CONTENT_BIAS = 0.36


def podcast_filter(width: int, height: int) -> str:
    """Uncropped two-shot sat high, over a blurred copy of itself.

    Between the other two: `fit` never crops but spends its bars on flat colour,
    `blur` fills the frame richly but crops hard to do it. This keeps everyone in
    shot like `fit` and still fills the frame like `blur`.
    """
    half_w, half_h = width // 2, height // 2
    y = f"max(0\\,min(H-h\\,{PODCAST_CONTENT_BIAS}*H-h/2))"
    return (
        f"[0:v]scale={half_w}:{half_h}:force_original_aspect_ratio=increase,"
        f"crop={half_w}:{half_h},gblur=sigma=12,"
        # Knocked well down, unlike the Classic backdrop. Most of this frame is
        # backdrop with captions sitting on it, and the podcast styles are quiet
        # low-contrast faces with no outline -- cream text on a bright blurred
        # shirt was genuinely unreadable. Darkened, it reads as a deliberate
        # surface and the captions sit on it cleanly.
        f"eq=brightness=-0.55:saturation=0.35,"
        f"scale={width}:{height}[bg];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:{y}[framed]"
    )


def podcast_caption_margin_v(src_w: int, src_h: int,
                             out_w: int = OUT_W, out_h: int = OUT_H) -> int:
    """Caption baseline for the podcast frame.

    The video is biased upward, so the generic margin -- which assumes equal
    bands and measures from the bottom -- would drop captions into the far
    corner of a very tall empty band. This centres them in the space actually
    left under the video, while still clearing the platform's UI.
    """
    if _landscape_margin(out_w, out_h) is not None:
        return _landscape_margin(out_w, out_h)
    fg_h = min(int(out_w * src_h / src_w), out_h) if src_w and src_h else out_h // 2
    top = max(0, min(out_h - fg_h, PODCAST_CONTENT_BIAS * out_h - fg_h / 2))
    band_top = top + fg_h                      # first free pixel under the video
    band = max(out_h - band_top, 1)
    floor = int(out_h * (PLATFORM_UI_FRACTION + CAPTION_LIFT))
    # Centre of the free band, expressed as a margin from the bottom.
    return max(floor, int(out_h - (band_top + band * 0.42)))


def smart_caption_margin_v(plan: dict, out_h: int = OUT_H) -> int:
    """Caption baseline for a face-aware frame.

    These layouts have no bars -- that is the point of them -- so captions sit
    ON the picture and the question becomes which part of the picture they can
    cover without hiding a face.

    Stacked: the seam between the two tiles. It is the one horizontal band that
    belongs to neither speaker, it is where every podcast clip on the platforms
    puts its captions, and text there reads against both tiles at once.

    Single: the lower third, clear of the platform's own UI. Lower would be
    under the like button; higher would be across the speaker's chest.
    """
    if plan and plan.get("caption_pos") is not None:
        # The planner knows its own composition: a gaming stack wants captions
        # under the webcam, a screencast wants them low, a podcast wants the
        # lower third. Let it say so rather than inferring from `mode`, which
        # only distinguishes shapes the old renderer knew about.
        try:
            position = max(0.05, min(0.95, float(plan["caption_pos"])))
            return int(round(out_h * (1.0 - position)))
        except (TypeError, ValueError):
            pass
    if plan and plan.get("mode") == "adaptive":
        # One ASS file is shared by every visual scene. A caption cannot jump
        # between the seam and lower third without becoming distracting, so the
        # adaptive composition uses one steady lower-third baseline. It is clear
        # of faces in a single and sits comfortably inside the lower tile of a
        # split, matching the placement viewers already expect on podcast clips.
        return int(out_h * 0.24)
    if plan and plan.get("mode") == "stacked":
        # Just below the seam, so the text sits against the lower tile's top
        # rather than splitting the difference and clipping both.
        return int(out_h * 0.46)
    return int(out_h * (PLATFORM_UI_FRACTION + CAPTION_LIFT))


def _even(value: float) -> int:
    value = max(2, int(round(value)))
    return value - value % 2


def _local_axis(keys, scene_start, axis):
    """One tile axis on the scene-local FFmpeg timeline."""
    values = [
        (max(0.0, point[0] - scene_start), point[axis])
        for point in keys
    ]
    if not values:
        return "0"
    if values[0][0] > 0:
        values.insert(0, (0.0, values[0][1]))
    return values


# Seconds each b-roll cutaway takes to dissolve in and out.
BROLL_FADE = 0.18


def _wide_scene_filter(input_label: str, width: int, height: int, tag: str):
    """The honest no-face fallback, namespaced for an adaptive scene."""
    half_w, half_h = width // 2, height // 2
    y = f"max(0\\,min(H-h\\,{PODCAST_CONTENT_BIAS}*H-h/2))"
    return (
        f"{input_label}split=2[{tag}bg0][{tag}fg0];"
        f"[{tag}bg0]scale={half_w}:{half_h}:force_original_aspect_ratio=increase,"
        f"crop={half_w}:{half_h},gblur=sigma=12,"
        f"eq=brightness=-0.25:saturation=0.9,scale={width}:{height}[{tag}bg];"
        f"[{tag}fg0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"setsar=1[{tag}fg];"
        # setsar: the foreground's scale leaves a fractional sample aspect
        # (5120:5121 on a 720p source), and concat refuses to join scenes
        # whose SAR differs -- every adaptive plan with a wide scene failed.
        f"[{tag}bg][{tag}fg]overlay=(W-w)/2:{y},setsar=1[{tag}out]"
    ), f"[{tag}out]"


def _adaptive_filter(plan: dict, width: int, height: int,
                     transitions: bool = False) -> str:
    """Compose camera-aware scenes and concatenate them into one clean timeline."""
    from app.pipeline import reframe

    scenes = [scene for scene in plan.get("scenes", [])
              if scene.get("end", 0) - scene.get("start", 0) > 0.02]
    if not scenes:
        return podcast_filter(width, height)

    parts = []
    hud_overlay = plan.get("hud_overlay")
    if hud_overlay:
        # One full-source stream per scene for the minimap crop, plus a base for
        # the normal composition. Splitting here means a trim inside one scene
        # cannot consume the stream another scene still needs.
        hud_labels = "".join(f"[hud{i}]" for i in range(len(scenes)))
        parts.append(f"[0:v]split={len(scenes) + 1}[vbase]{hud_labels};")
        base_source = "[vbase]"
    else:
        base_source = "[0:v]"
    if len(scenes) > 1:
        labels = "".join(f"[ps{i}]" for i in range(len(scenes)))
        parts.append(f"{base_source}split={len(scenes)}{labels};")

    outputs = []
    fade_d = 0.07 if transitions and len(scenes) > 1 else 0.0
    for i, scene in enumerate(scenes):
        source = f"[ps{i}]" if len(scenes) > 1 else base_source
        start = max(0.0, float(scene.get("start", 0)))
        end = max(start + 0.02, float(scene.get("end", start + 0.02)))
        trimmed = f"[ps{i}trim]"
        parts.append(
            f"{source}trim=start={start:.3f}:end={end:.3f},"
            f"setpts=PTS-STARTPTS{trimmed};"
        )
        broll_out = None
        if plan.get("broll") and len(scenes) == 1:
            # The stack keeps rendering; this second stream becomes the
            # full-frame cutaway that covers it at the chosen windows.
            broll_src = f"[ps{i}broll]"
            parts.append(f"{trimmed}split=2[ps{i}tiles]{broll_src};")
            trimmed = f"[ps{i}tiles]"
            fragment, broll_out = _wide_scene_filter(
                broll_src, width, height, f"ps{i}bw")
            parts.append(fragment + ";")

        tiles = scene.get("tiles") or []
        if scene.get("mode") == "wide" or not tiles:
            fragment, output = _wide_scene_filter(trimmed, width, height, f"ps{i}w")
            parts.append(fragment + ";")
            outputs.append(output)
            continue

        if len(tiles) > 1:
            labels = "".join(f"[ps{i}in{j}]" for j in range(len(tiles)))
            parts.append(f"{trimmed}split={len(tiles)}{labels};")

        tile_outputs = []
        layouts = []
        for j, tile in enumerate(tiles):
            tile_in = f"[ps{i}in{j}]" if len(tiles) > 1 else trimmed
            rx, ry, rw, rh = tile["rect"]
            tile_w, tile_h = _even(width * rw), _even(height * rh)
            tile_x, tile_y = _even(width * rx) if rx else 0, _even(height * ry) if ry else 0
            # The bands are expressed as fractions (0.37 + 0.37 + 0.26), and
            # rounding each to an even pixel can leave a 2px strip at the
            # bottom. That makes a 1918px scene next to a 1920px one and concat
            # refuses to join them. Snap the last band to the real edge.
            if ry + rh >= 0.999 and tile_y + tile_h < height:
                tile_h = _even(height - tile_y)
            win_w, win_h = (_even(tile["win"][0]), _even(tile["win"][1]))
            keys = tile.get("keys") or []
            x_keys = _local_axis(keys, start, 1)
            y_keys = _local_axis(keys, start, 2)
            x = reframe.expr(x_keys, 0, 0) if x_keys != "0" else "0"
            y = reframe.expr(y_keys, 0, 0) if y_keys != "0" else "0"
            out = f"[ps{i}tile{j}]"
            crop = (f"{tile_in}crop={win_w}:{win_h}:"
                    f"'({x})-{win_w // 2}':'({y})-{win_h // 2}'")
            if tile.get("backdrop"):
                # A quiet surface for captions, derived from the footage so the
                # composition still reads as one image. Used under a gameplay
                # stack: the text needs somewhere to live that is neither over
                # the HUD nor over the webcam.
                parts.append(
                    f"{tile_in}scale={tile_w}:{tile_h}:"
                    f"force_original_aspect_ratio=increase,"
                    f"crop={tile_w}:{tile_h},gblur=sigma=24,"
                    f"eq=brightness=-0.5:saturation=0.45,"
                    f"setsar=1{out};"
                )
            elif tile.get("fit"):
                # PRESERVE the crop's own aspect inside the tile, filling the
                # leftover space with a blurred copy of itself. A webcam
                # overlay is a landscape window in a landscape frame; forcing
                # it to fill a tall tile means throwing away most of the
                # picture and upscaling the rest, which is how a facecam ends
                # up as a blurry close-up of a forehead.
                tag = f"ps{i}t{j}"
                parts.append(
                    f"{crop},split=2[{tag}a][{tag}b];"
                    f"[{tag}b]scale={tile_w}:{tile_h}:"
                    f"force_original_aspect_ratio=increase,"
                    f"crop={tile_w}:{tile_h},gblur=sigma=26,"
                    f"eq=brightness=-0.08:saturation=1.0,"
                    f"setsar=1[{tag}bg];"
                    f"[{tag}a]scale={tile_w}:{tile_h}:"
                    f"force_original_aspect_ratio=decrease:flags=lanczos,"
                    # A 9:16 crop of 16:9 footage is a ~1.8x enlargement; lanczos
                    # alone leaves it soft. A light unsharp returns the edge
                    # definition the platforms' own recompression then removes.
                    f"unsharp=5:5:0.45:5:5:0.0,"
                    f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.35:t=2,"
                    f"setsar=1[{tag}fg];"
                    f"[{tag}bg][{tag}fg]overlay=(W-w)/2:(H-h)/2:format=auto{out};"
                )
            else:
                parts.append(
                    f"{crop},"
                    f"scale={tile_w}:{tile_h}:flags=lanczos,"
                    f"unsharp=5:5:0.45:5:5:0.0,setsar=1{out};"
                )
            tile_outputs.append(out)
            layouts.append(f"{tile_x}_{tile_y}")

        if len(tile_outputs) == 1:
            output = tile_outputs[0]
        else:
            output = f"[ps{i}out]"
            parts.append(
                f"{''.join(tile_outputs)}xstack=inputs={len(tile_outputs)}:"
                f"layout={'|'.join(layouts)}:fill=black{output};"
            )

        # Stamp the minimap back into the corner. The action crop above
        # deliberately follows motion and can throw the HUD away; this inset is
        # cropped from the original frame, not the crop, so it is always the
        # real minimap and never a blurry enlargement of part of the action.
        if hud_overlay and scene.get("mode") != "wide":
            src_w, src_h = plan.get("src") or (0, 0)
            rx, ry, rw, rh = hud_overlay.get(
                "rect") or (0.0, 0.66, 0.22, 0.30)
            if src_w and src_h:
                cw = max(2, int(round(src_w * rw)))
                ch = max(2, int(round(src_h * rh)))
                cx = max(0, min(int(round(src_w * rx)), src_w - cw))
                cy = max(0, min(int(round(src_h * ry)), src_h - ch))
                size = max(80, int(round(width * float(
                    hud_overlay.get("size", 0.22)))))
                size_h = max(2, int(round(size * ch / cw)))
                hud_label = f"[hud{i}]"
                hud_out = f"[ps{i}hudout]"
                parts.append(
                    f"{hud_label}trim=start={start:.3f}:end={end:.3f},"
                    f"setpts=PTS-STARTPTS,"
                    f"crop={cw}:{ch}:{cx}:{cy},"
                    f"scale={size}:{size_h}:flags=lanczos,setsar=1"
                    f"[ps{i}hud];"
                    f"{output}[ps{i}hud]overlay=x=20:y=H-h-20:format=auto"
                    f"{hud_out};"
                )
                output = hud_out

        # A short dip through black on every scene change. It costs no time --
        # unlike a crossfade it does not shorten the video and therefore cannot
        # desynchronise the untouched audio -- and it takes the hard edge off
        # the layout cuts that a podcast edit makes every few seconds.
        if fade_d:
            scene_dur = end - start
            signature = (scene.get("mode"),
                         tuple(t.get("rect") for t in (scene.get("tiles") or [])))
            cut_in = i > 0 and signature != (
                scenes[i - 1].get("mode"),
                tuple(t.get("rect") for t in (scenes[i - 1].get("tiles") or [])))
            cut_out = i < len(scenes) - 1 and signature != (
                scenes[i + 1].get("mode"),
                tuple(t.get("rect") for t in (scenes[i + 1].get("tiles") or [])))
            fade_tags = []
            if cut_in:
                fade_tags.append(f"fade=t=in:st=0:d={fade_d}")
            if cut_out and scene_dur > fade_d * 1.5:
                fade_tags.append(
                    f"fade=t=out:st={max(0.0, scene_dur - fade_d):.3f}"
                    f":d={fade_d}")
            if fade_tags:
                faded = f"[ps{i}fade]"
                parts.append(f"{output}{','.join(fade_tags)}{faded};")
                output = faded

        # Every scene must share the same SAR before concat; a crop from a
        # scaled tile can otherwise carry a fractional SAR and ffmpeg rejects
        # the whole timeline.
        sar_out = f"[ps{i}sar]"
        parts.append(
            f"{output}scale={width}:{height}:"
            f"force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=sar=1{sar_out};")
        output = sar_out
        outputs.append(output)

    broll_out = locals().get("broll_out")
    final = "[framedbase]" if broll_out else "[framed]"
    if len(outputs) == 1:
        # Give the rest of the render pipeline its stable public label.
        parts.append(f"{outputs[0]}null{final};")
    else:
        parts.append(f"{''.join(outputs)}concat=n={len(outputs)}:v=1:a=0{final};")
    if broll_out:
        windows = plan.get("broll") or []
        if windows:
            # A cross-dissolve, not a hard switch: the cutaway's alpha ramps up
            # and back down at each window's edges. Toggling the overlay on and
            # off popped a full-frame shot in for a second and a half and read
            # as a glitch. The gate still bounds it, so outside the windows the
            # overlay costs nothing and cannot leak through.
            # One faded copy per window, composited in turn: chained alpha
            # fades on a single stream multiply, so a later window's fade-in
            # would hold every earlier window at zero.
            parts.append(
                f"{broll_out}format=yuva420p,split={len(windows)}"
                + "".join(f"[brw{k}]" for k in range(len(windows))) + ";")
            base = "[framedbase]"
            for k, (a, b) in enumerate(windows):
                out = "[framed]" if k == len(windows) - 1 else f"[brl{k}]"
                parts.append(
                    f"[brw{k}]fade=t=in:st={a:.3f}:d={BROLL_FADE:.2f}:alpha=1,"
                    f"fade=t=out:st={max(a, b - BROLL_FADE):.3f}:d={BROLL_FADE:.2f}"
                    f":alpha=1[brf{k}];"
                    f"{base}[brf{k}]overlay=0:0:format=auto:"
                    f"enable='between(t,{a:.3f},{b:.3f})':eof_action=pass{out};")
                base = out
    # Every part above ends in ";" so they can be concatenated, which leaves one
    # trailing. The caller appends its own ";"-prefixed stages, and ";;" is an
    # empty filter: ffmpeg rejects the whole graph with "No such filter: ''".
    return "".join(parts).rstrip(";")


def smart_filter(plan: dict, width: int, height: int,
                 transitions: bool = False) -> str:
    """Composite a reframe plan from `pipeline.reframe` into a vertical canvas.

    Two shapes, because a podcast has two shapes. One speaker gets a single
    portrait window that follows them; two speakers get a window each, stacked.
    Both fill the canvas edge to edge -- the whole point of doing the detection
    work is that no part of the frame ends up as padding.
    """
    from app.pipeline import reframe

    if plan.get("mode") == "adaptive":
        return _adaptive_filter(plan, width, height, transitions=transitions)

    # Legacy v1 plans are still understood by the renderer. The editor's cache
    # is versioned and no longer returns them, but keeping this path makes an
    # in-flight job deploy-safe.
    win_w, win_h = plan["win"]
    # Even dimensions: libx264 rejects odd ones on yuv420p.
    win_w -= win_w % 2
    win_h -= win_h % 2

    # lanczos on every scale below, because this path is the only one that
    # genuinely ENLARGES: a 607-wide crop of a 1080p source becomes a 1080-wide
    # canvas, a 1.8x upscale. ffmpeg's default bicubic is built for downscaling
    # and turns that into mush; lanczos keeps the edge detail, at a cost too
    # small to measure next to the encode.

    if plan["mode"] == "stacked":
        tile_h = height // 2
        tile_h -= tile_h % 2
        parts = ["[0:v]split=2[a][b];"]
        for tag, label in (("a", "top"), ("b", "bottom")):
            keys = plan["windows"][0 if tag == "a" else 1]
            x = reframe.expr([(t, v) for t, v, _ in keys], 0, 0)
            y = reframe.expr([(t, v) for t, _, v in keys], 0, 0)
            # The window is expressed by its CENTRE; crop wants a corner.
            parts.append(
                f"[{tag}]crop={win_w}:{win_h}:'({x})-{win_w // 2}':'({y})-{win_h // 2}',"
                f"scale={width}:{tile_h}:flags=lanczos,"
                f"unsharp=5:5:0.45:5:5:0.0,setsar=1[{label}];"
            )
        parts.append("[top][bottom]vstack=inputs=2[framed]")
        return "".join(parts)

    keys = plan["windows"][0]
    x = reframe.expr([(t, v) for t, v, _ in keys], 0, 0)
    y = reframe.expr([(t, v) for t, _, v in keys], 0, 0)
    return (
        f"[0:v]crop={win_w}:{win_h}:'({x})-{win_w // 2}':'({y})-{win_h // 2}',"
        f"scale={width}:{height}:flags=lanczos,"
        f"unsharp=5:5:0.45:5:5:0.0,setsar=1[framed]"
    )


def remap_reframe_plan(plan: dict, clip_start: float, segments: list) -> dict:
    """Move a visual plan onto the pause-tightened output timeline."""
    if not plan or not segments:
        return plan
    from app.pipeline import pacing

    if plan.get("mode") != "adaptive":
        return dict(plan, windows=[
            [(pacing.remap_time(clip_start + t, segments), x, y)
             for t, x, y in keys]
            for keys in plan["windows"]
        ])

    duration = sum(end - start for start, end in segments)
    scenes = []
    for scene in plan.get("scenes", []):
        mapped_start = pacing.remap_time(clip_start + scene["start"], segments)
        mapped_end = pacing.remap_time(clip_start + scene["end"], segments)
        if mapped_end - mapped_start <= 0.02:
            continue
        tiles = []
        for tile in scene.get("tiles", []):
            keys = [
                (pacing.remap_time(clip_start + t, segments), x, y)
                for t, x, y in tile.get("keys", [])
            ]
            tiles.append(dict(tile, keys=keys))
        scenes.append(dict(scene, start=mapped_start, end=mapped_end, tiles=tiles))

    if not scenes:
        return None
    scenes[0]["start"] = 0.0
    scenes[-1]["end"] = duration
    return dict(plan, duration=duration, scenes=scenes)


def fit_caption_margin_v(src_w: int, src_h: int,
                         out_w: int = OUT_W, out_h: int = OUT_H) -> int:
    """Caption baseline for the fit frame.

    Same problem the podcast frame has: `fit` biases its video block upward, so
    the lower bar is larger than the generic margin's "half the leftover height"
    assumption. Using that formula put captions well above the middle of the bar
    they are supposed to sit in.
    """
    if _landscape_margin(out_w, out_h) is not None:
        return _landscape_margin(out_w, out_h)
    fg_h = content_height(src_w, src_h, out_w, out_h) if src_w and src_h else out_h
    top = max(0, min(out_h - fg_h, FIT_CONTENT_BIAS * out_h - fg_h / 2))
    band_top = top + fg_h
    band = max(out_h - band_top, 1)
    floor = int(out_h * (PLATFORM_UI_FRACTION + CAPTION_LIFT))
    return max(floor, int(out_h - (band_top + band * 0.45)))


def fill_filter(width: int, height: int) -> str:
    """Edge-to-edge: scale up and crop. No bars, but the sides are lost."""
    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}[framed]"
    )


def split_filter(width: int, height: int, gameplay_index: int) -> str:
    """Speaker on top, looping gameplay below -- the retention-bait format."""
    half = height // 2
    return (
        f"[0:v]scale={width}:{half}:force_original_aspect_ratio=increase,"
        f"crop={width}:{half}[top];"
        f"[{gameplay_index}:v]scale={width}:{half}:force_original_aspect_ratio=increase,"
        f"crop={width}:{half}[bot];"
        f"[top][bot]vstack=inputs=2[framed]"
    )


# Speed change, two honestly different effects:
#
#   "natural"  atempo    -- resamples in the time domain, pitch preserved. This
#                           is what you want for "make it punchier".
#   "pitched"  asetrate  -- replays the samples at a different rate, so pitch
#                           moves with speed. Chipmunk up, demon down. Deliberate
#                           meme effect, not a bug.
#
# atempo only accepts 0.5-2.0 per instance, so faster/slower gets chained.
MIN_SPEED = 0.5
MAX_SPEED = 3.0


def _atempo_chain(speed: float) -> str:
    """atempo clauses whose product is `speed`, each within ffmpeg's 0.5-2.0."""
    parts = []
    remaining = speed
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 0.001:
        parts.append(f"atempo={remaining:.4f}")
    return ",".join(parts) or "anull"


def speed_filters(speed: float, pitched: bool, sample_rate: int = 44100) -> tuple:
    """Returns (video_filter, audio_filter) for a playback speed change."""
    speed = max(MIN_SPEED, min(MAX_SPEED, float(speed)))
    video = f"setpts={1.0 / speed:.6f}*PTS"
    if pitched:
        # Pitch rides along with speed, then resample back to a normal rate so
        # downstream filters and the encoder see the sample rate they expect.
        audio = (f"asetrate={int(sample_rate * speed)},"
                 f"aresample={sample_rate},atempo=1.0")
    else:
        audio = _atempo_chain(speed)
    return video, audio


def split_caption_margin_v(height: int) -> int:
    """Puts captions just below the speaker/gameplay seam at height/2."""
    return height // 2 - 80


def _segments_filter(segments: list, want_audio: bool) -> tuple:
    """Cuts the source to `segments` and concatenates them, dropping the gaps.

    Returns (filter_string, video_label, audio_label). Timestamps are reset per
    piece with setpts/asetpts, otherwise concat keeps the original PTS and the
    output stalls for exactly as long as the gap we just removed.

    Note the input here must NOT be pre-seeked with -ss: these are absolute
    source times.
    """
    parts = []
    for n, (seg_start, seg_end) in enumerate(segments):
        parts.append(
            f"[0:v]trim=start={seg_start:.3f}:end={seg_end:.3f},"
            f"setpts=PTS-STARTPTS[sv{n}]"
        )
        if want_audio:
            parts.append(
                f"[0:a]atrim=start={seg_start:.3f}:end={seg_end:.3f},"
                f"asetpts=PTS-STARTPTS[sa{n}]"
            )

    if want_audio:
        joins = "".join(f"[sv{n}][sa{n}]" for n in range(len(segments)))
        parts.append(f"{joins}concat=n={len(segments)}:v=1:a=1[cv][ca]")
        return ";".join(parts), "[cv]", "[ca]"

    joins = "".join(f"[sv{n}]" for n in range(len(segments)))
    parts.append(f"{joins}concat=n={len(segments)}:v=1[cv]")
    return ";".join(parts), "[cv]", None


def whoosh_path():
    """The bundled scene-change whoosh, or None when the asset is absent."""
    path = os.path.join(os.path.dirname(__file__), "..", "..",
                        "assets", "sfx", "whoosh.wav")
    return os.path.abspath(path) if os.path.exists(path) else None


def _ff_escape(text: str) -> str:
    """Escape a string for use inside an ffmpeg filter argument."""
    return (text.replace("\\", "\\\\").replace(":", "\\:")
                .replace("'", "\u2019").replace("%", "\\%")
                .replace(",", "\\,").replace("[", "\\[").replace("]", "\\]"))


def _watermark_font() -> str:
    """A bold font file drawtext can actually load.

    drawtext needs a FILE, not a family, and the shipped caption fonts are not
    present in every checkout -- so fall back through the fonts a Linux box
    reliably has rather than failing the render over a watermark.
    """
    from app.pipeline.subtitles import FONTS_DIR
    candidates = []
    if os.path.isdir(FONTS_DIR):
        candidates += [os.path.join(FONTS_DIR, n) for n in sorted(os.listdir(FONTS_DIR))
                       if "bold" in n.lower() and n.lower().endswith((".ttf", ".otf"))]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


def watermark_filter(text: str, video_label: str, height: int) -> tuple:
    """(filter, new_label) drawing a burned-in credit at the foot of the frame.

    Burned into the pixels on purpose: this is what a free clip carries when it
    is posted, so it has to survive being re-uploaded, re-encoded and cropped by
    a platform. It sits BELOW the caption band -- captions are the thing people
    are reading -- and inside the safe area so a platform's own UI does not
    cover it, which would defeat the point entirely.
    """
    font = _watermark_font()
    if not font or not text:
        return "", video_label

    size = max(22, int(height * 0.022))
    pad = int(height * 0.028)
    return (
        f"{video_label}drawtext=fontfile='{_ff_escape(font)}'"
        f":text='{_ff_escape(text)}'"
        f":fontcolor=white@0.9:fontsize={size}"
        f":box=1:boxcolor=black@0.35:boxborderw={max(8, size // 3)}"
        f":x=(w-text_w)/2:y=h-text_h-{pad}[wm]",
        "[wm]",
    )


def _hw_encode_flags(high_quality: bool, reframe_plan) -> list[str]:
    """Pick encoder flags: hardware (VideoToolbox) when available, else libx264."""
    if HW_ENCODER == "h264_videotoolbox":
        # VideoToolbox uses -q:v (1-100, higher = better) instead of CRF.
        # These sat at 65/55, which is visibly soft on 9:16 text and on a crop
        # that has already been enlarged ~1.8x -- exactly the two things every
        # clip here is made of.
        quality = "50" if high_quality else "58"
        return ["-c:v", "h264_videotoolbox", "-q:v", quality]
    if high_quality:
        crf = "17" if reframe_plan else "18"
        return ["-c:v", "libx264", "-preset", "slow", "-crf", crf]
    crf = "18" if reframe_plan else "19"
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", crf]


def render_clip(
    video_path: str,
    start: float,
    end: float,
    out_path: str,
    subtitle_path: str = None,
    audio_override_path: str = None,
    gameplay_path: str = None,
    ratio: str = "9:16",
    frame: str = "blur",
    segments: list = None,
    mute_spans: list = None,
    overlay_list: list = None,
    speed: float = None,
    speed_pitched: bool = False,
    background: str = None,
    reframe_plan: dict = None,
    emoji_overlays: list = None,
    transitions: bool = False,
    sfx_events: list = None,
    music_path: str = None,
    music_gain: float = 0.16,
    watermark: str = None,
    high_quality: bool = False,
) -> str:
    duration = max(end - start, 1)
    width, height = RATIOS.get(ratio, RATIOS["9:16"])

    # Pause-tightening needs absolute source timestamps, so the input cannot be
    # pre-seeked; trim= inside the filter graph does the seeking instead.
    if segments:
        cmd = ["ffmpeg", "-y", "-i", video_path]
    else:
        cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", video_path]
    cmd_inputs = [video_path]        # index 0

    source_has_audio = has_audio(video_path)
    if audio_override_path:
        cmd += ["-i", audio_override_path]
        cmd_inputs.append(audio_override_path)
        audio_map = ["-map", "1:a"]
        extra = ["-shortest"]
    elif source_has_audio:
        audio_map = ["-map", "0:a"]
        extra = []
    else:
        # Silent source: export the picture and skip audio entirely. A player
        # handles a video-only MP4 cleanly; ffmpeg does not handle a map that
        # points at a stream the file never had.
        audio_map = []
        extra = []

    # Pause-tightening runs first, so the framing filters operate on the already
    # shortened stream. Its output is relabelled to [0:v]/[0:a] equivalents by
    # feeding the concat labels into the framing filter below.
    prefix = ""
    src_v, src_a = "[0:v]", "[0:a]"
    if segments:
        want_audio = (not audio_override_path) and source_has_audio
        prefix, src_v, src_a = _segments_filter(segments, want_audio)
        prefix += ";"
        duration = sum(e - s for s, e in segments)
        if not want_audio:
            src_a = None

    # The zoom policy needs the real source dimensions; without them the frames
    # that use it fall back to plain letterboxing. Probed once here because both
    # `blur` and `fit` want it now.
    try:
        sw, sh = probe_dimensions(video_path)
    except (subprocess.SubprocessError, ValueError, KeyError, IndexError):
        sw = sh = None

    if gameplay_path:
        # -stream_loop -1 loops short gameplay files; the output -t caps length.
        gameplay_input_index = 2 if audio_override_path else 1
        cmd += ["-stream_loop", "-1", "-i", gameplay_path]
        cmd_inputs.append(gameplay_path)
        filter_complex = split_filter(width, height, gameplay_input_index)
    elif frame == "fit":
        filter_complex = fit_filter(width, height, background, sw, sh)
    elif frame in ("auto", "podcast") and reframe_plan:
        # Face-aware crop, which is the whole point of the podcast template.
        #
        # Rebase the keyframes when pauses were cut. The plan is keyed to clip
        # time, but this filter reads `t` from the stream AFTER the tightening
        # concat -- so on a clip with dead air removed, every crop move fired
        # late by the total removed so far, drifting further out toward the end.
        plan = remap_reframe_plan(reframe_plan, start, segments)
        filter_complex = (smart_filter(plan, width, height, transitions=transitions)
                          if plan else podcast_filter(width, height))
    elif frame == "auto":
        # The auto planner found nothing it could frame better than the plain
        # treatment: letterbox the whole frame rather than guess a crop.
        filter_complex = fit_filter(width, height, background, sw, sh)
    elif frame == "podcast":
        # No plan: detection found nothing trackable (a screen share, a wide
        # stage shot, b-roll). The fixed letterbox is the honest answer there --
        # a confident crop of footage with no subject in it is worse.
        filter_complex = podcast_filter(width, height)
    elif frame == "fill":
        filter_complex = fill_filter(width, height)
    else:
        filter_complex = blur_filter(width, height, sw, sh)

    if segments:
        # Point the framing filter at the concatenated stream. An ffmpeg output
        # label may only be consumed once, and blur_filter reads the source
        # twice (background + foreground), so fan it out with split first.
        uses = filter_complex.count("[0:v]")
        if uses > 1:
            labels = [f"[cvs{i}]" for i in range(uses)]
            prefix += f"{src_v}split={uses}{''.join(labels)};"
            for label in labels:
                filter_complex = filter_complex.replace("[0:v]", label, 1)
        else:
            filter_complex = filter_complex.replace("[0:v]", src_v)
        filter_complex = prefix + filter_complex
        if src_a:
            audio_map = ["-map", src_a]

    # Auto-censor: silence exactly the profane words. Times are already on the
    # output timeline. Skipped under a voiceover override -- that audio is
    # generated text and never profane.
    if mute_spans and not audio_override_path and audio_map:
        gates = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in mute_spans)
        src = audio_map[1]
        filter_complex += f";{src if src.startswith('[') else '[' + src + ']'}"                           f"volume=enable='{gates}':volume=0[aout]"
        audio_map = ["-map", "[aout]"]

    video_label = "[framed]"

    if subtitle_path:
        # subtitles filter must run after the video is in its final [framed] state.
        # fontsdir makes the shipped caption fonts available to libass on any
        # machine -- without it, font choice would silently depend on the host.
        from app.pipeline.subtitles import FONTS_DIR
        escaped = subtitle_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        fonts_escaped = FONTS_DIR.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        filter_complex += f";[framed]subtitles='{escaped}':fontsdir='{fonts_escaped}'[v]"
        video_label = "[v]"

    # Sound design: short whooshes on the scene cuts, mixed under the real
    # audio rather than replacing it. `amix` with duration=first keeps the
    # source track's length so a 0.35s effect can never extend the clip.
    if sfx_events and audio_map:
        base = audio_map[1]
        base_label = base if base.startswith("[") else f"[{base}]"
        sfx_parts = []
        sfx_labels = []
        for n, event in enumerate(sfx_events):
            path = event.get("path")
            if not path or not os.path.exists(path):
                continue
            index = len(cmd_inputs)
            cmd += ["-i", path]
            cmd_inputs.append(path)
            ms = int(max(0.0, float(event.get("time", 0.0))) * 1000)
            gain = max(0.0, min(1.0, float(event.get("gain", 0.28))))
            sfx_parts.append(
                f"[{index}:a]aformat=sample_rates=44100:"
                f"channel_layouts=stereo,atrim=0:0.6,asetpts=PTS-STARTPTS,"
                f"adelay={ms}|{ms},volume={gain:.3f}[sfx{n}]"
            )
            sfx_labels.append(f"[sfx{n}]")
        if sfx_parts:
            filter_complex += ";" + ";".join(sfx_parts)
            filter_complex += (
                f";{base_label}{''.join(sfx_labels)}"
                f"amix=inputs={1 + len(sfx_labels)}:duration=first:normalize=0"
                f"[amixed]"
            )
            audio_map = ["-map", "[amixed]"]

    # Color emoji, composited as PNGs above the captions. They cannot be font
    # glyphs here: libass's CoreText provider will not rasterise a color emoji
    # face, so the only way to get the reference's color emoji is to draw them.
    if emoji_overlays:
        pieces = []
        for n, emo in enumerate(emoji_overlays):
            path = emo.get("path")
            if not path or not os.path.exists(path):
                continue
            index = len(cmd_inputs)
            cmd += ["-loop", "1", "-i", path]
            cmd_inputs.append(path)
            size = max(32, int(emo.get("size") or 100))
            start = max(0.0, float(emo.get("start", 0.0)))
            end = max(start + 0.2, float(emo.get("end", start + 0.5)))
            out = f"[emo{n}]"
            x, y = int(emo.get("x", 0)), int(emo.get("y", 0))
            gate = (f"enable='between(t,{start:.3f},{end:.3f})'"
                    f":eof_action=pass")
            pieces.append(
                f"[{index}:v]scale={size}:{size}:flags=lanczos,format=rgba,"
                f"fade=t=in:st=0:d=0.10:alpha=1,"
                f"setpts=PTS-STARTPTS+{start:.3f}/TB,"
                f"split=2[es{n}][ec{n}];"
                # A soft drop shadow, the way the reference emoji sits on the
                # picture instead of looking pasted flat onto it.
                f"[es{n}]colorchannelmixer=rr=0:gg=0:bb=0:aa=0.45[esh{n}];"
                f"{video_label}[esh{n}]overlay=x={x + 6}:y={y + 8}:{gate}"
                f"[evsh{n}];"
                f"[evsh{n}][ec{n}]overlay=x={x}:y={y}:{gate}{out}"
            )
            video_label = out
        if pieces:
            filter_complex += ";" + ";".join(pieces)

    # Overlays go on last so a logo or handle sits above the captions, and so
    # their coordinates refer to the final canvas rather than the source frame.
    if overlay_list:
        from app.pipeline import overlays as _ov
        next_input = len(cmd_inputs)
        extra_inputs, ov_filter, video_label = _ov.build(
            overlay_list, width, height, video_label, next_input,
            os.path.dirname(os.path.abspath(out_path)))
        for path in extra_inputs:
            cmd += ["-i", path]
        if ov_filter:
            filter_complex += ";" + ov_filter

    if speed and abs(speed - 1.0) > 0.001:
        vf, af = speed_filters(speed, speed_pitched)
        filter_complex += f";{video_label}{vf}[vspd]"
        video_label = "[vspd]"
        if audio_map:
            a_in = audio_map[1]
            a_in = a_in if a_in.startswith("[") else f"[{a_in}]"
            filter_complex += f";{a_in}{af}[aspd]"
            audio_map = ["-map", "[aspd]"]
        duration = duration / max(speed, 0.001)

    # Music bed, ducked under the voice. Added after speed so the bed is trimmed
    # to the final duration, and before loudnorm so the finished mix is normalised
    # as one thing. The sidechain is the speech itself: the bed drops ~8 dB under
    # a line and comes back in the gaps, which is what stops a bed from fighting
    # the dialogue.
    if music_path and os.path.exists(music_path):
        index = len(cmd_inputs)
        cmd += ["-stream_loop", "-1", "-i", music_path]
        cmd_inputs.append(music_path)
        gain = max(0.0, min(0.6, float(music_gain)))
        fade_out = max(0.0, duration - 0.8)
        bed = (f"[{index}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
               f"atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
               f"volume={gain:.3f},afade=t=in:st=0:d=0.35")
        if duration > 1.0:
            bed += f",afade=t=out:st={fade_out:.3f}:d=0.8"
        bed += "[musicbed]"
        filter_complex += ";" + bed
        if audio_map:
            base = audio_map[1]
            base = base if base.startswith("[") else f"[{base}]"
            filter_complex += (
                f";{base}aformat=sample_rates=44100:channel_layouts=stereo,"
                f"asplit=2[sp][sc]"
                f";[musicbed][sc]sidechaincompress=threshold=0.04:ratio=10"
                f":attack=8:release=320[musicduck]"
                f";[sp][musicduck]amix=inputs=2:duration=first:normalize=0"
                f"[amusic]"
            )
            audio_map = ["-map", "[amusic]"]
        else:
            # Silent footage: a bed is the only audio track it will have.
            audio_map = ["-map", "[musicbed]"]

    # Loudness and edge fades. Platforms normalise playback, so a clip that is
    # 6 dB quieter than its neighbours feels amateur even when the picture is
    # right; -14 LUFS is the short-form target. The 40/60ms fades remove the
    # click that a hard cut at a non-zero sample produces.
    if audio_map:
        a_in = audio_map[1]
        a_in = a_in if a_in.startswith("[") else f"[{a_in}]"
        fade_out = max(0.0, duration - 0.06)
        norm = (f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
                f"afade=t=in:st=0:d=0.04")
        if duration > 0.15:
            norm += f",afade=t=out:st={fade_out:.3f}:d=0.06"
        filter_complex += f";{a_in}{norm}[anorm]"
        audio_map = ["-map", "[anorm]"]

    # Last of all, so nothing can be composited over it.
    if watermark:
        wm_filter, video_label = watermark_filter(watermark, video_label, height)
        if wm_filter:
            filter_complex += ";" + wm_filter

    video_map = video_label

    cmd += [
        "-filter_complex", filter_complex,
        "-map", video_map,
        *audio_map,
        "-t", str(duration),
        # veryfast/crf 20 encodes several times quicker than medium/crf 18 and the
        # difference is not visible once the clip is re-compressed by TikTok/Shorts.
        # The face-aware frames are the exception: they ENLARGE a crop ~1.8x, so
        # they arrive at the encoder already soft with no detail to spare, and
        # every later recompression starts from that. Measured side by side, the
        # preset barely moved -- crf did, so only crf moves here.
        #
        # High quality buys a slower preset and a lower crf. It is a paid option
        # precisely BECAUSE the note above is true for most clips: it costs real
        # encoder time for a difference the platforms usually recompress away.
        # Worth it for footage that will be re-cut or archived, not worth it by
        # default -- which is why the default did not change.
        *(_hw_encode_flags(high_quality, reframe_plan)),
        # Bounded on purpose. x264 allocates per-thread frame buffers sized from
        # the CPU count it can see, and in a container that is the HOST's count
        # -- 48 on Railway against 8 on a laptop -- while the memory ceiling
        # belongs to the container. At 1080x1920 through this filter graph that
        # is enough allocation to get the process OOM-killed, which arrives as
        # SIGKILL with an empty error and looks like a mystery. make_proxy
        # survives the same machine only because it scales the frames down
        # first. Costs little at veryfast, where the encoder is not the
        # bottleneck.
        "-threads", str(FFMPEG_THREADS),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        *extra,
        "-movflags", "+faststart",
        out_path,
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(ffmpeg_failure("clip render", cmd, proc))
    return out_path


# ---------------------------------------------------------------------------
# Preview proxy
# ---------------------------------------------------------------------------
# Editing should feel instant, and it cannot if every change costs an ffmpeg
# pass over a 1080p source. So the pipeline also writes a small, seek-friendly
# copy of the source that a browser can stream and scrub freely. The editor
# plays THAT, draws captions as DOM elements on top, and only renders a real
# MP4 when the user exports. Same idea as an NLE's proxy workflow.

PROXY_WIDTH = 640           # enough to judge framing and read captions
PROXY_CRF = 30              # small file; quality is irrelevant for a preview
PROXY_KEYFRAME_SECONDS = 1  # dense keyframes so scrubbing lands where you drop it

# How far either side of a clip the editor lets you scrub. The trim handles are
# clamped to this window, so nothing outside it is reachable -- and therefore
# nothing outside it is worth encoding. Must match CONTEXT in LiveEditor.jsx.
PROXY_CONTEXT_SECONDS = 30


def proxy_window(start: float, end: float, duration: float = None) -> tuple:
    """The (from, to) span of source the editor can actually reach for a clip."""
    lo = max(0.0, float(start) - PROXY_CONTEXT_SECONDS)
    hi = float(end) + PROXY_CONTEXT_SECONDS
    if duration:
        hi = min(hi, float(duration))
    return lo, max(hi, lo + 1.0)


# ffmpeg reports progress on STDERR, continuously, one line per update. So the
# last N characters of a failed run are nearly always "frame= 19 fps=5.4 ..."
# and the line that actually explains the failure has long since scrolled past.
# Tailing the buffer was hiding the diagnosis in every single report.
_FFMPEG_NOISE = re.compile(
    r"^\s*(frame=|size=|video:|audio:|Press \[q\]|built with|configuration:|"
    r"lib(av|sw|postproc)\w*\s|\[libx264|Output #|Input #|Stream #|Stream map|"
    r"Metadata:|Side data:|encoder\s*:|handler_name\s*:|vendor_id\s*:|"
    r"major_brand|minor_version|compatible_brands|cpb:|Duration:|"
    r"Last message repeated)"
)


def ffmpeg_failure(what: str, cmd: list, proc) -> str:
    """A message that names the cause, instead of echoing the progress meter.

    Three things go in, in order of how often they turn out to be the answer:

    1. THE EXIT CODE, which was previously computed and thrown away. A negative
       code means the kernel killed the process, and that is a completely
       different problem from ffmpeg rejecting its input -- it needs saying,
       because a killed process writes no error at all and the stderr simply
       stops mid-sentence, which reads like a truncated log rather than a
       diagnosis.
    2. The real error lines, filtered out of the progress spam.
    3. The command, so the filter graph can be reproduced by hand.

    The full stderr goes to the server console; only the summary travels.
    """
    code = proc.returncode
    lines = [l.rstrip() for l in (proc.stderr or "").splitlines() if l.strip()]

    # Unabridged, to the console. Whatever the summary decides to highlight, the
    # complete output has to be somewhere -- the summary is a guess about what
    # mattered, and a guess is a bad thing to be stuck with at 2am.
    print(f"[clipper] {what} failed (exit {code}). Full ffmpeg output:\n"
          + "\n".join(lines[-80:]))

    if code < 0:
        sig = -code
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = f"signal {sig}"
        detail = (
            f"{what} was KILLED by the system ({name}, exit {code}) rather than "
            f"failing on its own -- so ffmpeg never got to report anything, "
            f"which is why the log just stops."
        )
        if sig == signal.SIGKILL:
            detail += (
                "\n\nSIGKILL almost always means the container ran out of "
                "memory. x264 sizes its per-thread buffers from the CPU count "
                "it can see, and inside a container that is the HOST's count, "
                "while the memory ceiling is the container's -- so the same "
                "code that fits on a laptop can be killed on a bigger machine. "
                "CLIPPER_FFMPEG_THREADS caps it; raise the service's memory if "
                "it persists."
            )
        return detail

    errors = [l for l in lines if not _FFMPEG_NOISE.match(l)]
    body = "\n".join(errors[-12:]) if errors else "(ffmpeg printed no error text)"
    return (f"{what} failed with exit code {code}:\n{body}\n\n"
            f"command: {' '.join(cmd[:40])}")


def make_proxy(video_path: str, out_path: str,
               start: float = None, end: float = None) -> str:
    """Writes a small, densely-keyframed preview copy of the source.

    `start`/`end` cut a window out of the source instead of copying all of it,
    and that is the difference between the editor opening now and opening in
    several minutes. The proxy used to transcode the whole video: a 16-minute
    podcast re-encoded end to end so that someone could scrub a 45-second clip.
    Everything outside the trim window is unreachable in the editor, so encoding
    it bought nothing and cost all of the wait. The window is ~105s, so this is
    roughly a tenth of the work and finishes while the clips are still rendering.

    Written to a side file and swapped in atomically. This is not a nicety:
    ffmpeg fills an mp4 progressively and only writes the moov atom at the very
    end, so a half-finished preview is a file with a non-zero size that no
    browser can decode. The editor's readiness check is "does this file exist",
    so anyone who opened the editor during the encode got a lifted loading veil
    over a permanently black, unseekable video. With the swap, the file does not
    exist until it is playable.
    """
    part_path = out_path + ".part"
    fps_probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", video_path],
        capture_output=True, text=True,
    )
    try:
        num, _, den = fps_probe.stdout.strip().partition("/")
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    gop = max(1, int(round(fps * PROXY_KEYFRAME_SECONDS)))

    # -ss BEFORE -i seeks by index rather than by decoding up to the mark, which
    # on a 16-minute source is the difference between instant and a minute of
    # throwaway decoding. Since we re-encode, output timestamps restart at zero:
    # the proxy's t=0 is source time `start`, and the editor is told that offset.
    seek = ["-ss", f"{max(0.0, float(start)):.3f}"] if start is not None else []
    span = (["-t", f"{max(0.1, float(end) - float(start)):.3f}"]
            if start is not None and end is not None else [])

    cmd = ["ffmpeg", "-y", *seek, "-i", video_path, *span,
         "-vf", f"scale={PROXY_WIDTH}:-2",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(PROXY_CRF),
         "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
         "-threads", str(FFMPEG_THREADS),
         "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k",
         "-movflags", "+faststart",
         # Explicit, because the output is named .part: ffmpeg picks the muxer
         # from the file extension and "Unable to choose an output format for
         # preview.mp4.part" failed EVERY proxy the moment the atomic swap was
         # introduced. That is why the editor sat on "Preparing the editor
         # preview" indefinitely -- there was never a file coming.
         "-f", "mp4",
         part_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise RuntimeError(ffmpeg_failure("editor preview", cmd, proc))
    os.replace(part_path, out_path)
    return out_path
