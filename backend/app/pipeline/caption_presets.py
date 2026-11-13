"""The caption preset registry -- one definition per look, used by both sides.

Why this is its own module
--------------------------
The renderer and the editor have to agree about what a caption looks like, and
until now they agreed by duplication: `subtitles.STYLES` in Python and a hand-kept
`STYLES` object in LiveEditor.jsx. Every new look meant editing both, and the two
drifted (the editor previewed sizes and colours the export did not use).

So the registry lives here, and `web_presets()` renders it into exactly the shape
the browser needs -- CSS colours, not ASS colours. Adding a preset is one entry in
one file and it appears, correctly previewed, in the picker.

The axes
--------
A preset is not just a colour scheme. Six independent axes describe every look
short-form editors actually ship:

  mode           how text is chunked and revealed
                   karaoke    -- whole cue visible, active word marked
                   phrase     -- 2-3 words at a time, replaced wholesale
                                 (the Hormozi/MrBeast cadence)
                   typewriter -- characters appear one at a time
                   stack      -- two lines: the lead-in small on top, the last
                                 word huge underneath in the accent colour
  active_effect  how the spoken word is marked (see ACTIVE_EFFECTS)
  entrance       how a cue arrives (folded in from the old separate control)
  position       where the block sits
  box            none / word chip / full plate
  case           casing

Colour format: ASS is &HAABBGGRR -- byte order reversed from CSS, alpha first
and inverted (00 = opaque). `ass_to_css` is the only place that has to know.
"""

# ---------------------------------------------------------------------------
# Active-word effects
# ---------------------------------------------------------------------------
# What "the word being spoken" looks like. This used to be a single hardcoded
# behaviour -- recolour, optionally scale -- which is why every preset felt like
# a palette swap of the same caption.
#
# `web` is the hint the editor uses to draw the same thing in DOM. The renderer
# is still the authority; the editor is an approximation whose job is to make the
# choice obvious before you spend a render on it.
ACTIVE_EFFECTS = {
    "color":    {"name": "Colour",   "desc": "Spoken word changes colour", "web": "color"},
    "scale":    {"name": "Scale",    "desc": "Spoken word grows to ~115%", "web": "scale"},
    "pop":      {"name": "Pop",      "desc": "Word springs up and settles", "web": "pop"},
    "underline": {"name": "Underline", "desc": "Line tracks under the word", "web": "underline"},
    "glow":     {"name": "Glow",     "desc": "Coloured halo on the word",   "web": "glow"},
    "marker":   {"name": "Marker",   "desc": "Highlighter swipe behind it", "web": "marker"},
    "progress": {"name": "Fill",     "desc": "Word fills left to right",    "web": "progress"},
}

# Effects that need the base style to carry a box, because they paint one.
BOX_EFFECTS = {"marker"}

# ---------------------------------------------------------------------------
# Entrances
# ---------------------------------------------------------------------------
# Folded into the preset rather than offered as a separate control. A caption
# look is a whole design: "Hormozi Punch" means something specific about both
# colour and arrival, and asking people to rebuild that from two orthogonal
# menus produced mostly bad combinations.
ENTRANCES = ("none", "fade", "pop", "riseup", "punch", "bounce")

CATEGORIES = [
    # The designed looks. Every family below this one varies a single axis of the
    # same white Arial caption; these each pick a typeface, a depth treatment and
    # a cadence together, which is what makes a caption read as a *style*.
    {"id": "signature", "name": "Signature", "desc": "Designed looks: type, depth and motion together"},
    # First because it is the default family: the four house looks below are
    # complete recipes, not palette swaps, and they are what most people should
    # start from.
    {"id": "pro",       "name": "Pro",       "desc": "House looks: glow, pop, chip, soft"},
    {"id": "karaoke",   "name": "Karaoke",   "desc": "Word-by-word, follows the voice"},
    {"id": "hormozi",   "name": "Hormozi",   "desc": "Big caps, a few words at a time"},
    {"id": "hype",      "name": "Hype",      "desc": "Loud, fast, for gaming and reactions"},
    {"id": "podcast",   "name": "Podcast",   "desc": "Quiet and readable, no shouting"},
    {"id": "social",    "name": "Social",    "desc": "Looks native to TikTok and Reels"},
    {"id": "cinematic", "name": "Cinematic", "desc": "Film subtitles, wide and calm"},
    {"id": "neon",      "name": "Neon",      "desc": "Gradient and glow, for gaming"},
    {"id": "story",     "name": "Story",     "desc": "Typewriter reveals for narration"},
    # Added because every family above starts from white text and only differs
    # in how the spoken word is marked -- so the picker read as one caption in
    # nine palettes. These change the BASE colour, which is the first thing
    # anyone actually notices.
    {"id": "vibrant",   "name": "Vibrant",   "desc": "Coloured text, not just white"},
    {"id": "clean",     "name": "Clean",     "desc": "Plates and soft type, easy to read"},
]

