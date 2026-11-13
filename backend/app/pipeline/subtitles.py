"""Builds short-form captions as an ASS subtitle file with word-by-word highlighting.

Why ASS instead of SRT
----------------------
SRT carries no styling, so ffmpeg's `subtitles` filter falls back to libass
defaults -- including PlayResY=288. Every FontSize and MarginV then gets scaled
by 1920/288, which is how a MarginV of 120 ended up ~800px off the bottom,
landing captions in the middle of the frame on top of people's faces.

Writing ASS ourselves lets us declare PlayResX/PlayResY as the real 1080x1920
output, so every number below is honest pixels, and lets us emit one event per
word so the active word can be highlighted as it is spoken.
"""

# --- caption feel -------------------------------------------------------------
MAX_WORDS_PER_CUE = 3          # keep lines short enough to read in a glance

# Caption sizes are px on the 1080x1920 reference canvas -- the same number the
# editor shows. Bounds keep text readable without swallowing the frame.
# Captions are burned in BEFORE the speed filter runs, so subtitle timings stay
# on the ORIGINAL timeline and the speed change carries them along with the
# picture. Scaling them here as well would double-apply the speed.
# ---------------------------------------------------------------------------
# Caption animation
# ---------------------------------------------------------------------------
# How a cue ENTERS the frame, independent of how it is coloured. Style says what
# captions look like; animation says how they arrive. Kept separate so any style
# can wear any animation.
#
# ASS override tags, applied per cue:
#   \fad(in,out)          fade in/out, milliseconds
#   \t(t0,t1,tags)        interpolate tags between two times
#   \move(x0,y0,x1,y1,..) slide the line between two points
#   \fscx/\fscy           horizontal/vertical scale, percent
#
# Durations stay short (<=220ms). Anything slower reads as sluggish on a clip
# where each cue is on screen for well under two seconds.
ANIMATIONS = {
    "none":    {"name": "None",      "desc": "Cuts straight in"},
    "fade":    {"name": "Fade",      "desc": "Soft fade in and out"},
    "pop":     {"name": "Pop",       "desc": "Scales up with a slight overshoot"},
    "riseup":  {"name": "Rise",      "desc": "Slides up into place"},
    "punch":   {"name": "Punch",     "desc": "Snaps in oversized, settles"},
    "bounce":  {"name": "Bounce",    "desc": "Overshoots then springs back"},
}


