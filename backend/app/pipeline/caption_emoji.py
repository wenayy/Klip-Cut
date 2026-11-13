"""Chooses emoji for caption cues, using the model to judge relevance.

Emoji are decoration. Decoration must never be why a paid job fails, so every
error path here returns "no emoji" and lets the clip render plain -- a missing
emoji is invisible to the user, while a failed render costs them credits and a
video.

Two things make the difference between this looking authored and looking
automated:

  DENSITY  An emoji on every cue reads as noise. The model is told to mark only
           cues that genuinely earn one, and the result is capped afterwards
           regardless of what came back.
  DRAWABILITY  A font can map a codepoint and still draw nothing -- see the note
           in subtitles._covers, which is the trap that once made the editor
           promise emoji the export silently dropped. Every suggestion is
           checked against the shipped face before it is used.
"""

import os
import re

from app.pipeline import subtitles
from app.pipeline.analyzer import _openai_chat

# Roughly one cue in three. Past that the emoji stop being emphasis and start
# being wallpaper -- and the eye has nowhere to land.
MAX_DENSITY = 0.14
# Keyword pop-ups (captions off) are already only the lines that matter, and
# the emoji is half of what is left on screen -- so most of them (3 in 5) get one.

# Skin-tone modifiers and ZWJ sequences (families, professions, couples) are the
# ones that fall apart: a font either lacks the composed glyph and draws the
# pieces separately, or draws nothing. Single codepoints are what survive.
_ZWJ = "‍"
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_VARIATION_SELECTOR = 0xFE0F

_PROMPT = """You are adding emoji to captions for a short-form video clip.

Below are the numbered caption cues, in order. Pick the cues where ONE emoji
genuinely reinforces what is being said -- a concrete object, a strong emotion,
a number, a reaction. Skip cues that are connective tissue or filler, and skip
cues that are already vivid without help.

{target}

Rules:
- Mark at most {budget} cues. Fewer is better than forcing them. If you are
  unsure about a cue, leave it out.
- Exactly one emoji per marked cue, never two.
- Single common emoji only. No flags, no skin-tone modifiers, and no combined
  sequences such as families or professions -- they do not render reliably.
- If a cue does not clearly call for an emoji, leave it out entirely.

Cues:
{cues}

Reply with JSON only, in this exact shape:
{{"emoji": {{"3": "\U0001f525", "7": "\U0001f4b0"}}}}
"""


def _is_simple_emoji(text: str) -> bool:
    """One drawable pictograph, not a composed sequence."""
    if not text or _ZWJ in text:
        return False
    points = [ord(c) for c in text if ord(c) != _VARIATION_SELECTOR]
    if len(points) != 1:
        return False
    point = points[0]
    if point in _SKIN_TONES:
        return False
    # The ranges the caption renderer already recognises as emoji; anything
    # outside them would not be routed to the emoji face downstream anyway.
    return (0x1F300 <= point <= 0x1FAFF or 0x2600 <= point <= 0x27BF
            or 0x1F000 <= point <= 0x1F2FF or point in (0x2B50, 0x2B55))


def _drawable(text: str, face: dict) -> bool:
    """Whether the shipped emoji face can actually put this on screen.

    A cmap hit is not enough. subtitles._covers is the honest test and is reused
    rather than reimplemented, because the subtle half of it -- COLRv1 and sbix
    glyphs that exist but rasterise to nothing -- is exactly what this guard is
    for.
    """
    path = os.path.join(subtitles.FONTS_DIR, face["file"])
    return all(subtitles._covers(path, ord(c)) for c in text
               if ord(c) != _VARIATION_SELECTOR)


# ---------------------------------------------------------------------------
# The no-model path
# ---------------------------------------------------------------------------
# Emoji are not worth a hard dependency on a working API key. The owner's own
# account had a dead key and every clip rendered emoji-free because of it --
# which is the same class of bug as the font that silently drew nothing. These
# rules cover the vocabulary short-form clips actually use; the model is still
# asked first, because it reads context a keyword list cannot, and this only
# runs when that answer is unavailable.
_EMOJI_RULES = (
    # Content-creation vocabulary, which is what most of these clips are about.
    (r"\b(clip|clips|video|editing|edit|cut|footage)\b", "\U0001f3ac"),
    (r"\b(podcast|microphone|mic)\b", "\U0001f399"),
    (r"\b(camera|film|shoot|record)\b", "\U0001f3a5"),
    (r"\b(voice|talk|speak|say)\b", "\U0001f5e3"),
    (r"\b(audience|followers|views|viral|reach)\b", "\U0001f4e3"),
    (r"\b(money|cash|revenue|profit|dollars?|bucks|price|cost|rich|million|billion|salary|pay)\b", "\U0001f4b0"),
    (r"\b(grow|growth|scale|up|increase|higher)\b", "\U0001f4c8"),
    (r"\b(drop|down|crash|lose|lost|fail|fall)\b", "\U0001f4c9"),
    (r"\b(fire|insane|crazy|wild|unbelievable|nuts|brutal)\b", "\U0001f525"),
    (r"\b(laugh|lol|funny|joke|hilarious|comedy)\b", "\U0001f602"),
    (r"\b(love|heart|adore|beautiful)\b", "\u2764"),
    (r"\b(brain|smart|genius|idea|mind)\b", "\U0001f9e0"),
    (r"\b(time|clock|minutes?|hours?|seconds?|wait|late)\b", "\u23f0"),
    (r"\b(work|job|business|company|startup|office|hire)\b", "\U0001f4bc"),
    (r"\b(win|won|victory|champion|best|first|record)\b", "\U0001f3c6"),
    (r"\b(nervous|anxious|scared|afraid|worried|panic)\b", "\U0001f630"),
    (r"\b(game|gaming|play|player|level|boss)\b", "\U0001f3ae"),
    (r"\b(code|coding|developer|programming|software|build)\b", "\U0001f4bb"),
    (r"\b(phone|call|text|message)\b", "\U0001f4f1"),
    (r"\b(food|eat|hungry|pizza|lunch|dinner)\b", "\U0001f355"),
    (r"\b(sleep|tired|night|bed)\b", "\U0001f634"),
    (r"\b(scary|afraid|fear|danger|warning|careful)\b", "\U0001f631"),
    (r"\b(secret|hidden|reveal|surprise)\b", "\U0001f92b"),
    (r"\b(strong|power|force|muscle|gym)\b", "\U0001f4aa"),
    (r"\b(look|watch|see|eyes|view)\b", "\U0001f440"),
    (r"\b(rocket|launch|fast|speed|quick|instant)\b", "\U0001f680"),
    (r"\b(idea|light|create|new|invent)\b", "\U0001f4a1"),
    (r"\b(no|stop|wrong|bad|never)\b", "\U0001f6ab"),
    (r"\b(yes|okay|agree|right|exactly)\b", "\U0001f44d"),
)

