"""Game HUD awareness.

A 9:16 crop of a 16:9 game frame is a ~1.8x enlargement that also throws away
everything outside a narrow central column. On a GTA-style HUD that column
usually keeps the action and loses the minimap, the weapon wheel and the wanted
stars -- the parts a viewer uses to understand what is happening.

Two signals decide the treatment:

  * a title/profile prior ("GTA", "Grand Theft Auto") because those games share
    a corner-HUD layout even when ten seconds of footage is too dark to measure;
  * a cheap temporal check -- HUD elements are the only parts of a game frame
    that stay pixel-identical while the world moves behind them.

When a HUD is present the action plan prefers a HUD-safe full-frame composition
instead of a crop: the whole game stays readable, with the letterbox filled by
a blurred copy of the same footage. That is less immersive than a crop and
much better than cutting the minimap in half.
"""

from __future__ import annotations

import cv2
import numpy as np

# Normalised (x, y, w, h) priors for a GTA-style corner HUD. Kept loose: these
# describe where the UI lives, not exact pixels, and only decide a treatment.
GTA_REGIONS = {
    "minimap": (0.01, 0.66, 0.22, 0.30),
    "status": (0.01, 0.90, 0.34, 0.09),
    "weapon": (0.78, 0.68, 0.21, 0.22),
    "objective": (0.01, 0.03, 0.34, 0.12),
}

# Short names used in logs/plan; broad on purpose.
PROFILE_HINTS = ("gta", "grand theft auto", "gta vi", "gta 6", "gta v")


def profile_for(title: str = "", genre: str = "") -> str:
    text = f" {title or ''} ".lower()
    if any(hint in text for hint in PROFILE_HINTS):
        return "gta"
    return "generic"


def _rect(x, y, w, h):
    return (round(float(x), 4), round(float(y), 4),
            round(float(w), 4), round(float(h), 4))


def detect(video_path, start, end, src_w, src_h, title: str = "",
           genre: str = "", frames: int = 8):
    """Return {profile, present, regions, minimap, confidence}.

    Cheap by design: eight 240px frames, one variance map, one Canny pass per
    frame. It is a treatment decision, not a UI parser.
    """
    profile = profile_for(title, genre)
    fallback = {
        "profile": profile,
        "present": profile == "gta",
        "regions": [],
        "minimap": None,
        "confidence": 0.0,
    }
    if not video_path or not src_w or not src_h:
        return fallback

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return fallback
    span = max(0.1, float(end) - float(start))
    grays, edges = [], []
    try:
        for i in range(frames):
            t = float(start) + (i + 0.5) * span / frames
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                continue
            small = cv2.resize(frame, (240, 135))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            grays.append(gray.astype(np.float32))
            edges.append(cv2.Canny(gray, 60, 160))
    finally:
        cap.release()

    if len(grays) < 4:
        return fallback

    stack = np.stack(grays)
    variance = stack.std(axis=0)
    edge = np.stack(edges).mean(axis=0) / 255.0
    h, w = variance.shape
    rows, cols = 6, 8
    cell_h, cell_w = h // rows, w // cols
    candidates = []
    for r in range(rows):
        if not (r <= 1 or r >= 4):
            continue          # UI lives at the top or bottom, not mid-screen
        for c in range(cols):
            ys, xs = r * cell_h, c * cell_w
            v = float(variance[ys:ys + cell_h, xs:xs + cell_w].mean())
            e = float(edge[ys:ys + cell_h, xs:xs + cell_w].mean())
            if v < 7.0 and e > 0.035:
                candidates.append((r, c, v, e))

    if not candidates:
        return fallback

    buckets = {
        "bottom-left": lambda r, c: r >= 4 and c <= 2,
        "bottom-right": lambda r, c: r >= 4 and c >= 5,
        "top-left": lambda r, c: r <= 1 and c <= 3,
        "top-right": lambda r, c: r <= 1 and c >= 4,
    }
    regions = []
    for name, test in buckets.items():
        cells = [(r, c) for r, c, _, _ in candidates if test(r, c)]
        if len(cells) < 2:
            continue
        r0 = min(r for r, _ in cells)
        r1 = max(r for r, _ in cells) + 1
        c0 = min(c for _, c in cells)
        c1 = max(c for _, c in cells) + 1
        conf = len(cells) / max(1, (r1 - r0) * (c1 - c0))
        if conf < 0.35:
            continue
        regions.append({
            "kind": name,
            "rect": _rect(c0 / cols, r0 / rows,
                          (c1 - c0) / cols, (r1 - r0) / rows),
            "confidence": round(conf, 2),
        })

    if not regions:
        return fallback

    minimap = next((r["rect"] for r in regions if r["kind"] == "bottom-left"),
                   None)
    # One corner can be a static wall or a scoreboard. Two opposite corners, or
    # the GTA prior, are what make it a HUD rather than a coincidence.
    opposite = any(r["kind"] == "bottom-right" for r in regions) and \
        any(r["kind"] == "bottom-left" for r in regions)
    present = profile == "gta" or opposite or len(regions) >= 2
    return {
        "profile": profile,
        "present": present,
        "regions": regions,
        "minimap": minimap,
        "confidence": round(sum(r["confidence"] for r in regions) /
                            max(1, len(regions)), 2),
    }


def contains(hud, x: float, y: float) -> bool:
    """Whether a normalised point sits inside a detected HUD region."""
    for region in (hud or {}).get("regions", []):
        rx, ry, rw, rh = region["rect"]
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            return True
    return False


def caption_pos(hud) -> float:
    """Where captions sit: above a bottom HUD instead of across it."""
    if (hud or {}).get("present"):
        return 0.82
    return 0.86