def animation_tags(anim: str, cue_ms: int, play_res: tuple, margin_v: int) -> str:
    """Leading ASS override tags that animate a cue's entrance.

    cue_ms is the cue's own duration, so effects never outlast the line they
    belong to -- a 200ms entrance on a 150ms cue would simply never finish.
    """
    if not anim or anim == "none":
        return ""
    span = max(60, min(220, cue_ms // 3))

    # Fade-in only, NEVER a fade-out here. A cue is emitted as one Dialogue line
    # PER WORD (that is how the karaoke highlight works), so a fade-out on this
    # line would fade the caption away after the first word and the next word's
    # line would cut back in -- read as a flicker, not an entrance.
    if anim == "fade":
        return f"\\fad({span},0)"

    if anim == "pop":
        return f"\\fscx60\\fscy60\\t(0,{span},\\fscx100\\fscy100)\\fad({span // 2},0)"

    if anim == "punch":
        return (f"\\fscx130\\fscy130\\t(0,{span},\\fscx100\\fscy100)"
                f"\\fad({span // 3},0)")

    if anim == "bounce":
        half = max(30, span // 2)
        return (f"\\fscx70\\fscy70\\t(0,{half},\\fscx112\\fscy112)"
                f"\\t({half},{span},\\fscx100\\fscy100)\\fad({half},0)")

    if anim == "riseup":
        w, h = play_res
        y_end = h - margin_v
        y_start = y_end + int(h * 0.045)
        return f"\\move({w // 2},{y_start},{w // 2},{y_end},0,{span})\\fad({span},0)"

    return ""


MIN_CAPTION_PX = 28
MAX_CAPTION_PX = 160

# Shipped caption fonts (all SIL Open Font License -- free for commercial use).
# `family` is the name INSIDE the ttf, which is what libass matches on; the
# renderer points ffmpeg's fontsdir at FONTS_DIR so these work on any machine,
# not just one that happens to have them installed.
import os as _os
import re as _re
FONTS_DIR = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", "assets", "fonts"))
def _family_of(path: str) -> str:
    """The name libass will actually match this file by.

    This is fussier than it looks. libass asks its font provider for a family,
    and on a machine where the font is not installed system-wide the only thing
    that matches is name record 1 -- NOT record 16, the "typographic family".
    PIL returns record 16, which is why asking for "Poppins" silently rendered
    in Helvetica: the file calls itself "Poppins ExtraBold".

    When the subfamily is anything other than Regular it has to be appended too,
    or a weight-specific file never matches: "Mukta" misses, "Mukta ExtraBold"
    hits. Verified against libass's own fontselect output for every shipped face.
    """
    try:
        from fontTools.ttLib import TTFont
        f = TTFont(path, fontNumber=0, lazy=True)
        family = f["name"].getDebugName(1)
        subfamily = (f["name"].getDebugName(2) or "Regular").strip()
        if not family:
            raise ValueError("no name record 1")
        family = family.strip()
        if subfamily.lower() != "regular" and \
                not family.lower().endswith(subfamily.lower()):
            family = f"{family} {subfamily}"
        return family
    except Exception:
        pass
    try:
        from PIL import ImageFont
        return ImageFont.truetype(path, 12).getname()[0]
    except Exception:
        return None


def _has_devanagari(path: str) -> bool:
    """Whether this face can actually draw Hindi, rather than tofu boxes."""
    try:
        from fontTools.ttLib import TTFont          # optional, usually absent
        f = TTFont(path, fontNumber=0, lazy=True)
        return any(0x0915 in t.cmap for t in f["cmap"].tables if t.isUnicode())
    except Exception:
        pass
    try:
        from PIL import ImageFont
        return ImageFont.truetype(path, 12).getmask("\u0915").getbbox() is not None
    except Exception:
        return False


def _covers(path: str, codepoint: int) -> bool:
    """Whether this face can actually DRAW a codepoint through libass.

    A cmap entry is not enough, and believing it was is how the editor came to
    promise emoji that the export dropped. Noto Color Emoji maps U+1F600 and
    then stores the picture in a COLRv1 table, leaving the base outline empty
    (`numberOfContours == 0`); the FreeType libass is built against here does not
    composite COLRv1, so it finds the glyph, draws nothing, and reports no error.
    Apple Color Emoji fails the same way for a different reason -- `sbix`
    bitmaps this FreeType will not rasterise.

    So the test is "is there something a rasteriser can put on screen": real
    outlines (glyf contours or CFF), or a bitmap strike in a format this build
    does read. A monochrome emoji face such as Noto Emoji passes and burns in.
    """
    try:
        from fontTools.ttLib import TTFont, TTCollection
        f = (TTCollection(path).fonts[0] if path.lower().endswith(".ttc")
             else TTFont(path, fontNumber=0, lazy=True))
        name = None
        for t in f["cmap"].tables:
            if t.isUnicode() and codepoint in t.cmap:
                name = t.cmap[codepoint]
                break
        if name is None:
            return False
        if "CFF " in f or "CFF2" in f:
            return True
        if "CBDT" in f:                     # colour bitmaps FreeType can read
            return True
        glyf = f.get("glyf")
        if glyf is None:
            return False
        g = glyf[name]
        # Composite glyphs report -1 and are perfectly drawable.
        return g.numberOfContours != 0
    except Exception:
        return False


# Emoji burn-in is a font problem, not a text problem.
#
# The emoji survives everything we do: it reaches the .ass file as a literal
# character. libass then reports
#     Glyph 0x1F62D not found ... failed to find any fallback
# and draws nothing. macOS ships Apple Color Emoji as an `sbix` bitmap font, and
# the FreeType that libass (and Pillow) are built against here cannot rasterise
# sbix -- Pillow refuses the same file with "invalid pixel size". So there is no
# emoji-capable face on the fallback path at all.
#
# The fix is a font, not code: drop an emoji TTF that FreeType can read into
# assets/fonts -- Noto Emoji (plain outlines) always works, Noto Color Emoji
# (CBDT) works on most builds. This looks for one and reports whether captions
# can honestly promise emoji, so the editor can stop previewing something the
# export silently drops.
EMOJI_PROBE = 0x1F600           # GRINNING FACE, present in every emoji font


def emoji_font() -> dict:
    """The shipped face that can draw emoji, or None if there is none."""
    for entry in FONTS.values():
        if entry.get("emoji"):
            return entry
    return None


def _is_devanagari(text: str) -> bool:
    return any(0x0900 <= ord(c) <= 0x097F for c in text)


def devanagari_font() -> tuple:
    """(font id, family) of a shipped face that can draw Devanagari.

    Both halves are needed because the two styles resolve fonts differently: a
    caption style stores a FAMILY directly, while the title goes through
    title_look -> resolve_font, which expects an ID. Handing a family name to
    the ID path silently resolves to nothing and falls back to the very font
    that could not draw the text.

    Missing Devanagari is not a soft fallback the way a missing weight is:
    libass reports "failed to find any fallback with glyph 0x939", ffmpeg exits
    non-zero, and render_clip raises -- so a Hindi clip fails outright instead
    of rendering in the wrong face. The per-font coverage flag already exists
    (see _discover_fonts) and was only ever reported to the editor; this is what
    makes it decide something.
    """
    for key, entry in FONTS.items():
        if entry.get("devanagari"):
            return key, entry["family"]
    return None, None


def _covers_devanagari(family: str) -> bool:
    return any(e["family"] == family and e.get("devanagari")
               for e in FONTS.values())


def _romanize(text: str) -> str:
    """Devanagari as Roman Hinglish; any other non-Latin script as ASCII.

    unidecode alone spelled Hindi the way no one types it ("zabaradast",
    "nhiin"), so Devanagari goes through the Hinglish romaniser and unidecode
    only handles what is left.
    """
    from app.pipeline import hinglish
    text = hinglish.romanize_text(text) if _is_devanagari(text) else text
    try:
        from unidecode import unidecode
        return unidecode(text)
    except ImportError:
        return text


def _romanize_words(words: list) -> list:
    """Romanize the 'word'/'punctuated_word' fields of Deepgram word objects."""
    out = []
    for w in words:
        if not _is_devanagari(_word_text(w)):
            out.append(w)
            continue
        w = dict(w)
        if "punctuated_word" in w:
            w["punctuated_word"] = _romanize(w["punctuated_word"])
        if "word" in w:
            w["word"] = _romanize(w["word"])
        out.append(w)
    return out


def _fit_devanagari(st: dict, caption_text: str, title: str,
                    title_font: str) -> tuple:
    """(style, title_font) adjusted so each can draw the text it carries.

    Prefers romanization over a font swap: transliterating Devanagari to Latin
    keeps the user's chosen caption style intact. Falls back to a font swap
    only when unidecode is unavailable.
    """
    caption_needs = _is_devanagari(caption_text)
    title_needs = _is_devanagari(title or "")
    if not (caption_needs or title_needs):
        return st, title_font

    # Try romanization first -- keeps the chosen font.
    try:
        from unidecode import unidecode  # noqa: F811
        _has_unidecode = True
    except ImportError:
        _has_unidecode = False

    if _has_unidecode:
        # Romanization happens at the word level (in build_ass via
        # _romanize_words), so the font stays unchanged. Nothing to do here
        # for captions. Title is a plain string, romanize it directly.
        if title_needs:
            print(f"[clipper] title contains Devanagari; romanizing to keep "
                  f"{resolve_font(title_font) or TITLE_STYLE['font']}")
        if caption_needs:
            print(f"[clipper] captions contain Devanagari; romanizing to keep "
                  f"{st['font']}")
        return st, title_font

    # Fallback: swap to a Devanagari-capable font.
    font_id, family = devanagari_font()
    if not family:
        print("[clipper] text needs Devanagari but no shipped font provides it")
        return st, title_font

    if caption_needs and not _covers_devanagari(st["font"]):
        print(f"[clipper] captions contain Devanagari; using {family} instead "
              f"of {st['font']}, which cannot draw it")
        st = {**st, "font": family}

    title_now = resolve_font(title_font) or TITLE_STYLE["font"]
    if title_needs and not _covers_devanagari(title_now):
        print(f"[clipper] title contains Devanagari; using {family} instead of "
              f"{title_now}, which cannot draw it")
        title_font = font_id

    return st, title_font


def _discover_fonts() -> dict:
    """Every font in assets/fonts, keyed by a slug derived from its filename.

    Dropping a .ttf into the folder is all it takes to offer it -- there is no
    second list to keep in sync, which is exactly how the old hardcoded map
    ended up hiding eight installed faces.
    """
    found = {}
    if not _os.path.isdir(FONTS_DIR):
        return found
    for name in sorted(_os.listdir(FONTS_DIR)):
        if not name.lower().endswith((".ttf", ".otf")):
            continue
        path = _os.path.join(FONTS_DIR, name)
        stem = _os.path.splitext(name)[0]
        # "Baloo2-VariableFont_wght" -> "baloo2";  "LilitaOne-Regular" -> "lilitaone"
        slug = stem.split("-")[0].split("_")[0].lower()
        family = _family_of(path)
        if not family:
            continue
        label = _re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem.split("-")[0])
        found[slug] = {
            "family": family,
            "label": label,
            "file": name,
            "devanagari": _has_devanagari(path),
            "emoji": _covers(path, EMOJI_PROBE),
        }
    return found


FONTS = {
    # The system face the classic style was designed around; not a shipped file.
    "impact": {"family": "Arial Black", "label": "Impact",
               "file": None, "devanagari": False, "emoji": False},
}
FONTS.update(_discover_fonts())

# Slugs that older saved recipes used, before the registry was derived from the
# filenames on disk. A stored clip must keep rendering in the font it was made
# with, so these map forward rather than silently falling back to the default.
FONT_ALIASES = {"archivo": "archivoblack"}


def font_entry(font_id: str) -> dict:
    return FONTS.get(font_id) or FONTS.get(FONT_ALIASES.get(font_id, ""), None)


def resolve_font(font_id: str) -> str:
    """Family name for a font id; None means keep the style's own font."""
    entry = font_entry(font_id)
    return entry["family"] if entry else None
MAX_CUE_SECONDS = 1.8          # ...and swap them often enough to feel alive
MIN_WORD_SECONDS = 0.12        # floor so very fast words still register

# --- look ---------------------------------------------------------------------
# ASS colours are &HAABBGGRR -- byte order is reversed from hex you'd write in CSS.
# Each preset controls the base text, the spoken-word highlight, casing, and how
# hard the active word pops (fscx/fscy percentage).
# Each style varies on four independent axes, not just size:
#   position   where the caption block sits (bottom / middle / lower third)
#   box        "none" = outlined text, "word" = filled chip behind the spoken
#              word, "line" = a solid plate behind the whole caption
#   active     how the spoken word is marked (colour, and optionally scale)
#   uppercase  casing
# BorderStyle 3 is what makes a box possible at all: it renders the outline as
# a filled rectangle using OutlineColour, so an alpha-FF outline on the base
# style plus a per-word override gives a chip behind only the active word.
from app.pipeline import caption_presets
from app.pipeline.caption_presets import ACTIVE_EFFECTS, PRESETS   # noqa: F401

# The registry moved to caption_presets so the editor can be served the same
# definitions instead of keeping a hand-written copy. STYLES stays as the name
# the rest of the pipeline already imports.
STYLES = PRESETS


# Alignment codes: 2 = bottom-centre, 5 = middle-centre.
POSITIONS = {
    "bottom": {"alignment": 2},
    "lower": {"alignment": 2, "margin_frac": 0.13},
    "middle": {"alignment": 5},
}

COLOR_OUTLINE = "&H00000000"    # black
GLOW_STYLE = "CaptionGlow"
GLOW_SPREAD = 9                 # px the halo's stroke extends past the outline
GLOW_BLUR = 12


CHIP_STYLE = "CaptionChip"
BAR_STYLE = "CaptionBar"
# Every style that draws part of the caption. Per-scene placement has to move
# all of them together, or a halo, chip or bar stays behind when the text moves.
CAPTION_LAYER_STYLES = {"Caption", GLOW_STYLE, CHIP_STYLE, BAR_STYLE}

# The headline bar: a highlighter swipe behind the lower half of the big line.
# libass draws an opaque box from the font's own height, so the bar is that
# line again at BAR_RATIO of the size -- a box anchored on the same bottom edge
# and about half as tall -- with letter spacing restoring the full width.
# (Shrinking with \fscy instead hits VSFilter's double-scaled box geometry: the
# box either vanishes or covers the whole word.)
BAR_RATIO = 0.5
BAR_PAD_X = 12

# A headline line opens on a content word: a short function word goes up
# with the word after it rather than becoming the giant word on its own.
HEADLINE_STOP = {"the", "a", "an", "and", "so", "but", "or", "to", "of", "in",
                 "on", "at", "it", "is", "i", "we", "you", "my", "your", "this",
                 "that", "for", "with", "if"}


def _has_bar(st: dict) -> bool:
    return bool(st.get("bar")) and st.get("mode") == "headline"


def _font_metrics(path: str):
    """(cmap, advance table, units per em) of a font file, or None."""
    cached = _METRICS.get(path)
    if cached is not None or path in _METRICS:
        return cached
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(path, lazy=True)
        cached = (font.getBestCmap(), font["hmtx"].metrics, font["head"].unitsPerEm)
    except Exception:
        cached = None
    _METRICS[path] = cached
    return cached


_METRICS = {}


def text_width(font_id: str, text: str, size: float) -> float:
    """Advance width of `text` at `size` px in a shipped font, or 0 if unknown.

    Kerning is ignored, which is a few pixels on a headline -- inside the bar's
    padding.
    """
    entry = font_entry(font_id) if font_id else None
    if not entry or not entry.get("file"):
        return 0.0
    metrics = _font_metrics(_os.path.join(FONTS_DIR, entry["file"]))
    if not metrics:
        return 0.0
    cmap, hmtx, upem = metrics
    units = 0
    for ch in text:
        glyph = cmap.get(ord(ch))
        units += hmtx[glyph][0] if glyph in hmtx else upem * 0.5
    return units / upem * size


def _headline_split(plain: list) -> int:
    """How many of a cue's words go on the big first line of a headline."""
    if len(plain) >= 3 and _key_token(plain[0]) in HEADLINE_STOP:
        return 2
    return 1


def _bar_text(plain: list, rendered: list, st: dict, k: int) -> str:
    """The bar layer for a headline cue: line 1 boxed in the bar colour, line 2
    there only to hold the layout, invisible and unboxed."""
    top_plain = " ".join(plain[:k])
    if st.get("uppercase"):
        top_plain = top_plain.upper()
    size = st["size"]
    spacing = st.get("spacing", 0)
    width = text_width(st.get("font_id"), top_plain, size)
    if not width:
        return ""
    small_fs = max(8, int(round(size * BAR_RATIO)))
    # Spacing that brings the smaller run back to the big line's width.
    extra = spacing + (width - text_width(st.get("font_id"), top_plain, small_fs)) \
        / max(1, len(top_plain))
    line1 = (f"{{\\q2\\1a&HFF&\\3c{st['bar']}&\\3a&H00&\\fs{small_fs}"
             f"\\fsp{extra:.2f}\\xbord{BAR_PAD_X}\\ybord0}}" + " ".join(rendered[:k]))
    if k >= len(rendered):
        return line1
    small = int(round(st.get("stack_small", 58)))
    return (line1 + f"{{\\3a&HFF&\\fs{size}\\fsp{spacing}"
            f"\\fscx{small}\\fscy{small}}}\\N" + " ".join(rendered[k:]))


def _has_glow(st: dict) -> bool:
    """A halo needs outlined text: on a boxed preset it would blur the plate."""
    return bool(st.get("glow")) and st.get("box", "none") == "none"


def _has_chip_layer(st: dict) -> bool:
    """Whether the word chip is drawn on its own layer, under outlined text.

    A box style (BorderStyle 3) cannot also stroke its glyphs, so the classic
    chip presets show bare, unoutlined text for every word that is not being
    spoken -- fine on a dark plate, unreadable over a bright frame. A preset
    that sets `text_outline` gets the chip and the text as two layers instead.
    A "line" box does the same as a backdrop band behind outlined text.
    """
    return st.get("box") in ("word", "line") and st.get("text_outline") is not None


def _preset_style(style: str) -> dict:
    """A preset by name, with its shipped typeface resolved to a family.

    Presets name a font FILE by slug (`font_id`) so the editor can preview the
    same face; libass wants the family. If the file is not installed the
    preset's raw `font` stands, exactly as before presets could name one.
    """
    st = STYLES.get(style, STYLES["classic"])
    family = resolve_font(st.get("font_id"))
    return {**st, "font": family} if family else st
BOX_TRANSPARENT = "&HFF000000"  # alpha FF = fully transparent box

PLAY_RES_X = 1080
PLAY_RES_Y = 1920

FILLERS = {"um", "uh", "eh", "mm", "hmm", "erm", "ah"}


def _ass_time(seconds: float) -> str:
    """ASS uses H:MM:SS.cc (centiseconds, single-digit hour)."""
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:d}:{minutes:02d}:{secs:05.2f}"


def _escape(text: str) -> str:
    """Braces open override blocks in ASS, and a trailing backslash would escape."""
    return text.replace("\\", "∖").replace("{", "(").replace("}", ")")


# Codepoints that live in an emoji face rather than a text face. Rough on
# purpose: the cost of a false positive is one glyph drawn from the emoji font,
# which is where it would have come from anyway.
def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF
            or 0x1F000 <= o <= 0x1F2FF or o in (0x2B50, 0x2B55, 0xFE0F))