# Shared palette, written once in ASS order.
W = "&H00FFFFFF"        # white
INK = "&H00101010"      # near-black
LIME = "&H004EF2D8"     # lime / spring green
YELLOW = "&H0000D9FF"   # a strong yellow
GREEN = "&H003AD43A"
RED = "&H004040F0"
CYAN = "&H00F0D060"
ORANGE = "&H000090FF"
MAGENTA = "&H00E060D0"
CREAM = "&H00E8F0F5"
BLUE = "&H00F66C2B"
PURPLE = "&H00F04080"
BLACK = "&H00000000"
HOT_PINK = "&H009314FF"
ELECTRIC = "&H00FFE100"  # electric cyan
VIOLET = "&H00F65C8B"

# Defaults every preset inherits, so an entry only states what is unusual about
# it. Without this each preset was 12 near-identical lines and the differences
# that actually mattered were invisible in the diff.
DEFAULTS = {
    "font": "Arial Black",
    "size": 76,
    "base": W,
    "active": LIME,
    "outline": 5,
    "shadow": 0,
    "uppercase": False,
    "active_scale": 100,
    "position": "bottom",
    "box": "none",
    "box_color": None,
    "keyword": ORANGE,
    "mode": "karaoke",
    "active_effect": "color",
    "entrance": "none",
    "max_words": 4,
    "rotate": 0,
    "spacing": 0,
    "category": "karaoke",
    "legacy": False,
    # --- depth and emphasis -------------------------------------------------
    # All optional, all off by default, so every preset written before these
    # existed renders exactly as it did.
    # Slug of a shipped face in assets/fonts (see subtitles.FONTS). Preferred
    # over `font`, which is a raw family name the editor cannot preview.
    "font_id": None,
    "outline_color": None,   # stroke colour; None = black
    # A hard, opaque offset shadow is what gives comic/sticker type its depth.
    # None keeps the soft translucent black every older preset shipped with.
    "shadow_color": None,
    "keyword_scale": 100,    # keywords drawn larger than the words around them
    "glow": None,            # colour of a blurred halo under the WHOLE caption
    # Words stay hidden until they are spoken. The layout is computed from the
    # full cue, so nothing reflows as the line fills in.
    "reveal": False,
    "stack_small": 58,       # stack mode: size of the top line, % of the bottom
    # With box="word": stroke the text and draw the chip on a layer underneath,
    # so words that are NOT being spoken stay readable over a bright frame.
    "text_outline": None,
}


def _p(id, name, category, **kw):
    return {**DEFAULTS, "id": id, "name": name, "category": category, **kw}


