"""Color emoji, composited as PNG stickers instead of font glyphs.

Why this is not a font
----------------------
The reference clips this product is measured against use full-color emoji. The
obvious route -- ship a color emoji TTF and let libass draw it -- does not work
on macOS: libass's CoreText provider ignores both Noto Color Emoji (CBDT) and
Twemoji (COLR) strikes, and Apple's own sbix face is refused by the FreeType
build. The result is a blank box, which is worse than a monochrome glyph.

So the renderer composites the emoji itself. Twemoji PNGs live in
`assets/emoji/`, keyed by lowercase codepoint. They are drawn after the
subtitles, above the caption block, exactly where the reference puts them --
floating beside the line rather than glued to the last word. It also means the
emoji is identical on every machine, with no font fallback to go wrong.

Density is the other half of the design. An emoji on every cue reads as
wallpaper; the reference uses roughly one per ten seconds. The caller decides
which cues earn one (see `caption_emoji`); this module only places them.
"""

import math
import os
import urllib.request

from app.pipeline import subtitles

EMOJI_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "assets", "emoji"))

# Twemoji PNGs are square 72px masters; scale to roughly the caption's own size
# so the emoji reads as part of the line rather than as a watermark.
EMOJI_SCALE = 1.35
MIN_PX = 48
MAX_PX = 240

# Where the sticker sits. The reference puts it just above the caption block,
# on the side away from the face; a fixed fraction of frame width is both close
# enough and immune to whatever wrapping libass chose.
EMOJI_X_FRACTION = 0.70

_VARIATION = 0xFE0F
_ZWJ = 0x200D


# Twemoji's PNG set is large (thousands of files, tens of MB). Bundling the
# common ones and fetching the rest on first use keeps the checkout sane while
# still letting the model pick the right emoji rather than the nearest one we
# happened to ship. Set CLIPPER_EMOJI_FETCH=0 to stay fully offline; missing
# marks are then simply dropped, never drawn as a blank glyph.
_FETCH = os.environ.get("CLIPPER_EMOJI_FETCH", "1").strip().lower() \
    not in ("0", "false", "no", "off")
_CDN = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{name}.png"
_CDN_SVG = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/svg/{name}.svg"
_FAILED = set()


def _download(name, path):
    if not _FETCH or name in _FAILED:
        return None
    try:
        request = urllib.request.Request(
            _CDN.format(name=name), headers={"User-Agent": "KlipCut/1.0"})
        with urllib.request.urlopen(request, timeout=4) as response:
            data = response.read(120_000)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            _FAILED.add(name)
            return None
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
        print(f"[clipper] fetched color emoji {name}")
        return path
    except Exception:
        _FAILED.add(name)
        return None


def _rasterise(name, svg_path, hi_path):
    """SVG -> 256px PNG, so a fetched emoji is as crisp as a bundled one."""
    try:
        import cairosvg
        cairosvg.svg2png(url=svg_path, write_to=hi_path,
                         output_width=256, output_height=256)
        return hi_path if os.path.exists(hi_path) else None
    except Exception:
        return None


def _fetch_svg(name):
    if not _FETCH:
        return None
    try:
        request = urllib.request.Request(
            _CDN_SVG.format(name=name), headers={"User-Agent": "KlipCut/1.0"})
        with urllib.request.urlopen(request, timeout=4) as response:
            data = response.read(80_000)
        if b"<svg" not in data[:300]:
            return None
        return data
    except Exception:
        return None