def emoji_spans(text: str) -> str:
    """Names the emoji face around emoji runs, because fallback will not find it.

    libass only consults `fontsdir` for fonts requested BY NAME. Its fallback
    search -- the path taken when the styled face lacks a glyph -- goes through
    fontconfig's system font list, which knows nothing about assets/fonts. So a
    perfectly good emoji font sitting next to the caption fonts still produced
    "failed to find any fallback with glyph 0x1F600" and drew nothing.

    Naming the face in an override block puts it on the path libass does honour.
    With no drawable emoji face installed this is a no-op and the text is left
    exactly as it was.
    """
    face = emoji_font()
    if not face or not any(_is_emoji(c) for c in text):
        return text
    out, run = [], []

    def flush_emoji():
        if run:
            out.append("{\\fn%s}%s{\\r}" % (face["family"], "".join(run)))
            run.clear()

    for ch in text:
        if _is_emoji(ch):
            run.append(ch)
        else:
            flush_emoji()
            out.append(ch)
    flush_emoji()
    return "".join(out)


def emoji_tail(mark: str, back_to: str) -> str:
    r"""An emoji appended to a caption cue, without disturbing the cue's styling.

    emoji_spans closes its override with \r, which is why it was confined to
    titles: \r resets to the style default and would wipe the karaoke colour
    overrides a caption line is carrying, turning the highlight off partway
    through. Naming the caption face explicitly on the way back out closes the
    font override and nothing else, so the same trick becomes safe on cues.

    Returns "" when no emoji face is installed, so callers need no guard.
    """
    face = emoji_font()
    if not mark or not face:
        return ""
    return " {\\fn%s}%s{\\fn%s}" % (face["family"], mark, back_to)


def _word_text(word: dict) -> str:
    return word.get("punctuated_word") or word.get("word") or ""


def group_words(words: list, max_words: int = None) -> list:
    """Chunks words into cues, breaking on length, duration, or sentence end.

    `max_words` comes from the preset: a Hormozi look is defined by showing two
    or three words at a time, and a podcast look by showing a readable sentence.
    That is the same axis, so it belongs to the preset rather than to a constant.
    """
    limit = max_words or MAX_WORDS_PER_CUE
    cues = []
    current = []

    for word in words:
        current.append(word)
        text = _word_text(word)
        span = current[-1]["end"] - current[0]["start"]

        ends_sentence = text.endswith((".", "!", "?"))
        if (len(current) >= limit or span >= MAX_CUE_SECONDS or ends_sentence
                or word.get("cue_break")):
            cues.append(current)
            current = []

    if current:
        cues.append(current)
    return cues