# ---------------------------------------------------------------------------
# The presets
# ---------------------------------------------------------------------------
# Order within a category is the order they appear in the picker.
_LIST = [
    # -- the five originals ------------------------------------------------
    # Kept under their original ids, because saved clips reference them by name
    # and a clip must keep re-exporting the way it was made. They are marked
    # legacy only so the picker can sort them after the designed families.
    _p("classic", "Classic", "karaoke", active=LIME, entrance="fade", legacy=True),
    _p("fitbox", "Fitbox", "karaoke", size=66, active=W, outline=14,
       box="word", box_color=BLUE, active_effect="marker", legacy=True),
    _p("tech", "Tech", "karaoke", size=80, active="&H00FFE9BF", outline=4,
       position="middle", keyword="&H00FFC34F", legacy=True),
    _p("business", "Business", "podcast", size=58, base="&H00807A72",
       active="&H001E1A1A", outline=18, position="lower", box="line",
       box_color="&H00F5F3EF", keyword="&H002020C8", legacy=True),
    _p("gameplay", "Gameplay", "hype", size=84, active=GREEN, outline=6,
       uppercase=True, active_scale=118, keyword="&H0000D4FF", legacy=True),

    # -- signature: one designed look per job to be done -----------------------
    # Each of these fixes a typeface, a depth treatment (hard shadow, coloured
    # stroke or halo) and a cadence together. They are grouped by what the clip
    # IS -- gameplay, podcast, story -- because that is how people choose, and
    # the first three of each kind are what Auto reaches for by genre.
    #
    # Gameplay: loud, fast, reads over a busy frame.
    _p("sig_beast", "Beast", "signature",
       font_id="bangers", size=134, uppercase=True, base=W, active=YELLOW,
       outline=9, shadow=7, shadow_color=BLACK, keyword=GREEN,
       keyword_scale=118, active_effect="pop", entrance="punch", max_words=3,
       spacing=2),
    _p("sig_clutch", "Clutch", "signature",
       font_id="anton", size=130, uppercase=True, base=W, active=RED,
       outline=8, shadow=6, shadow_color=BLACK, keyword=YELLOW,
       keyword_scale=122, active_effect="scale", active_scale=116,
       entrance="punch", max_words=3, rotate=2, spacing=1),
    _p("sig_arcade", "Arcade", "signature",
       font_id="bungee", size=110, uppercase=True, base=W, active=ELECTRIC,
       outline=6, glow=HOT_PINK, keyword=HOT_PINK, active_effect="pop",
       entrance="pop", max_words=3),
    _p("sig_wordpop", "Word Pop", "signature",
       font_id="lilitaone", size=124, uppercase=True, base=W, active=LIME,
       outline=10, shadow=5, shadow_color=BLACK, keyword=YELLOW,
       active_effect="pop", reveal=True, max_words=3, position="middle"),
    _p("sig_comic", "Comic", "signature",
       font_id="bangers", size=138, uppercase=True, base=YELLOW, active=W,
       outline=9, shadow=8, shadow_color=RED, keyword=W, keyword_scale=114,
       mode="phrase", entrance="punch", max_words=3, rotate=-3, spacing=2),
    # Podcast and talking head: the point of the line, readable at a glance.
    _p("sig_mogul", "Mogul", "signature",
       font_id="anton", size=150, uppercase=True, base=W, active=YELLOW,
       outline=8, shadow=5, shadow_color=BLACK, keyword=YELLOW,
       mode="stack", active_effect="pop", reveal=True, max_words=4,
       position="middle", spacing=1),
    _p("sig_deepdive", "Deep Dive", "signature",
       font_id="poppins", size=82, base=W, active=W, outline=12,
       text_outline=4, shadow=3, box="word", box_color=VIOLET, active_effect="marker", keyword=W,
       entrance="fade", max_words=5, position="lower"),
    _p("sig_podpro", "Pod Pro", "signature",
       font_id="poppins", size=84, base=W, active=YELLOW, outline=6,
       shadow=4, shadow_color=BLACK, keyword=YELLOW, keyword_scale=110,
       active_effect="color", entrance="fade", max_words=5, position="lower"),
    _p("sig_bubble", "Bubble", "signature",
       font_id="titanone", size=102, base=W, active=YELLOW, outline=10,
       outline_color=VIOLET, shadow=6, shadow_color="&H00501830",
       keyword=YELLOW, active_effect="pop", entrance="bounce", max_words=3),
    _p("sig_neon", "Neon Tube", "signature",
       font_id="archivoblack", size=96, uppercase=True, base=W,
       active=W, outline=4, outline_color="&H00D08000", glow=ELECTRIC,
       keyword=ELECTRIC, active_effect="scale", active_scale=114,
       entrance="fade", max_words=3),

    # -- pro: the house looks, modelled on what KeyMo and OpusClip ship ----
    # These are not another palette. Each one is a complete recipe -- size,
    # weight, effect and cadence -- because that is what the reference styles
    # are, and rebuilding them from separate colour/size/effect menus is how
    # you end up with a caption that is 90% of the way to none of them.
    # The reference style: all caps, heavy white type, thick black outline and
    # the spoken word filled in the light blue KeyMo/Opus use. Sentence case
    # reads as "podcast"; this reads as short-form.
    _p("pro_caps", "Pro Caps", "pro",
       font="Archivo Black", font_id="archivoblack", size=92,
       uppercase=True, base=W,
       active="&H00FF9B5A", outline=7, shadow=2,
       active_effect="color", entrance="fade", max_words=2,
       keyword="&H00FF9B5A"),
    _p("pro_glow", "Pro Glow", "pro",
       size=88, base=W, active=CYAN, outline=4, shadow=2,
       active_effect="glow", entrance="fade", max_words=4, keyword=YELLOW),
    _p("pro_pop", "Pro Pop", "pro",
       size=90, uppercase=True, base=W, active=YELLOW, outline=6,
       active_effect="pop", entrance="punch", max_words=3, keyword=YELLOW),
    _p("pro_chip", "Pro Chip", "pro",
       size=84, uppercase=True, base=W, active=INK, outline=16,
       box="word", box_color=YELLOW, active_effect="marker",
       entrance="fade", max_words=4, keyword=YELLOW),
    _p("pro_soft", "Pro Soft", "pro",
       size=80, base=CREAM, active=YELLOW, outline=3, shadow=4,
       active_effect="color", entrance="fade", max_words=6, position="lower"),

    # -- karaoke: one family, seven ways of marking the spoken word --------
    _p("karaoke_fill", "Fill", "karaoke", size=84,
       active=LIME, active_effect="color", entrance="fade"),
    _p("karaoke_grow", "Grow", "karaoke",
       active=LIME, active_effect="scale", active_scale=115, entrance="fade"),
    _p("karaoke_pop", "Pop", "karaoke",
       active=YELLOW, active_effect="pop", entrance="fade"),
    _p("karaoke_underline", "Underline", "karaoke",
       active=W, active_effect="underline", entrance="fade"),
    _p("karaoke_glow", "Glow", "karaoke",
       active=CYAN, active_effect="glow", outline=4, entrance="fade"),
    _p("karaoke_marker", "Marker", "karaoke",
       active=INK, outline=14, box="word", box_color=YELLOW,
       active_effect="marker", entrance="fade"),
    _p("karaoke_progress", "Progress", "karaoke",
       active=LIME, active_effect="progress", entrance="fade"),

    # -- hormozi: 2-3 huge words, replaced wholesale ------------------------
    _p("hormozi_bold", "Hormozi Bold", "hormozi",
       size=104, uppercase=True, outline=8, max_words=3, mode="phrase",
       active=YELLOW, keyword=YELLOW, entrance="pop", position="middle"),
    _p("hormozi_yellow", "Hormozi Yellow", "hormozi",
       size=104, uppercase=True, outline=8, max_words=3, mode="phrase",
       base=YELLOW, active=W, keyword=W, entrance="pop", position="middle"),
    _p("hormozi_punch", "Hormozi Punch", "hormozi",
       size=112, uppercase=True, outline=9, max_words=2, mode="phrase",
       active=GREEN, keyword=GREEN, entrance="punch", position="middle"),
    _p("hormozi_minimal", "Hormozi Minimal", "hormozi",
       size=92, uppercase=True, outline=6, max_words=3, mode="phrase",
       active=W, keyword=W, entrance="fade", position="middle"),

    # -- hype: aggressive, tilted, for gameplay and reactions --------------
    _p("hype_beast", "Beast", "hype",
       size=100, uppercase=True, outline=10, max_words=3, mode="phrase",
       active=YELLOW, keyword=YELLOW, entrance="punch", rotate=3,
       position="middle"),
    _p("hype_impact", "Impact", "hype",
       size=96, uppercase=True, outline=9, active=RED, keyword=YELLOW,
       active_effect="scale", active_scale=122, entrance="bounce"),
    _p("hype_bounce", "Bounce", "hype",
       size=88, uppercase=True, outline=7, active=LIME,
       active_effect="pop", entrance="bounce"),
    _p("hype_emoji", "Emoji Pop", "hype",
       size=88, uppercase=True, outline=7, active=YELLOW,
       active_effect="pop", entrance="pop", keyword=GREEN),

    # -- podcast: the ones that do not shout --------------------------------
    # "Does not shout" is about colour and motion, not size. These were first
    # written at 52-62, which is genuinely small on a 1080-wide canvas, and the
    # podcast frame hands captions roughly half the height -- a single thin line
    # floating in that much space read as a mistake rather than as restraint.
    # Sized so a normal cue wraps to two lines and fills the band, which is the
    # shape these clips actually have on the platforms.
    # The outline is not decoration. These used to have none, which was correct
    # when the podcast frame was letterboxed and captions sat on a flat dark
    # bar. The frame is face-aware now and fills the canvas, so captions land on
    # a bookshelf or a lit face instead -- unoutlined cream on that is mush.
    _p("podcast_clean", "Podcast Clean", "podcast",
       size=78, base=CREAM, active=CREAM, outline=4, shadow=3,
       active_effect="color", entrance="fade", max_words=6, position="lower"),
    _p("podcast_bold", "Podcast Bold", "podcast",
       size=74, base=W, active=W, outline=3, shadow=3,
       active_effect="underline", entrance="fade", max_words=5, position="lower"),
    # The lower third stays smaller: it is a plate with text inside, and the
    # plate grows with the text until it stops reading as a lower third.
    _p("podcast_lower", "Lower Third", "podcast",
       size=62, base=INK, active=INK, outline=16, box="line", box_color=CREAM,
       active_effect="color", entrance="fade", max_words=7, position="lower"),

    # -- social: reads as a platform's own caption --------------------------
    _p("social_tiktok", "TikTok", "social",
       size=70, base=W, active=W, outline=6, active_effect="color",
       entrance="none", max_words=5, position="middle"),
    _p("social_chip", "Rounded", "social",
       size=64, base=W, active=W, outline=16, box="line", box_color="&H00201818",
       active_effect="color", entrance="fade", max_words=5, position="lower"),
    _p("social_caps", "Native Caps", "social",
       size=72, uppercase=True, base=W, active=W, outline=6,
       active_effect="scale", active_scale=110, entrance="none", max_words=4,
       position="middle"),

    # -- cinematic: wide tracking, calm, low on the frame -------------------
    _p("cinema_film", "Film", "cinematic",
       size=54, base=CREAM, active=CREAM, outline=0, shadow=3, spacing=3,
       active_effect="color", entrance="fade", max_words=8, position="lower"),
    _p("cinema_doc", "Documentary", "cinematic",
       size=50, base=W, active=W, outline=2, shadow=2, spacing=2,
       active_effect="color", entrance="fade", max_words=8, position="lower"),
    _p("cinema_luxury", "Luxury", "cinematic",
       size=56, base=CREAM, active="&H00B0D0E8", outline=0, shadow=4, spacing=5,
       active_effect="color", entrance="fade", max_words=7, position="lower"),

    # -- neon: glow and colour ramps ---------------------------------------
    _p("neon_cyber", "Cyberpunk", "neon",
       size=86, uppercase=True, base=W, active=MAGENTA, outline=5,
       active_effect="glow", entrance="fade", keyword=CYAN),
    _p("neon_electric", "Electric", "neon",
       size=86, uppercase=True, base=W, active=CYAN, outline=5,
       active_effect="glow", entrance="pop", keyword=W),
    _p("neon_gradient", "Gradient", "neon",
       size=88, uppercase=True, base=W, active=PURPLE, outline=6,
       active_effect="gradient", entrance="fade", keyword=MAGENTA),
    _p("neon_gaming", "Gaming Green", "neon",
       size=88, uppercase=True, base=W, active=GREEN, outline=6,
       active_effect="glow", entrance="pop", keyword=LIME),

    # -- story: typewriter reveals -----------------------------------------
    _p("story_type", "Typewriter", "story",
       size=62, base=CREAM, active=CREAM, outline=0, shadow=3,
       mode="typewriter", entrance="none", max_words=6, position="lower"),
    _p("story_terminal", "Terminal", "story",
       size=58, base=GREEN, active=GREEN, outline=0, shadow=2, spacing=2,
       mode="typewriter", entrance="none", max_words=6, position="lower"),

    # -- vibrant: the base text carries the colour ---------------------------
    _p("vibrant_sunset", "Sunset", "vibrant",
       size=86, uppercase=True, base=ORANGE, active=W, outline=7,
       active_effect="scale", active_scale=114, entrance="pop", keyword=YELLOW),
    _p("vibrant_mint", "Mint", "vibrant",
       size=82, base=LIME, active=W, outline=6,
       active_effect="color", entrance="fade", keyword=W),
    _p("vibrant_bubblegum", "Bubblegum", "vibrant",
       size=84, uppercase=True, base=MAGENTA, active=YELLOW, outline=7,
       active_effect="pop", entrance="pop", keyword=W),
    _p("vibrant_ice", "Ice", "vibrant",
       size=82, base=CYAN, active=W, outline=6,
       active_effect="glow", entrance="fade", keyword=W),
    _p("vibrant_gold", "Gold", "vibrant",
       size=88, uppercase=True, base=YELLOW, active=W, outline=8,
       active_effect="scale", active_scale=112, entrance="fade", keyword=W),
    _p("vibrant_crimson", "Crimson", "vibrant",
       size=88, uppercase=True, base=RED, active=W, outline=8,
       active_effect="pop", entrance="punch", keyword=YELLOW),
    _p("vibrant_royal", "Royal", "vibrant",
       size=84, base=PURPLE, active=CYAN, outline=7,
       active_effect="glow", entrance="fade", keyword=W),

    # -- clean: plates and quiet type ---------------------------------------
    _p("clean_plate", "White Plate", "clean",
       size=62, base=INK, active=INK, outline=16, box="line", box_color=W,
       active_effect="underline", entrance="fade", max_words=6, position="lower"),
    _p("clean_dark", "Dark Plate", "clean",
       size=62, base=W, active=LIME, outline=16, box="line",
       box_color="&H00181414", active_effect="color", entrance="fade",
       max_words=6, position="lower"),
    _p("clean_soft", "Soft", "clean",
       size=66, base=CREAM, active=W, outline=0, shadow=4,
       active_effect="color", entrance="fade", max_words=6, position="lower"),
    _p("clean_editorial", "Editorial", "clean",
       size=58, base=W, active=YELLOW, outline=3, shadow=3, spacing=2,
       active_effect="underline", entrance="fade", max_words=7, position="lower"),
]