_RULES_RE = tuple((re.compile(pattern, re.IGNORECASE), mark)
                  for pattern, mark in _EMOJI_RULES)


_SPARSE_TARGET = """A referenced clip earning its keep marked roughly one cue in ten. Treat that as
the target, not a ceiling: most cues should get nothing. An emoji that is merely
adjacent to the topic ("a camera should follow the voice" -> a camera) is noise;
mark only where the emoji carries the feeling or the object the line is actually
about."""

_POPUP_TARGET = """These cues are the clip's key phrases, shown on their own with no other
captions, so the emoji carries the feeling of the moment. Mark most of them:
pick the emoji for what the phrase means in context, not a literal object."""


def _local_plan(texts: list, budget: int, face: dict, min_words: int = 2) -> dict:
    """Keyword-matched emoji for when the model is unavailable.

    One emoji per cue, only for cues that contain a concrete keyword, and never
    more than the same density cap the model works under. Connective cues
    ("and then he said") match nothing and stay clean by construction.
    """
    chosen = {}
    used = set()
    for index, text in enumerate(texts):
        if len(chosen) >= budget:
            break
        if len(text.split()) < min_words:
            continue
        for pattern, mark in _RULES_RE:
            # Never the same emoji twice in one clip. Two film reels on
            # consecutive cues is the tell that a keyword list, not an editor,
            # chose them.
            if mark in used:
                continue
            if pattern.search(text) and _drawable(mark, face):
                chosen[index] = mark
                used.add(mark)
                break
    return chosen


def _cue_text(cue: list) -> str:
    return " ".join(
        (w.get("punctuated_word") or w.get("word") or "") for w in cue
    ).strip()


def plan(words: list, style: str = "classic", max_words: int = None,
         popups: bool = False) -> dict:
    """{cue index: emoji} for the cues worth marking. Empty dict on any problem.

    Cues are grouped here with the same rule build_ass uses, so the indices this
    returns line up with the ones it iterates. `popups` is the captions-off
    mode: the words are keyword pop-ups only, and most of them earn an emoji. Passing the map in -- rather than
    calling the model from inside the renderer -- keeps build_ass free of
    network calls and testable without one.
    """
    face = subtitles.emoji_font()
    if not face:
        # No face can draw emoji, so skip the API call entirely rather than pay
        # for suggestions that would all be discarded.
        return {}

    limit = (max(1, min(8, int(max_words))) if max_words else
             subtitles.STYLES.get(
                 style, subtitles.STYLES["classic"]).get("max_words"))
    cues = list(subtitles.group_words(words, limit))
    texts = [_cue_text(c) for c in cues]
    if not any(texts):
        return {}

    budget = (-(-len(cues) * 3 // 5) if popups              # ceil(0.6 n)
              else max(1, int(len(cues) * MAX_DENSITY)))
    min_words = 1 if popups else 2
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(texts) if t)

    try:
        from app.pipeline.analyzer import BULK_MODEL
        reply = _openai_chat(
            _PROMPT.format(budget=budget, cues=listing,
                           target=_POPUP_TARGET if popups else _SPARSE_TARGET),
            temperature=0.3,
            model=BULK_MODEL)
    except (RuntimeError, ValueError) as exc:
        print(f"[clipper] emoji model unavailable ({exc}); using keyword "
              f"matching instead")
        return _local_plan(texts, budget, face, min_words)

    raw = reply.get("emoji") if isinstance(reply, dict) else None
    if not isinstance(raw, dict):
        return _local_plan(texts, budget, face, min_words)

    chosen = {}
    for key, mark in raw.items():
        if len(chosen) >= budget:
            break
        try:
            index = int(str(key).strip().rstrip("."))
        except ValueError:
            continue
        if not (0 <= index < len(cues)) or index in chosen:
            continue
        mark = (mark or "").strip()
        # No font check here any more: the mark is rendered as a Twemoji PNG,
        # not a glyph, so what matters is that it is one simple codepoint.
        # `emoji_overlay` fetches the artwork and drops the mark if it cannot.
        if _is_simple_emoji(mark):
            chosen[index] = mark

    if not chosen:
        # The model answered but marked nothing. That is a legitimate answer for
        # a dry clip; do not override it with keywords.
        return {}
    print(f"[clipper] emoji on {len(chosen)} of {len(cues)} cues")
    return chosen