# ---------------------------------------------------------------------------
# Active-word effects
# ---------------------------------------------------------------------------
# Inline ASS overrides applied to the one word currently being spoken. Each
# event is emitted at the exact moment its word becomes active, so \t timings
# are relative to that instant and read as a per-word animation.
#
# GRADIENT_RAMP exists because ASS has no gradient fill. A per-word colour ramp
# across the cue is the honest approximation available in libass; it reads as a
# colour sweep on a 3-4 word cue, which is where the look is used.
GRADIENT_RAMP = ["&H00F04080", "&H00E060D0", "&H00D080F0", "&H00F0D060"]


def _scale_tags(percent: float) -> str:
    r"""\fscx/\fscy for a size, or nothing at the style's own 100%."""
    pct = int(round(percent))
    return "" if pct == 100 else f"\\fscx{pct}\\fscy{pct}"


def _active_override(st: dict, word_index: int = 0, factor: float = 1.0) -> str:
    """The override block that marks the spoken word, minus the braces.

    `factor` is the word's resting size as a fraction of the style size. Every
    scale below is absolute in ASS, so a word that rests at 58% (the top line of
    a stack) has to have its pop computed from 58, not from 100 -- otherwise
    being spoken would also blow it up to full size.
    """
    effect = st.get("active_effect", "color")
    active = st["active"]
    scale = st.get("active_scale", 100)

    def pct(value):
        return int(round(value * factor))

    if effect == "gradient":
        colour = GRADIENT_RAMP[word_index % len(GRADIENT_RAMP)]
        return f"\\c{colour}&" + _scale_tags(100 * factor)

    tags = f"\\c{active}&"

    if effect == "scale" or (effect == "color" and scale != 100):
        s = scale if scale != 100 else 115
        tags += f"\\fscx{pct(s)}\\fscy{pct(s)}"
    elif effect == "pop":
        # Overshoot then settle. Two \t segments rather than one, because a
        # single interpolation to the final size reads as a slow grow, not a pop.
        tags += (f"\\fscx{pct(88)}\\fscy{pct(88)}"
                 f"\\t(0,70,\\fscx{pct(118)}\\fscy{pct(118)})"
                 f"\\t(70,150,\\fscx{pct(104)}\\fscy{pct(104)})")
    elif effect == "bounce":
        # Drops in small, overshoots, dips under and lands: a real bounce has
        # the undershoot a pop does not.
        tags += (f"\\fscx{pct(60)}\\fscy{pct(60)}"
                 f"\\t(0,90,\\fscx{pct(118)}\\fscy{pct(118)})"
                 f"\\t(90,170,\\fscx{pct(93)}\\fscy{pct(93)})"
                 f"\\t(170,230,\\fscx{pct(100)}\\fscy{pct(100)})")
    else:
        tags += _scale_tags(100 * factor)

    if effect == "underline":
        tags += "\\u1"
    elif effect == "glow":
        # A blurred outline in the accent colour IS the glow -- libass has no
        # separate shadow-colour blur. Needs BorderStyle 1, which every glow
        # preset has (box="none"), or the blur would fill a solid plate instead.
        # Restrained on purpose. A wide blur at this canvas size reads as a
        # coloured cloud around the word rather than as light.
        tags += f"\\3c{active}&\\3a&H50&\\bord{st.get('outline', 5) + 4}\\blur4"
    elif effect == "marker":
        # Turn the transparent base box opaque for this word only.
        chip = st.get("box_color") or active
        tags += f"\\3c{chip}&\\3a&H00&"

    return tags


def _header(margin_v: int, st: dict, play_res: tuple,
            title_style: str = None, title_font: str = None,
            title_size: int = None,
            lock_margin: bool = False) -> str:
    res_x, res_y = play_res
    ts = title_look(title_style, title_font, title_size)
    pos = POSITIONS.get(st.get("position", "bottom"), POSITIONS["bottom"])
    alignment = pos["alignment"]
    if "margin_frac" in pos and not lock_margin:
        margin_v = int(res_y * pos["margin_frac"])

    box = st.get("box", "none")
    outline_width = st["outline"]
    italic = -1 if st.get("italic") else 0
    chip_style = ""
    if box == "none" or _has_chip_layer(st):
        border_style = 1
        outline_colour = st.get("outline_color") or COLOR_OUTLINE
        if _has_chip_layer(st):
            # Text keeps a real stroke; the chip moves to the layer below, where
            # `outline` is its padding and only the spoken word turns it opaque
            # -- or, for a backdrop band, every word is.
            outline_width = st["text_outline"]
            if box == "line":
                # A backdrop band is ONE box around the whole cue (BorderStyle
                # 4, drawn in BackColour). Per-glyph boxes (BorderStyle 3)
                # overlap, and a translucent band then shows darker blotches
                # wherever two of them meet. The outline is transparent: style
                # 4 would otherwise also stroke the invisible text.
                outline, back, chip_border = BOX_TRANSPARENT, st["box_color"], 4
            else:
                outline, back, chip_border = BOX_TRANSPARENT, "&HFF000000", 3
            chip_style = (
                f"\nStyle: {CHIP_STYLE},{st['font']},{st['size']},&HFF000000,"
                f"&HFF000000,{outline},{back},-1,{italic},0,0,100,100,"
                f"{st.get('spacing', 0)},{st.get('rotate', 0)},{chip_border},"
                f"{st['outline']},0,{alignment},60,60,{margin_v},1")
    else:
        # BorderStyle 3 draws OutlineColour as a filled box behind the text.
        # For a per-word chip the base box is fully transparent (alpha FF) and
        # only the active word overrides it back to opaque.
        border_style = 3
        outline_colour = st["box_color"] if box == "line" else BOX_TRANSPARENT
    if _has_bar(st):
        chip_style += (
            f"\nStyle: {BAR_STYLE},{st['font']},{st['size']},&HFF000000,"
            f"&HFF000000,{BOX_TRANSPARENT},&HFF000000,-1,{italic},0,0,100,100,"
            f"{st.get('spacing', 0)},{st.get('rotate', 0)},3,{BAR_PAD_X},0,"
            f"{alignment},60,60,{margin_v},1")

    # PrimaryColour is what \k fills TO and SecondaryColour what it fills FROM.
    # Every other effect wants both to be the resting colour; the progress fill
    # is the one look where they must differ, because that gap IS the effect.
    primary = secondary = st["base"]
    if st.get("active_effect") == "progress":
        primary, secondary = st["active"], st["base"]

    shadow = st.get("shadow", 0)
    spacing = st.get("spacing", 0)
    angle = st.get("rotate", 0)
    # BackColour is the shadow. The default is a soft translucent black; a
    # preset that names a colour gets it fully opaque, which is the difference
    # between "a bit of depth" and the hard offset of sticker/comic type.
    back_colour = st.get("shadow_color") or "&H80000000"

    # The halo layer: the same text with an invisible fill and a wide stroke in
    # the glow colour, blurred per event (a style cannot carry a blur). Drawn on
    # the layer underneath, so the real caption sits crisp on top of its light.
    glow_style = ""
    if _has_glow(st):
        glow = st["glow"].replace("&H00", "&H38", 1)       # slightly translucent
        glow_style = (
            f"\nStyle: {GLOW_STYLE},{st['font']},{st['size']},&HFF000000,"
            f"&HFF000000,{glow},&HFF000000,-1,{italic},0,0,100,100,{spacing},{angle},1,"
            f"{st['outline'] + GLOW_SPREAD},0,{alignment},60,60,{margin_v},1")

    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{st["font"]},{st["size"]},{primary},{secondary},{outline_colour},{back_colour},-1,{italic},0,0,100,100,{spacing},{angle},{border_style},{outline_width},{shadow},{alignment},60,60,{margin_v},1{glow_style}{chip_style}
Style: Title,{ts["font"]},{ts["size"]},{ts["colour"]},{ts["colour"]},{ts["edge"]},&H00000000,-1,0,0,0,100,100,0,0,{ts["border_style"]},{ts["outline"]},{ts["shadow"]},8,90,90,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# The title must not read as "another caption". It was previously 54px while
# captions run 58-84px, so it was literally the smallest text on screen -- which
# is why it neither caught the eye nor announced what the clip was about. It now
# sits clearly above every caption size, in a heavier plate, and holds longer.
TITLE_STYLE = {
    "font": "Arial Black", "size": 92,
    "colour": "&H00101010",         # near-black text
    "plate": "&H00FFFFFF",          # on a white plate
    "outline": 24,                  # plate padding, via BorderStyle 3
    "seconds": 4.5,                 # long enough to actually be read
    "top_frac": 0.07,               # distance from the top of the frame
}