PRESETS = {p["id"]: p for p in _LIST}
DEFAULT_PRESET = "classic"

# "gradient" is declared on a preset but is not a real ASS capability -- see
# subtitles._active_override. Registered here so the picker can describe it.
ACTIVE_EFFECTS["gradient"] = {
    "name": "Gradient", "desc": "Colour ramps across the words", "web": "gradient",
}


def get(preset_id: str) -> dict:
    """A preset by id, falling back to the default rather than raising.

    An unknown id means an old saved clip or a hand-edited request; neither is a
    reason to fail an export that can perfectly well render in the default look.
    """
    return PRESETS.get(preset_id) or PRESETS[DEFAULT_PRESET]


# ---------------------------------------------------------------------------
# Serving the registry to the browser
# ---------------------------------------------------------------------------

def ass_to_css(colour: str) -> str:
    """&HAABBGGRR -> #RRGGBB. Alpha is dropped; nothing here is translucent."""
    if not colour:
        return "#ffffff"
    h = colour.replace("&H", "").replace("&", "")
    h = h.rjust(8, "0")[-6:]        # BBGGRR
    return f"#{h[4:6]}{h[2:4]}{h[0:2]}"


def css_to_ass(colour: str) -> str:
    """#RRGGBB -> &H00BBGGRR. The inverse of ass_to_css.

    Lets the editor hand back a plain CSS colour from a colour input without
    knowing anything about ASS byte order.
    """
    if not colour:
        return W
    h = str(colour).strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return W
    try:
        int(h, 16)
    except ValueError:
        return W
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def web_presets() -> dict:
    """The registry in browser terms, so the picker can render live previews.

    Every colour arrives as CSS hex and every axis the preview needs is named
    explicitly. The editor does no translation of its own -- that was the source
    of the old drift between preview and export.
    """
    out = []
    for p in _LIST:
        out.append({
            "id": p["id"],
            "name": p["name"],
            "category": p["category"],
            "legacy": p["legacy"],
            "px": p["size"],
            "color": ass_to_css(p["base"]),
            "active": ass_to_css(p["active"]),
            "keyword": ass_to_css(p["keyword"]),
            "box": p["box"],
            "boxColor": ass_to_css(p["box_color"]) if p["box_color"] else None,
            "outline": p["outline"],
            "shadow": p["shadow"],
            "caps": p["uppercase"],
            "spacing": p["spacing"],
            "rotate": p["rotate"],
            "mode": p["mode"],
            "effect": p["active_effect"],
            "entrance": p["entrance"],
            "activeScale": p["active_scale"],
            "maxWords": p["max_words"],
            "reveal": bool(p.get("reveal")),
            "position": p["position"],
            # Depth and emphasis. `font` is a slug the editor resolves against
            # the served font list, so a preset previews in its own typeface.
            "font": p["font_id"],
            "outlineColor": ass_to_css(p["outline_color"]) if p["outline_color"] else None,
            "shadowColor": ass_to_css(p["shadow_color"]) if p["shadow_color"] else None,
            "keywordScale": p["keyword_scale"],
            "glow": ass_to_css(p["glow"]) if p["glow"] else None,
            "reveal": p["reveal"],
            "stackSmall": p["stack_small"],
            "textOutline": p["text_outline"],
        })
    return {
        "categories": CATEGORIES,
        "presets": out,
        "effects": [{"id": k, **v} for k, v in ACTIVE_EFFECTS.items()],
        "default": DEFAULT_PRESET,
    }