def asset_path(char):
    """Best available Twemoji artwork for one simple emoji, or None.

    Prefers the 256px render: the 72px masters are visibly soft once they are
    scaled to a 100px+ sticker, and the whole point of compositing them was to
    look like the reference rather than like a font fallback.
    """
    if not char:
        return None
    points = [ord(c) for c in char if ord(c) not in (_VARIATION, _ZWJ)]
    if len(points) != 1:
        return None
    name = f"{points[0]:x}"
    hi = os.path.join(EMOJI_DIR, "256", f"{name}.png")
    if os.path.exists(hi):
        return hi

    small = os.path.join(EMOJI_DIR, f"{name}.png")
    # An emoji we already ship at 72px: fetch its SVG once and upgrade it.
    if os.path.exists(small):
        svg = os.path.join(EMOJI_DIR, "svg", f"{name}.svg")
        if not os.path.exists(svg):
            data = _fetch_svg(name)
            if data:
                try:
                    with open(svg, "wb") as handle:
                        handle.write(data)
                except OSError:
                    pass
        if os.path.exists(svg):
            upgraded = _rasterise(name, svg, hi)
            if upgraded:
                return upgraded
        return small

    data = _fetch_svg(name)
    if data:
        svg = os.path.join(EMOJI_DIR, "svg", f"{name}.svg")
        try:
            with open(svg, "wb") as handle:
                handle.write(data)
        except OSError:
            return None
        upgraded = _rasterise(name, svg, hi)
        if upgraded:
            print(f"[clipper] fetched color emoji {name}")
            return upgraded
    # Older cached 72px asset from before the 256px set existed.
    if os.path.exists(small):
        return small
    return _download(name, small)


def _approx_lines(text, font_px, width):
    """How many caption lines libass will use, near enough to place a sticker.

    A full measurement would mean reproducing libass shaping and wrapping for a
    number that only decides whether the emoji floats 100px higher or lower.
    Average advance of a heavy sans at this size is ~0.60em; wrapping happens
    at the frame width minus the 60px side margins.
    """
    if not text:
        return 1
    usable = max(1.0, width - 120.0)
    per_line = max(6, int(usable / (font_px * 0.60)))
    return max(1, min(3, math.ceil(len(text) / per_line)))


def plan_overlays(cue_emoji, words, style, play_res=(1080, 1920),
                  margin_v=400, clip_start=0.0, max_words=None,
                  positions=None):
    """Turn a {cue: emoji} map into overlay placements for the renderer.

    Returns (inline, overlays):
      inline    {cue: emoji} for cues with no bundled PNG -- these keep the
                monochrome glyph rather than silently losing the mark
      overlays  [{path, start, end, x, y, size, cue}] in OUTPUT time
    """
    st = subtitles.STYLES.get(style, subtitles.STYLES["classic"])
    font_px = int(st.get("size", 76))
    limit = (max(1, min(8, int(max_words))) if max_words
             else st.get("max_words"))
    cues = list(subtitles.group_words(words, limit))
    width, height = play_res

    size = max(MIN_PX, min(MAX_PX, int(font_px * EMOJI_SCALE)))
    inline = {}
    overlays = []

    for index, mark in (cue_emoji or {}).items():
        path = asset_path(mark)
        if path is None or not (0 <= index < len(cues)):
            # No artwork means no emoji. Drawing a text glyph here is what puts
            # an empty box on screen on this renderer; losing the mark is the
            # cheaper failure and the one a viewer cannot see.
            continue
        cue = cues[index]
        text = " ".join(
            (w.get("punctuated_word") or w.get("word") or "") for w in cue
        ).strip()
        lines = _approx_lines(text, font_px, width)
        line_height = int(font_px * 1.16)
        # Follow the caption when the layout moves it. Without this the emoji
        # stayed at the plan's global baseline while the text jumped to the
        # seam on a split, leaving the sticker floating in the wrong place.
        cue_start = cue[0]["start"] - clip_start
        pos = None
        for span in positions or []:
            if span["start"] <= cue_start < span["end"]:
                pos = span["pos"]
                break
        block_bottom = (int(round(height * float(pos))) if pos is not None
                        else height - margin_v)
        block_top = block_bottom - lines * line_height

        # Default: float above the block, on the right -- the reference's shape.
        y = block_top - size - int(font_px * 0.06)
        x = int(width * EMOJI_X_FRACTION) - size // 2
        if block_top < height * 0.55:
            # Captions sit high (a reaction layout puts them just under the
            # seam). Floating above would push the emoji onto a face, so put it
            # beside the block instead.
            y = block_bottom - size - int(font_px * 0.12)
            x = int(width * 0.80) - size // 2

        x = max(12, min(width - size - 12, x))
        y = max(12, min(height - size - 12, y))

        start = max(0.0, cue[0]["start"] - clip_start)
        end = max(start + 0.4, cue[-1]["end"] - clip_start)
        overlays.append({
            "path": path,
            "start": round(start, 3),
            "end": round(end, 3),
            "x": int(x),
            "y": int(y),
            "size": int(size),
            "cue": index,
        })

    return inline, overlays