# A title is a design decision, not a fixed asset. The white plate reads as
# "documentary caption" and is wrong for a gaming or meme clip, so the plate is
# now one option among several rather than the only thing on offer.
#
# BorderStyle 3 fills OutlineColour as a box behind the glyphs (the plate looks);
# BorderStyle 1 strokes it as an edge (the outline looks). `shadow` is the ASS
# drop-shadow distance in px.
TITLE_LOOKS = {
    "plate": {                       # white block, near-black text -- the default
        "colour": "&H00101010", "edge": "&H00FFFFFF",
        "border_style": 3, "outline": 24, "shadow": 0,
    },
    "ink": {                         # inverted plate: dark block, white text
        "colour": "&H00FFFFFF", "edge": "&H00161212",
        "border_style": 3, "outline": 24, "shadow": 0,
    },
    "outline": {                     # white text with a hard black stroke, no box
        "colour": "&H00FFFFFF", "edge": "&H00000000",
        "border_style": 1, "outline": 7, "shadow": 0,
    },
    "shadow": {                      # white text on a soft drop shadow
        "colour": "&H00FFFFFF", "edge": "&H00000000",
        "border_style": 1, "outline": 2, "shadow": 5,
    },
    "lime": {                        # highlighter block, the meme/gaming look
        "colour": "&H00101010", "edge": "&H004EF2D8",
        "border_style": 3, "outline": 24, "shadow": 0,
    },
    "clean": {                       # bare white text, nothing behind it
        "colour": "&H00FFFFFF", "edge": "&H00000000",
        "border_style": 1, "outline": 0, "shadow": 0,
    },
}
DEFAULT_TITLE_LOOK = "plate"


def title_look(name: str = None, font: str = None, size: int = None) -> dict:
    """Resolve a title look, with the font overridable independently.

    Look and typeface are separate choices: the same white plate reads very
    differently in Anton than in Merriweather, and forcing them to move together
    would collapse the useful combinations.
    """
    look = TITLE_LOOKS.get(name or DEFAULT_TITLE_LOOK, TITLE_LOOKS[DEFAULT_TITLE_LOOK])
    return {
        **look,
        "font": resolve_font(font) or TITLE_STYLE["font"],
        "size": size or TITLE_STYLE["size"],
    }


# The title card is a default, not a requirement. Plenty of people want the cut
# and the captions but intend to write their own hook in the platform's composer,
# or to title it in their own editor -- and burning one in is irreversible.
TITLE_LOOK_OFF = "none"


def _title_events(title: str, play_res: tuple, title_style: str = None) -> str:
    """A title card pinned near the top for the opening seconds.

    Reference clips from finished tools nearly always open with one: it gives
    the viewer the premise before the speaker gets there, which is most of why
    they feel edited rather than merely cut.
    """
    if not title or title_style == TITLE_LOOK_OFF:
        return ""
    res_y = play_res[1]
    margin = int(res_y * TITLE_STYLE["top_frac"])
    end = _ass_time(TITLE_STYLE["seconds"])
    # The title is one event with no per-word overrides, so naming the emoji
    # face inside it cannot disturb anything else. (Caption events carry
    # karaoke colour overrides that an \r would reset, so they are left alone
    # until that interaction is worked out.)
    text = emoji_spans(_escape(title.strip()))
    # \an8 = top-centre, independent of the caption style's own alignment.
    return (f"Dialogue: 0,0:00:00.00,{end},Title,,0,0,{margin},,"
            f"{{\\an8}}{text}\n")


# Spoken numbers that carry a stat. Single-digit words are left out on purpose:
# "one of the", "no one", "two of us" are filler far more often than figures.
NUMBER_WORDS = {
    "ten", "eleven", "twelve", "fifteen", "twenty", "thirty", "forty", "fifty",
    "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million",
    "billion", "trillion", "percent", "dozen", "half", "double", "triple",
}
KEYWORD_EMPHASIS_SCALE = 112
# A number-shaped token: 20, 2008, 3.5, 1,000, $5, 40%, 10k, 1st, 90s.
_NUMBER = _re.compile(r"^[$\u20b9\u20ac\u00a3]?\d[\d,.:]*(%|k|m|b|st|nd|rd|th|s)?$")


def _key_token(text: str) -> str:
    """A word as keyword matching sees it: lower case, no punctuation, no 's."""
    t = (text or "").lower().strip(".,!?;:\"'()[]\u0964\u2019\u201c\u201d")
    for suffix in ("'s", "\u2019s"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t


def keyword_hits(words: list, keywords: list = None) -> set:
    """Indices into `words` that the clip is ABOUT.

    Keywords from the analyzer are often phrases ("what is a woman"), and the
    old check compared each caption word against the whole phrase, so a phrase
    keyword never matched anything. Phrases are matched as runs of words here,
    plural and possessive forms count ("women's" for "women"), and numbers --
    the stats a clip usually turns on -- are always emphasised, at no cost.
    """
    hits = set()
    for a, b in keyword_spans(words, keywords):
        hits.update(range(a, b))
    return hits


def keyword_spans(words: list, keywords: list = None) -> list:
    """[(first, end)) word-index spans, one per keyword said, in order.

    A span is one whole phrase, so two keywords said back to back stay two
    spans -- the captions-off pop-ups show them as two moments, not one run.
    """
    tokens = [_key_token(_word_text(w)) for w in words]
    phrases = []
    for keyword in keywords or []:
        parts = [_key_token(p) for p in str(keyword).split()]
        parts = [p for p in parts if p]
        if parts:
            phrases.append(parts)
    spans = []
    for i in range(len(tokens)):
        for phrase in phrases:
            n = len(phrase)
            window = tokens[i:i + n]
            if len(window) == n and all(
                    a == b or (len(b) > 3 and a.rstrip("s") == b.rstrip("s"))
                    for a, b in zip(window, phrase)):
                spans.append((i, i + n))
        if _NUMBER.match(tokens[i]) or tokens[i] in NUMBER_WORDS:
            spans.append((i, i + 1))
    spans.sort()
    merged = []
    for a, b in spans:                       # a number inside a phrase, say
        if merged and a < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


# Captions off still shows what the clip is about: its keyword phrases pop up
# on their own (with emoji), the way editors put up a key word over a talking
# head rather than subtitling every sentence.
POPUP_HOLD = 1.2          # s a pop-up stays up, so a fast phrase is still read
POPUP_REPEAT_GAP = 10.0   # the same phrase again this soon is not shown again
POPUP_MAX_WORDS = 3       # longer phrases are shown in even chunks


def keyword_popups(words: list, keywords: list = None) -> list:
    """The words to caption when captions are off: keyword phrases only.

    Returns copies of the chosen words; each pop-up's last word carries
    `cue_break` (so every grouping of cues -- captions, emoji, overlays -- ends
    the cue there) and an `end` held for POPUP_HOLD, never into the next one.
    """
    chunks, last_seen = [], {}
    for a, b in keyword_spans(words, keywords):
        key = " ".join(_key_token(_word_text(w)) for w in words[a:b])
        start = words[a]["start"]
        if start - last_seen.get(key, -1e9) < POPUP_REPEAT_GAP:
            continue
        last_seen[key] = start
        n = b - a
        parts = -(-n // POPUP_MAX_WORDS)
        bounds = [a + round(k * n / parts) for k in range(parts + 1)]
        chunks.extend(words[x:y] for x, y in zip(bounds, bounds[1:]))
    out = []
    for k, chunk in enumerate(chunks):
        copies = [dict(w) for w in chunk]
        last = copies[-1]
        end = max(last["end"], copies[0]["start"] + POPUP_HOLD)
        if k + 1 < len(chunks):
            end = min(end, chunks[k + 1][0]["start"] - 0.05)
        last["end"] = max(last["end"], end)
        last["cue_break"] = True
        out.extend(copies)
    return out


def keyword_slots(words: list, hits: set) -> dict:
    """{word index: colour slot} for the keyword hits of a clip.

    Each distinct keyword gets the next slot of the preset's palette the first
    time it is said and keeps it after that, so "money" is the same colour
    every time it comes back while the next idea gets a different one. A
    phrase keyword is one run of hits and shares one slot.
    """
    slots, seen = {}, {}
    run = []

    def close():
        if not run:
            return
        key = " ".join(_key_token(_word_text(words[i])) for i in run)
        slot = seen.setdefault(key, len(seen))
        for i in run:
            slots[i] = slot

    for i in sorted(hits):
        if run and i != run[-1] + 1:
            close()
            run = []
        run.append(i)
    close()
    return slots


def _keyworded(rendered: list, cue: list, key_hits: dict, st: dict) -> str:
    """Cue text with the clip's keywords coloured, nothing else marked."""
    parts = []
    for j, token in enumerate(rendered):
        if j in key_hits:
            override = _keyword_override(st, 1.0, _slot(key_hits, j))
            parts.append(f"{{{override}}}{token}{{\\r}}")
        else:
            parts.append(token)
    return " ".join(parts)


def _slot(key_hits, j: int) -> int:
    """A hit's palette slot; a plain set of hits (older callers) is all slot 0."""
    return key_hits.get(j, 0) if isinstance(key_hits, dict) else 0


def _keyword_override(st: dict, factor: float = 1.0, slot: int = 0) -> str:
    """Colour, and optionally size, for a word the clip is ABOUT.

    A keyword has to read as different from both the resting text and the word
    being spoken. Where the preset's keyword colour is one of those two (many
    looks use one accent for both, and a recoloured base takes the keyword
    colour with it), size carries the emphasis instead. Distinct keywords take
    turns through the preset's palette.
    """
    palette = st.get("keyword_palette") or [st.get("keyword", st["active"])]
    colour = palette[slot % len(palette)]
    scale = st.get("keyword_scale", 100)
    if scale == 100 and colour in (st["active"], st["base"]):
        scale = KEYWORD_EMPHASIS_SCALE
    return f"\\c{colour}&" + _scale_tags(scale * factor)


def _dialogue(start: float, end: float, text: str, st: dict,
              under: str = "") -> list:
    """The Dialogue line(s) for one caption event.

    Almost always one. A glow preset adds a second copy on the layer below,
    carrying the halo: same text and the same overrides, so it tracks every pop
    and reveal of the caption above it exactly. A backdrop band and a headline
    bar are drawn from `under`, a still copy of the cue: a box drawn around a
    word mid-pop would jump wider with it.
    """
    span = f"{_ass_time(start)},{_ass_time(end)}"
    if _has_bar(st) and under:
        return [f"Dialogue: 0,{span},{BAR_STYLE},,0,0,0,,{under}",
                f"Dialogue: 1,{span},Caption,,0,0,0,,{text}"]
    if _has_chip_layer(st):
        plate = text
        if st.get("box") == "line":
            plate = under or text
            if st.get("pad_x"):
                # Wider than tall: a band, not a box hugging the words. A
                # reset drops the padding, so it is restated after each one.
                pad = f"\\xbord{st['pad_x']}\\ybord{st['outline']}"
                plate = "{" + pad + "}" + plate.replace("{\\r", "{\\r" + pad)
        # For a word chip, identical text on both layers: the marker override
        # that makes the chip opaque for the spoken word also tints that word's
        # stroke on the layer above, so the two always agree about which word
        # is lit.
        return [f"Dialogue: 0,{span},{CHIP_STYLE},,0,0,0,,{plate}",
                f"Dialogue: 1,{span},Caption,,0,0,0,,{text}"]
    if not _has_glow(st):
        return [f"Dialogue: 0,{span},Caption,,0,0,0,,{text}"]
    blur = f"\\blur{GLOW_BLUR}"
    # A reset wipes the blur along with everything else, so it is restated after
    # every one -- otherwise the halo turns into a hard coloured outline mid-line.
    halo = "{" + blur + "}" + text.replace("{\\r", "{\\r" + blur)
    return [f"Dialogue: 0,{span},{GLOW_STYLE},,0,0,0,,{halo}",
            f"Dialogue: 1,{span},Caption,,0,0,0,,{text}"]


def _compose(rendered: list, cue: list, key_hits: set, st: dict,
             active_index: int) -> str:
    """One cue's text at the instant word `active_index` is being spoken.

    Handles the three things a word can be at that instant -- spoken, a keyword,
    or not yet said -- and the stacked layout, where the lead-in sits small above
    a final word drawn at full size.

    Every styled token is closed with a reset PLUS the line's resting scale, not
    a bare reset: the space that follows belongs to the resting size, and a bare
    reset on a 58% line leaves full-width gaps between small words.
    """
    effect = st.get("active_effect", "color")
    count = len(rendered)
    stacked = st.get("mode") == "stack" and count > 1
    small = st.get("stack_small", 58) / 100.0
    reveal = bool(st.get("reveal"))

    def line(lo, hi, factor, emphasised=False):
        rest = _scale_tags(100 * factor)
        reset = "{\\r" + rest + "}"
        parts = []
        for j in range(lo, hi):
            token = rendered[j]
            keyword = j in key_hits
            if reveal and j > active_index:
                tags = rest + "\\alpha&HFF&"
            elif count == 1 and effect == "color":
                # One word per cue means every word is "the active word". Painting
                # it in the highlight colour turns a white-on-black caption into a
                # solid-colour caption -- a different style, not a faster cadence.
                # Keep the resting colour unless the word is a keyword.
                tags = (_keyword_override(st, factor, _slot(key_hits, j))
                        if keyword else rest)
            elif j == active_index:
                # Inline overrides need BOTH the &H prefix and a trailing &.
                # Without the trailing &, libass mis-parses the tag and leaks
                # stray punctuation into the rendered line.
                tags = _active_override(st, j, factor)
                if keyword and st["active"] == st["base"]:
                    # A look whose highlight is pure motion: a keyword keeps
                    # its colour while it pops, rather than going plain.
                    palette = st.get("keyword_palette") or [st.get("keyword", st["active"])]
                    colour = palette[_slot(key_hits, j) % len(palette)]
                    tags = tags.replace(f"\\c{st['active']}&", f"\\c{colour}&", 1)
            elif effect == "gradient":
                # The ramp has to run across the WHOLE cue, not just the word
                # being spoken -- colouring one word from a four-colour ramp
                # and leaving the rest white reads as alternating text. libass
                # cannot interpolate colour within a glyph, so per-word steps
                # are the ramp.
                tags = f"\\c{GRADIENT_RAMP[j % len(GRADIENT_RAMP)]}&" + rest
            elif keyword:
                tags = _keyword_override(st, factor, _slot(key_hits, j))
            elif emphasised:
                # The big word of a stack stays in the accent colour once it has
                # landed -- it is the point of the line, not a passing highlight.
                tags = f"\\c{st['active']}&"
            else:
                tags = rest
            parts.append(f"{{{tags}}}{token}{reset}" if tags else token)
        return " ".join(parts)

    if stacked:
        return (line(0, count - 1, small) + "\\N"
                + line(count - 1, count, 1.0, emphasised=True))
    if st.get("mode") == "headline":
        # The opposite of a stack: the first word huge, the rest small below,
        # still in reading order. No automatic wrapping, so the bar layer and
        # the text always break in the same place.
        k = _headline_split([_plain_token(t) for t in rendered])
        if k >= count:
            return "{\\q2}" + line(0, count, 1.0)
        return "{\\q2}" + line(0, k, 1.0) + "\\N" + line(k, count, small)
    return line(0, count, 1.0)


def _fit_canvas(st: dict, play_res: tuple) -> dict:
    """A preset designed on the 1080x1920 portrait canvas, sized for this one.

    Every size is absolute pixels on the output canvas, so on a 1920x1080 frame
    a caption designed at 134px filled an eighth of the picture's height.
    Landscape and square canvases scale every pixel measure by the height
    ratio, softened -- a caption is still read on a small screen.
    """
    res_x, res_y = play_res
    if res_x <= res_y * 0.7:                      # portrait: as designed
        return st
    k = min(1.0, (res_y / PLAY_RES_Y) ** 0.45)
    out = dict(st)
    for key in ("size", "outline", "shadow", "text_outline", "pad_x"):
        if out.get(key):
            out[key] = max(1, int(round(out[key] * k)))
    if out.get("spacing"):
        out["spacing"] = round(out["spacing"] * k, 1)
    out["size"] = max(MIN_CAPTION_PX, out["size"])
    return out


def _plain_token(token: str) -> str:
    """A rendered token without its override blocks (an emoji tail, say)."""
    return _re.sub(r"\{[^}]*\}", "", token)


def _phrase_event(rendered: list, cue: list, key_hits: dict, st: dict,
                  start: float, end: float, entrance: str, cue_ms: int,
                  play_res: tuple, margin_v: int, under: str = "") -> str:
    """One Dialogue for a whole chunk -- the Hormozi/MrBeast cadence.

    A phrase preset is defined by replacement, not accumulation: two or three
    words land together, hold, and are gone. Emitting the usual per-word events
    here would put a travelling highlight on text the viewer finished reading
    before the second word was spoken.
    """
    text = _keyworded(rendered, cue, key_hits, st)
    if entrance and entrance != "none":
        tags = animation_tags(entrance, cue_ms, play_res, margin_v)
        if tags:
            text = "{" + tags + "}" + text
    return "\n".join(_dialogue(start, end, text, st, under))


# A character reveal is one Dialogue per step, so an unbounded step count would
# put thousands of events in a file for no visible gain. 45ms is around the
# fastest reveal that still reads as typing rather than as a cut.
TYPEWRITER_STEP_SECONDS = 0.045
MAX_TYPEWRITER_STEPS = 64


def _typewriter_events(rendered: list, st: dict,
                       start: float, end: float) -> list:
    """Characters appearing one at a time across the cue's own span.

    Timed across the cue rather than per word: the reveal has to finish by the
    time the chunk is replaced, and pinning each character to a word's timing
    would make the rate lurch with the speaker's pace.
    """
    full = " ".join(rendered)
    span = end - start
    if span <= 0 or not full:
        return []

    steps = min(len(full), MAX_TYPEWRITER_STEPS,
                max(1, int(span / TYPEWRITER_STEP_SECONDS)))
    events = []
    for k in range(1, steps + 1):
        cut = max(1, round(len(full) * k / steps))
        t0 = start + span * (k - 1) / steps
        # The final step holds until the cue ends; the rest last one step each.
        t1 = end if k == steps else start + span * k / steps
        if t1 <= t0:
            continue
        events.extend(_dialogue(t0, t1, full[:cut], st))
    return events


def _progress_event(rendered: list, cue: list, st: dict, clip_start_time: float,
                    start: float, end: float, entrance: str, cue_ms: int,
                    play_res: tuple, margin_v: int) -> str:
    r"""One Dialogue using \kf, libass's native left-to-right fill.

    \kf durations are centiseconds and are consumed in sequence from the line's
    own start, so they must be built from the words' real gaps -- a word that
    starts late needs the preceding one's fill to stretch, or the sweep drifts
    ahead of the voice within a couple of words.
    """
    parts = []
    for j, token in enumerate(rendered):
        w_start = cue[j]["start"] - clip_start_time
        w_end = (cue[j + 1]["start"] - clip_start_time
                 if j + 1 < len(cue) else end)
        cs = max(1, int(round((w_end - w_start) * 100)))
        parts.append(f"{{\\kf{cs}}}{token}")
    text = " ".join(parts)
    if entrance and entrance != "none":
        tags = animation_tags(entrance, cue_ms, play_res, margin_v)
        if tags:
            text = "{" + tags + "}" + text
    return "\n".join(_dialogue(start, end, text, st))


def _recolour(st: dict, color: str = None, active_color: str = None) -> dict:
    """A preset with its base and/or highlight colour replaced.

    Colours arrive from the editor as CSS hex. Every preset used to ship white
    text, so "pick a caption style" only ever changed how the spoken word was
    marked -- this is the axis people reach for first.
    """
    patch = {}
    if color:
        patch["base"] = caption_presets.css_to_ass(color)
        # `keyword` defaults to orange on top of white; on a recoloured base it
        # reads as a second, unasked-for highlight.
        patch["keyword"] = patch["base"]
    if active_color:
        patch["active"] = caption_presets.css_to_ass(active_color)
    return {**st, **patch} if patch else st


def _ass_seconds(value: str) -> float:
    """H:MM:SS.cc from a Dialogue line -> seconds."""
    try:
        h, m, rest = value.split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    except (ValueError, AttributeError):
        return -1.0


def _place_captions(lines: list, positions: list, play_res: tuple,
                    st: dict) -> list:
    """Inject a per-scene \\pos into every caption event.

    The style's MarginV sets one baseline for the whole file. A clip whose
    layout changes -- single to split and back on every speaker change -- needs
    the text to move with it, and ASS has no per-event margin. \\pos is the
    per-event placement the format does have.

    Only bottom-anchored styles are handled: for those, y is the bottom of the
    block, which is the same coordinate margin_v uses. A middle-anchored style
    would need a different origin, so it keeps its preset placement.
    """
    if not positions:
        return lines
    alignment = POSITIONS.get(st.get("position", "bottom"),
                              POSITIONS["bottom"]).get("alignment")
    if alignment != 2:
        return lines
    res_x, res_y = play_res
    spans = sorted(positions, key=lambda p: p["start"])

    out = []
    for line in lines:
        # Dialogue: layer,start,end,style,name,marginL,marginR,marginV,effect,text
        parts = line.split(",", 9)
        if len(parts) < 10 or parts[3] not in CAPTION_LAYER_STYLES:
            out.append(line)
            continue
        seconds = _ass_seconds(parts[1])
        chosen = None
        for span in spans:
            if span["start"] <= seconds < span["end"]:
                chosen = span
                break
        if chosen is None and spans and seconds >= spans[-1]["end"]:
            chosen = spans[-1]
        if chosen is None:
            out.append(line)
            continue
        y = int(round(res_y * float(chosen["pos"])))
        head = ",".join(parts[:9])
        out.append(f"{head},{{\\pos({res_x // 2},{y})}}{parts[9]}")
    return out


def build_ass(words: list, clip_start_time: float, out_path: str,
              margin_v: int = 150, style: str = "classic",
              play_res: tuple = (PLAY_RES_X, PLAY_RES_Y),
              title: str = "", keywords: list = None,
              font: str = None, size_px: int = None,
              animation: str = None,
              color: str = None, active_color: str = None,
              title_style: str = None, title_font: str = None,
              title_size: int = None,
              cue_emoji: dict = None,
              max_words: int = None,
              positions: list = None,
              lock_margin: bool = False) -> str:
    """Writes an ASS file whose timings are relative to the clip's own start.

    words:           Deepgram word objects with absolute 'start'/'end' seconds
    clip_start_time: where this clip begins in the source video
    margin_v:        distance in real pixels from the bottom of the 1920px frame
                     to the bottom of the caption block
    style:           key into STYLES; unknown names fall back to classic
    """
    st = _preset_style(style)
    family = resolve_font(font)
    if family:
        st = {**st, "font": family, "font_id": font}
    # Absolute px against the 1080x1920 reference canvas. None means "whatever
    # this caption style was designed at", which is what most people want.
    if size_px:
        st = {**st, "size": max(MIN_CAPTION_PX, min(MAX_CAPTION_PX, int(size_px)))}
    # A recoloured base takes the keyword colour with it (see _recolour), so it
    # keeps one keyword colour; otherwise keywords rotate through the palette.
    st = {**_recolour(st, color, active_color),
          "keyword_palette": (None if color
                              else caption_presets.keyword_palette(st))}
    st = _fit_canvas(st, play_res)
    # The editor's "1 / 2 / 3 words" control overrides the preset's cadence for
    # this clip only. It has to be applied before anything groups cues -- the
    # emoji planner uses the same number, or the marks land on the wrong lines.
    if max_words:
        st = {**st, "max_words": max(1, min(8, int(max_words)))}

    # Romanize Devanagari words so the chosen caption font is preserved.
    # _fit_devanagari decides whether romanization or a font swap is needed.
    caption_text = "".join(_word_text(w) for w in words)
    st, title_font = _fit_devanagari(st, caption_text, title, title_font)
    if _is_devanagari(caption_text):
        words = _romanize_words(words)
    if _is_devanagari(title or ""):
        title = _romanize(title)

    lines = [_header(margin_v, st, play_res, title_style, title_font,
                     title_size=title_size, lock_margin=lock_margin)]
    lines.append(_title_events(title, play_res, title_style))

    # Keywords stay coloured for the whole cue, independent of the karaoke
    # highlight. Reference clips from finished tools use this to make the point
    # of a line readable at a glance, even when paused mid-scroll.
    # Matched across the whole clip, then handed to each cue as local indices,
    # so a keyword phrase is found even when cue chunking splits it.
    global_hits = keyword_hits(words, keywords)
    slots = keyword_slots(words, global_hits)
    position = {id(w): i for i, w in enumerate(words)}

    cues = list(group_words(words, st.get("max_words")))
    mode = st.get("mode", "karaoke")
    effect = st.get("active_effect", "color")

    for ci, cue in enumerate(cues):
        rendered = [_escape(_word_text(w)) for w in cue]
        key_hits = {j: slots[position[id(w)]] for j, w in enumerate(cue)
                    if position.get(id(w)) in global_hits}
        if st["uppercase"]:
            rendered = [t.upper() for t in rendered]

        # AFTER uppercasing, which would otherwise turn \fn into \FN and the
        # family name with it, leaving a tag libass does not recognise. Attached
        # to the cue's last word rather than to the finished line so it travels
        # through karaoke, phrase and typewriter output alike.
        mark = (cue_emoji or {}).get(ci)
        if mark and rendered:
            rendered[-1] += emoji_tail(mark, st["font"])

        # Where the following cue takes over. The last word of this cue must not
        # outlive it, or two different lines are on screen at once.
        next_cue_start = (cues[ci + 1][0]["start"] - clip_start_time
                          if ci + 1 < len(cues) else None)

        cue_start = cue[0]["start"] - clip_start_time
        cue_end = cue[-1]["end"] - clip_start_time
        if next_cue_start is not None:
            cue_end = min(cue_end, next_cue_start)
        cue_ms = int(max(0.0, cue_end - cue_start) * 1000)
        entrance = animation or st.get("entrance")

        # The still layer under the text: a headline's bar, or a backdrop band.
        under = ""
        if _has_bar(st):
            under = _bar_text([_word_text(w) for w in cue], rendered, st,
                              _headline_split([_plain_token(t) for t in rendered]))
        elif st.get("box") == "line" and _has_chip_layer(st):
            under = _keyworded(rendered, cue, key_hits, st)

        # -- phrase mode ---------------------------------------------------
        # The whole chunk appears at once and is replaced by the next one. No
        # per-word marking: with two or three words on screen the eye has already
        # read all of them before the second is spoken, so a travelling highlight
        # adds motion without adding information.
        if mode == "phrase":
            lines.append(_phrase_event(rendered, cue, key_hits, st, cue_start,
                                       cue_end, entrance, cue_ms, play_res,
                                       margin_v, under))
            continue

        # -- typewriter mode -----------------------------------------------
        if mode == "typewriter":
            lines.extend(_typewriter_events(rendered, st, cue_start, cue_end))
            continue

        # -- progress fill --------------------------------------------------
        # \kf is libass's own left-to-right fill and needs one Dialogue per cue
        # with per-word durations, not one per word. It is the only effect that
        # cannot be expressed in the per-word event model below.
        if effect == "progress":
            lines.append(_progress_event(rendered, cue, st, clip_start_time,
                                         cue_start, cue_end, entrance, cue_ms,
                                         play_res, margin_v))
            continue

        for i, word in enumerate(cue):
            start = word["start"] - clip_start_time
            # Hold the highlight until the next word actually begins, so there is
            # never a gap where nothing is lit.
            if i + 1 < len(cue):
                end = cue[i + 1]["start"] - clip_start_time
            else:
                end = word["end"] - clip_start_time
            end = max(end, start + MIN_WORD_SECONDS)

            # The floor above is a legibility minimum, but it must never push an
            # event past the one that follows it. When speech runs faster than
            # MIN_WORD_SECONDS -- constant in fast Hindi -- an unclamped floor
            # left two Dialogue lines overlapping, and libass stacks overlapping
            # lines VERTICALLY: the caption block visibly jumped between two
            # heights and, mid-cue, drew the same text twice with two different
            # words highlighted. That is the flicker. A briefly-short highlight
            # is the correct trade against a doubled, jumping caption.
            hard_limit = (cue[i + 1]["start"] - clip_start_time
                          if i + 1 < len(cue) else next_cue_start)
            if hard_limit is not None:
                end = min(end, hard_limit)

            if end <= 0 or end <= start:
                continue

            text = _compose(rendered, cue, key_hits, st, i)

            # Animate only the FIRST word event of a cue. Re-triggering the
            # entrance on every word would make the line jitter continuously
            # instead of arriving once.
            # A one-word cue is often under 150ms. The entrance animation is
            # clamped to a 60ms minimum, which at 25fps is one or two frames --
            # a flicker, not an arrival. Hard-cut those; the eye reads a new
            # word appearing as a clean change.
            if entrance and entrance != "none" and i == 0 and len(cue) > 1:
                tags = animation_tags(entrance, cue_ms, play_res, margin_v)
                if tags:
                    text = "{" + tags + "}" + text

            lines.extend(_dialogue(start, end, text, st, under))

    lines = _place_captions(lines, positions, play_res, st)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return out_path


def first_meaningful_word_time(words: list, limit: int = 4) -> float:
    """Start time of the first non-filler word, used to trim weak clip openings."""
    for word in words[:limit]:
        token = _word_text(word).strip().lower().strip(".,!?")
        if token and token not in FILLERS:
            return word["start"]
    return words[0]["start"] if words else 0.0


def build_ass_lines(caption_lines: list, out_path: str,
                    margin_v: int = 150, style: str = "classic",
                    play_res: tuple = (PLAY_RES_X, PLAY_RES_Y),
                    title: str = "", font: str = None, size_px: int = None,
                    animation: str = None,
                    color: str = None, active_color: str = None,
                    title_style: str = None, title_font: str = None,
                    title_size: int = None,
                    lock_margin: bool = False) -> str:
    """Whole-line captions with no karaoke, for translated subtitles.

    A translation has different words with different lengths from the speech,
    so per-word highlighting would be lying about timing. Whole lines timed to
    the original utterance spans stay honest: the line appears exactly while
    that sentence is being said. Times here are already clip-relative.
    """
    st = _preset_style(style)
    family = resolve_font(font)
    if family:
        st = {**st, "font": family}
    if size_px:
        st = {**st, "size": max(MIN_CAPTION_PX, min(MAX_CAPTION_PX, int(size_px)))}
    st = _recolour(st, color, active_color)
    caption_text = " ".join((l.get("text") or "") for l in caption_lines)
    st, title_font = _fit_devanagari(st, caption_text, title, title_font)
    if _is_devanagari(caption_text):
        caption_lines = [
            {**l, "text": _romanize(l.get("text") or "")} if _is_devanagari(l.get("text") or "") else l
            for l in caption_lines
        ]
    if _is_devanagari(title or ""):
        title = _romanize(title)

    out = [_header(margin_v, st, play_res, title_style, title_font,
                   title_size=title_size, lock_margin=lock_margin),
           _title_events(title, play_res, title_style)]
    for line in caption_lines:
        text = _escape((line.get("text") or "").strip())
        if not text:
            continue
        if st["uppercase"]:
            text = text.upper()
        start, end = float(line["start"]), float(line["end"])
        if end <= start:
            continue
        colour = f"\\c{st['active']}" if st.get("box") == "none" else ""
        # One cue per line here (no karaoke), so the entrance animation plays
        # once per sentence rather than once per word -- the same tags, applied
        # at the only granularity a translated line has.
        entrance = animation or st.get("entrance")
        anim = ""
        if entrance and entrance != "none":
            anim = animation_tags(entrance, int((end - start) * 1000),
                                  play_res, margin_v)
        # animation_tags returns BARE tags; unbraced they render as literal
        # text, so colour and animation share one override block.
        override = f"{{{anim}{colour}}}" if (anim or colour) else ""
        out.extend(_dialogue(start, end, f"{override}{text}", st))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return out_path
