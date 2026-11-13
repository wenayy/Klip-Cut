import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { getTranscript, saveClipEdit, exportClip, previewUrl,
         uploadOverlayImage, overlayImageUrl, gameplayUrl,
         getClipReframe, getGameplayList, gameplayFileUrl } from "./api";
import { FontPicker, SizePicker, LanguagePicker, useFonts,
         useTitleStyles, titleLookCss } from "./CaptionToolbar";
import { useEditHistory } from "./useEditHistory";
import { PLATFORMS, platformOverlay, BACKGROUNDS, logoSrc } from "./EditorPresets";
import TimelineTracks from "./TimelineTracks";
import CaptionPresetPicker, { useCaptionPresets } from "./CaptionPresets";
import { useToast } from "./Toast";
import posthog, { isPostHogConfigured } from "./posthog";

/* Caption looks come from the server (`/caption-options`), which serves the
   renderer's own preset registry translated into CSS terms. This file used to
   keep a hand-written copy of backend subtitles.STYLES; the two drifted on both
   sizes and colours, so the preview was confidently wrong about the export.

   FALLBACK_STYLE is only what to draw in the frame or two before the fetch
   lands -- never a second source of truth. */
const FALLBACK_STYLE = {
  color: "#fff", active: "#d8f24e", px: 76, box: "none", outline: 5,
  caps: false, effect: "color", mode: "karaoke", maxWords: 4, position: "bottom",
};

/* Caption and title sizes are absolute px on the OUTPUT canvas, whose width
   changes with the ratio (1080 at 9:16 and 1:1, 1920 at 16:9). Dividing by a
   fixed 1080 made every 16:9 preview draw text ~78% too large. The live width
   comes from `outW` below. */

/* Mirrors backend subtitles.TITLE_STYLE, so the card previews where and when it
   actually burns in. */
const TITLE_PX = 92;
const TITLE_SECONDS = 4.5;
const TITLE_TOP = 0.07;

/* Mirrors backend subtitles.group_words. The renderer shows short cues, not
   whole sentences, so a preview built from utterance text was showing captions
   that would never appear in the export. Same rules, same breaks. */
function groupWords(words, maxWords, maxSeconds) {
  const cues = [];
  let cur = [];
  for (const w of words) {
    cur.push(w);
    const span = cur[cur.length - 1].e - cur[0].t;
    const endsSentence = /[.!?]$/.test(w.w || "");
    if (cur.length >= maxWords || span >= maxSeconds || endsSentence) {
      cues.push(cur);
      cur = [];
    }
  }
  if (cur.length) cues.push(cur);
  return cues;
}

/* Fallback only. The real face list comes from the server via useFonts(), so a
   newly installed font previews in ITSELF rather than silently falling back. */
const FONT_FALLBACK = "'Archivo Black', sans-serif";

const RATIOS = { "9:16": 9 / 16, "1:1": 1, "16:9": 16 / 9 };

/* Rough but sufficient: the pictographic blocks plus the variation selector and
   zero-width joiner that hold sequences together. Used only to warn, never to
   rewrite what the user typed. */
const EMOJI_RE = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}]/u;

/* The renderer draws a stroke around each glyph (ASS Outline). Four offsets
   approximate it; -webkit-text-stroke thins glyphs inward instead of growing
   them outward, which is visibly not what burns in. A "line" box preset already
   has a plate behind it and wants no stroke at all. */
function strokeShadow(st) {
  /* A plain plate has no stroke; a backdrop band (textOutline set) strokes the
     text on top of it. */
  if (st.box === "line" && st.textOutline == null) return "none";
  const px = st.px || 76;
  /* A chip-layer preset strokes its text at textOutline; `outline` is then the
     chip's padding, not a stroke. */
  const strokePx = st.textOutline ?? (st.box === "word" ? 0 : st.outline || 0);
  const w = Math.min(0.09, (strokePx / px) * 0.9);
  const ink = st.outlineColor || "#000";
  const layers = [];
  if (w) {
    /* Eight offsets, not four: at the widths the signature looks use, the four
       cardinal copies leave visible notches on the diagonals. */
    const d = w * 0.7071;
    layers.push(`${w}em 0 0 ${ink}`, `-${w}em 0 0 ${ink}`,
                `0 ${w}em 0 ${ink}`, `0 -${w}em 0 ${ink}`,
                `${d}em ${d}em 0 ${ink}`, `-${d}em ${d}em 0 ${ink}`,
                `${d}em -${d}em 0 ${ink}`, `-${d}em -${d}em 0 ${ink}`);
  }
  if (st.shadow) {
    const off = (st.shadow / px) + w;
    /* A named shadow colour is the hard, opaque offset of sticker type; the
       default is the renderer's soft translucent black. */
    layers.push(st.shadowColor
      ? `${off}em ${off}em 0 ${st.shadowColor}`
      : `${off}em ${off}em 0.05em rgba(0,0,0,.55)`);
  }
  if (st.glow) layers.push(`0 0 .22em ${st.glow}`, `0 0 .5em ${st.glow}`);
  return layers.length ? layers.join(", ") : "none";
}

/* How the spoken word is marked, mirroring subtitles._active_override. This is
   the one place the editor interprets a preset, and it stays a small function
   for that reason -- everything it reads was computed by the server. */
const PREVIEW_RAMP = ["#8040f0", "#d060e0", "#f080d0", "#60d0f0"];

function previewWordStyle(st, i, activeIndex, count, slot = undefined) {
  const keyword = slot !== undefined;
  const active = i === activeIndex;
  /* Mirrors subtitles._compose: a stacked look draws its lead-in small on one
     line and the last word at full size on the next. */
  const stacked = st.mode === "stack" && count > 1;
  const big = stacked && i === count - 1;
  const layout = stacked
    ? (big
      ? { display: "block", color: st.active }
      : { fontSize: `${(st.stackSmall || 58) / 100}em` })
    : undefined;
  // Words that have not been spoken yet hold their place but stay hidden.
  if (st.reveal && i > activeIndex) return { ...layout, visibility: "hidden" };

  if (st.effect === "gradient") {
    return { ...layout, color: PREVIEW_RAMP[i % PREVIEW_RAMP.length] };
  }
  // Phrase presets replace the whole chunk and mark nothing inside it -- except
  // keywords, which are the point of the line in every look.
  if (!active || st.mode === "phrase") {
    return keyword ? { ...layout, ...keywordStyle(st, slot) } : layout;
  }
  const lit = { ...layout, ...activeWordStyle(st) };
  // A look whose highlight is pure motion keeps a keyword's colour while it pops.
  if (keyword && st.active === st.color) lit.color = keywordColour(st, slot);
  // The big word of a stack owns its line whichever effect is marking it.
  if (big) lit.display = "block";
  return lit;
}

/* Mirrors subtitles.keyword_hits for one cue: keyword phrases matched as runs
   of words (plurals and possessives count) plus spoken numbers, which are
   always emphasised. */
const NUMBER_WORDS = new Set(["ten", "eleven", "twelve", "fifteen", "twenty",
  "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
  "thousand", "million", "billion", "trillion", "percent", "dozen", "half",
  "double", "triple"]);

function keyToken(text) {
  return (text || "").toLowerCase()
    .replace(/^[.,!?;:"'()\[\]\u0964\u2019\u201c\u201d]+|[.,!?;:"'()\[\]\u0964\u2019\u201c\u201d]+$/g, "")
    .replace(/('|\u2019)s$/, "");
}

function keywordHits(words, keywords) {
  const tokens = words.map(keyToken);
  const phrases = (keywords || []).map((k) => String(k).split(/\s+/).map(keyToken).filter(Boolean))
    .filter((p) => p.length);
  const same = (a, b) => a === b || (b.length > 3 && a.replace(/s+$/, "") === b.replace(/s+$/, ""));
  const hits = new Set();
  tokens.forEach((tok, i) => {
    for (const phrase of phrases) {
      const win = tokens.slice(i, i + phrase.length);
      if (win.length === phrase.length && win.every((t, k) => same(t, phrase[k]))) {
        phrase.forEach((_, k) => hits.add(i + k));
      }
    }
    if (/^[$\u20b9\u20ac\u00a3]?\d[\d,.:]*(%|k|m|b|st|nd|rd|th|s)?$/.test(tok) || NUMBER_WORDS.has(tok)) hits.add(i);
  });
  return hits;
}

/* Mirrors subtitles.keyword_slots: each distinct keyword (a run of hits) takes
   the next palette colour the first time it is said and keeps it, so slots are
   numbered across the whole clip, not per cue. */
function keywordRuns(tokens, hits) {
  const runs = [];
  let run = [];
  [...hits].sort((a, b) => a - b).forEach((i) => {
    if (run.length && i !== run[run.length - 1] + 1) { runs.push(run); run = []; }
    run.push(i);
  });
  if (run.length) runs.push(run);
  return runs.map((r) => ({ idx: r, key: r.map((i) => keyToken(tokens[i])).join(" ") }));
}

function keywordColour(st, slot = 0) {
  const palette = st.keywordColors?.length ? st.keywordColors : [st.keyword || st.active];
  return palette[slot % palette.length];
}

function keywordStyle(st, slot = 0) {
  const colour = keywordColour(st, slot);
  const scale = st.keywordScale && st.keywordScale !== 100 ? st.keywordScale
    : (colour === st.active || colour === st.color ? 112 : 100);
  return scale === 100 ? { color: colour }
    : { color: colour, display: "inline-block", transform: `scale(${scale / 100})` };
}

/* Mirrors subtitles._headline_split: a short function word goes up with the
   word after it instead of becoming the giant word on its own. */
const HEADLINE_STOP = new Set(["the", "a", "an", "and", "so", "but", "or", "to",
  "of", "in", "on", "at", "it", "is", "i", "we", "you", "my", "your", "this",
  "that", "for", "with", "if"]);

function headlineSplit(words) {
  return words.length >= 3 && HEADLINE_STOP.has(keyToken(words[0])) ? 2 : 1;
}

function activeWordStyle(st) {
  switch (st.effect) {
    case "bounce":
      return { color: st.active, display: "inline-block",
               animation: "capBounce 230ms ease-out both" };
    case "marker":
      return { background: st.boxColor || st.active, color: st.color,
               padding: "0 .18em", borderRadius: 4 };
    case "underline":
      return { color: st.active, textDecoration: "underline",
               textUnderlineOffset: ".14em" };
    case "glow":
      return { color: st.active,
               textShadow: `0 0 .25em ${st.active}, 0 0 .5em ${st.active}` };
    case "scale":
    case "pop":
      return { color: st.active, display: "inline-block",
               transform: `scale(${(st.activeScale || 115) / 100})` };
    default:
      return { color: st.active };
  }
}

/* Mirrors reframe.motion_segments + reframe.expr: keyframes reduced to holds and
   moves, each move eased with a smoothstep. Stepping to the last keyframe made
   the preview camera jump twice a second while the export glided. */
const MOTION_EPSILON = 0.5;

function motionSegments(keys, axis) {
  const pts = keys.map((k) => [k[0], Math.round(k[axis] * 10) / 10]);
  if (pts.length <= 2) return pts;
  const dir = (a, b) => {
    const d = b[1] - a[1];
    return Math.abs(d) <= MOTION_EPSILON ? 0 : Math.sign(d);
  };
  const out = [pts[0]];
  for (let i = 1; i < pts.length - 1; i++) {
    if (dir(pts[i - 1], pts[i]) !== dir(pts[i], pts[i + 1])) out.push(pts[i]);
  }
  out.push(pts[pts.length - 1]);
  for (let i = 1; i < out.length; i++) {
    if (Math.abs(out[i][1] - out[i - 1][1]) <= MOTION_EPSILON) {
      out[i] = [out[i][0], out[i - 1][1]];
    }
  }
  return out;
}

const segmentCache = new WeakMap();

function easedAt(keys, axis, t) {
  let byAxis = segmentCache.get(keys);
  if (!byAxis) { byAxis = {}; segmentCache.set(keys, byAxis); }
  const segs = byAxis[axis] || (byAxis[axis] = motionSegments(keys, axis));
  if (t <= segs[0][0]) return segs[0][1];
  for (let i = 1; i < segs.length; i++) {
    const [t1, v1] = segs[i];
    if (t < t1) {
      const [t0, v0] = segs[i - 1];
      const s = Math.min(1, Math.max(0, (t - t0) / Math.max(t1 - t0, 1e-3)));
      return v0 + (v1 - v0) * s * s * (3 - 2 * s);
    }
  }
  return segs[segs.length - 1][1];
}

/* Mirrors render.MIN_CONTENT_FRACTION / MAX_ZOOM / content_height().
   The renderer's default frame is `blur`: the source centred at a computed
   height over a blurred copy of itself -- NOT edge-to-edge. Previewing it with
   a plain object-fit: cover was showing a crop that never gets exported, which
   is why the editor and the finished clip disagreed about what was on screen. */
const MIN_CONTENT_FRACTION = 0.55;
const MAX_ZOOM = 1.75;

/* Mirrors render.FIT_CONTENT_BIAS: in `fit` the video block sits above centre
   so the leftover height becomes a title band and a caption band instead of two
   equal dead margins. */
const FIT_CONTENT_BIAS = 0.42;

/* Mirrors render.PODCAST_CONTENT_BIAS. */
const PODCAST_CONTENT_BIAS = 0.36;

function contentHeightFrac(srcW, srcH, outW, outH) {
  if (!srcW || !srcH) return 1;
  const natural = (outW * srcH) / srcW;        // height at full output width
  if (natural >= outH) return 1;
  const zoom = Math.min(Math.max((outH * MIN_CONTENT_FRACTION) / natural, 1), MAX_ZOOM);
  return Math.min((natural * zoom) / outH, 1);
}

/* The editor's four sections. Grouped by the decision being made rather than by
   which part of the pipeline implements them -- "how do the captions look" is
   one decision even though it touches the style registry, the font list and the
   title renderer. */
const SECTIONS = [
  { id: "captions", name: "Captions", icon: "T",
    desc: "Styles, text and timing" },
  { id: "layout",   name: "Layout",   icon: "▦",
    desc: "Ratio, framing and safe areas" },
  { id: "brand",    name: "Brand",    icon: "◎",
    desc: "Logo, handle and overlays" },
  { id: "video",    name: "Video",    icon: "▶",
    desc: "Speed, pacing and audio" },
];

/* Mirrors pipeline/overlays.SAFE_* -- the bands Reels/TikTok/Shorts cover with
   their own buttons. Drawn as guides so nothing important lands under them. */
const SAFE = { top: 0.06, bottom: 0.12, right: 0.10 };
const MIN_CLIP = 3;
const CONTEXT = 30;

// Mirrors backend pipeline/pacing.py. The editor cannot honestly preview a
// clip with pauses removed unless it uses the same speech map the renderer uses.
const TIGHTEN_MAX_PAUSE = 0.35;
const PODCAST_TIGHTEN_MAX_PAUSE = 1.5;
const TIGHTEN_HEAD_PAD = 0.08;
const TIGHTEN_TAIL_PAD = 0.12;
const TIGHTEN_MIN_SEGMENT = 0.20;
const TIGHTEN_MIN_REMOVED = 0.4;

function fmt(t) {
  const s = Math.max(0, t);
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}.${String(Math.floor((s % 1) * 10))}`;
}

function wordsForRange(utterances, start, end, lineEdits = {}) {
  const out = [];
  for (const [i, u] of (utterances || []).entries()) {
    if (u.end <= start || u.start >= end) continue;

    const edited = lineEdits[i] !== undefined;
    if (edited) {
      const spanStart = Math.max(u.start, start);
      const spanEnd = Math.min(u.end, end);
      const toks = String(lineEdits[i] ?? u.text ?? "").split(/\s+/).filter(Boolean);
      const dt = (spanEnd - spanStart) / Math.max(toks.length, 1);
      toks.forEach((w, k) => {
        const t = spanStart + k * dt;
        out.push({ start: t, end: Math.min(spanEnd, t + dt * 0.85), text: w });
      });
      continue;
    }

    for (const w of u.words || []) {
      const ws = Number(w.t ?? w.start);
      const we = Number(w.e ?? w.end);
      const mid = (ws + we) / 2;
      if (start <= mid && mid <= end) {
        out.push({ start: ws, end: we, text: w.w ?? w.word ?? "" });
      }
    }
  }
  return out.sort((a, b) => a.start - b.start);
}

function planTightSegments(words, clipStart, clipEnd, maxPause = TIGHTEN_MAX_PAUSE) {
  if (!words.length) return [];

  const raw = [];
  let segStart = words[0].start - TIGHTEN_HEAD_PAD;
  let prevEnd = words[0].end;

  for (const word of words.slice(1)) {
    if (word.start - prevEnd > maxPause) {
      raw.push([Math.max(0, segStart), prevEnd + TIGHTEN_TAIL_PAD]);
      segStart = word.start - TIGHTEN_HEAD_PAD;
    }
    prevEnd = word.end;
  }
  raw.push([Math.max(0, segStart), prevEnd + TIGHTEN_TAIL_PAD]);

  const merged = [];
  for (const [rawStart, rawEnd] of raw) {
    const s = Math.max(rawStart, clipStart);
    const e = Math.min(rawEnd, clipEnd);
    if (e - s < TIGHTEN_MIN_SEGMENT) continue;
    if (merged.length && s <= merged[merged.length - 1][1]) {
      merged[merged.length - 1][1] = Math.max(merged[merged.length - 1][1], e);
    } else {
      merged.push([s, e]);
    }
  }
  return merged;
}

function buildTimelineMap(transcript, start, end, lineEdits, tighten,
                          maxPause = TIGHTEN_MAX_PAUSE) {
  const sourceDuration = Math.max(end - start, 0.001);
  const base = {
    active: false,
    duration: sourceDuration,
    words: wordsForRange(transcript?.utterances || [], start, end, lineEdits),
    segments: [{ start, end, outStart: 0, outEnd: sourceDuration }],
    sourceToOutput: (t) => Math.min(Math.max(t - start, 0), sourceDuration),
    outputToSource: (t) => start + Math.min(Math.max(t, 0), sourceDuration),
    nextSource: (t) => (t < start ? start : t <= end ? t : null),
  };
  if (!tighten || !transcript?.utterances?.length || !base.words.length) return base;

  const planned = planTightSegments(base.words, start, end, maxPause);
  const kept = planned.reduce((sum, [s, e]) => sum + e - s, 0);
  const removed = Math.max(0, sourceDuration - kept);
  if (!planned.length || removed < TIGHTEN_MIN_REMOVED) return base;

  let elapsed = 0;
  const segments = planned.map(([s, e]) => {
    const seg = { start: s, end: e, outStart: elapsed, outEnd: elapsed + e - s };
    elapsed += e - s;
    return seg;
  });

  function sourceToOutput(t) {
    const clamped = Math.min(Math.max(t, start), end);
    for (const seg of segments) {
      if (clamped < seg.start) return seg.outStart;
      if (clamped <= seg.end) return seg.outStart + (clamped - seg.start);
    }
    return elapsed;
  }

  function outputToSource(t) {
    const clamped = Math.min(Math.max(t, 0), elapsed);
    for (const seg of segments) {
      if (clamped <= seg.outEnd) return seg.start + Math.max(0, clamped - seg.outStart);
    }
    return segments[segments.length - 1].end;
  }

  function nextSource(t) {
    for (const seg of segments) {
      if (t < seg.start) return seg.start;
      if (t <= seg.end) return t;
    }
    return null;
  }

  return {
    active: true,
    duration: Math.max(elapsed, 0.001),
    words: base.words,
    segments,
    sourceToOutput,
    outputToSource,
    nextSource,
  };
}

/* A short, opinionated palette plus a native picker. A free colour input alone
   is a bad default here -- most people want one of a handful of colours that
   survive compression and stay legible burned over video, and nine of ten
   custom picks are a low-contrast disaster. */
const CAPTION_SWATCHES = [
  "#ffffff", "#101010", "#d8f24e", "#ffd900", "#ff9000", "#f04040",
  "#3ad43a", "#60d0f0", "#d060e0", "#e8f0f5",
];

function ColourRow({ value, fallback, onChange, testid }) {
  const current = value || fallback;
  return (
    <span className="colour-row" data-testid={testid}>
      {CAPTION_SWATCHES.map((c) => (
        <button key={c} type="button"
                className={`swatch ${current?.toLowerCase() === c ? "on" : ""}`}
                style={{ background: c }}
                onClick={() => onChange(c)}
                title={c} aria-label={`Use ${c}`} />
      ))}
      <span className="swatch swatch-custom" title="Custom colour">
        <input type="color" value={current || "#ffffff"}
               onChange={(e) => onChange(e.target.value)}
               aria-label="Custom colour" />
      </span>
      {value && (
        <button type="button" className="colour-reset" onClick={() => onChange(null)}>
          Reset
        </button>
      )}
    </span>
  );
}

/**
 * Non-destructive clip editor.
 *
 * Plays this clip's PROXY (a small copy of the source WINDOW the trim handles
 * can reach) and draws captions as DOM on top. Trimming, restyling, changing
 * ratio or font are pure state changes -- instant, free, and reversible. An MP4
 * is only rendered when the user exports, which is the one operation that costs
 * real time.
 */
export default function LiveEditor({
  job, clip, index, onClose, onSaved, onUpgrade, canEditCaptions = false,
  canTightenPauses = false, canRemoveWatermark = false,
}) {
  const [transcript, setTranscript] = useState(null);
  const [loadError, setLoadError] = useState("");
  /* The proxy builds in parallel with the clips, so the editor can open before
     it exists. Showing a black video in that window looked broken -- especially
     in split-screen, where only the top half went black. */
  const [proxyReady, setProxyReady] = useState(clip.proxy_ready !== false);

  /* The proxy covers [clip.start - CONTEXT, clip.end + CONTEXT], not the whole
     source, so its t=0 is source time `pOff`. Every time this component holds
     is a SOURCE time -- transcript timings, trim points, the playhead -- and
     only the two crossings into and out of a <video> element convert. Keeping
     the conversion at that boundary is what stops the offset leaking into the
     caption timing, where it would silently desync every cue. */
  const pOff = clip.proxy_offset || 0;
  const toProxy = (t) => Math.max(0, t - pOff);
  const fromProxy = (t) => t + pOff;
  const proxySrc = previewUrl(job.id, index);

  /* Which group of controls the inspector is showing. Deliberately NOT in the
     undo document: undoing your way back through a change should not also drag
     you to a different panel. */
  const [section, setSection] = useState("captions");

  const initial = clip.edit || {};

  /* Everything that describes the exported clip lives in ONE object behind an
     undo/redo stack. Keeping it together is what makes undo total: a control
     added later is covered without touching history logic. Transient UI state
     (what is playing, what is selected) deliberately stays out -- undoing a
     selection would be baffling. */
  const initialDoc = useMemo(() => ({
    start: initial.start ?? clip.start,
    end: initial.end ?? clip.end,
    ratio: initial.ratio || job.options?.ratio || "9:16",
    template: initial.template || job.options?.template || "classic",
    capStyle: initial.caption_style || job.options?.caption_style || "classic",
    capFont: initial.caption_font ?? null,
    capSize: initial.caption_size ?? null,       // null = the style's own size
    capPos: initial.caption_pos ?? null,         // null = automatic placement
    capAnim: initial.caption_anim || "none",
    /* How many words share a caption cue. null keeps the preset's own cadence;
       1 is the one-word-at-a-time look, 3 the reference's chunk size. */
    capWords: initial.words_per_cue ?? job.options?.words_per_cue ?? null,
    /* Punch-in on key words. An editorial look, not a correction, so it is its
       own remembered choice and rides the undo stack with everything else. */
    autoZoom: initial.auto_zoom ?? (job.options?.auto_zoom === true),
    autoTransitions: initial.auto_transitions ??
      (job.options?.auto_transitions === true),
    autoSfx: initial.auto_sfx ?? (job.options?.auto_sfx === true),
    musicBed: initial.music_bed ?? (job.options?.music_bed === true),
    autoBroll: initial.auto_broll ?? (job.options?.auto_broll !== false),
    /* Colour overrides on top of the preset. Every preset ships white text, so
       picking a "style" only ever changed how the spoken word was marked --
       this is the axis people reach for first. null = keep the preset's own. */
    capColor: initial.caption_color ?? null,
    capActive: initial.caption_active_color ?? null,
    /* Captions off is a real editing decision, not an absence: plenty of clips
       are posted with the platform's own captions or none at all, and until now
       the only way to get one was to re-run the whole job. */
    capOn: initial.captions_on ?? (job.options?.burn_subtitles !== false),
    /* Emoji is its own switch because "the captions are right but the emoji is
       not my channel" is a real, common edit. It is remembered per clip, so a
       re-export keeps the choice instead of silently reintroducing them. */
    autoEmoji: initial.auto_emoji ?? (job.options?.auto_emoji !== false),
    translateTo: initial.translate_to ?? null,
    title: clip.title || "",
    titleStyle: initial.title_style || (job.options?.burn_title === false ? "none" : "plate"),
    titleFont: initial.title_font ?? null,
    titleSize: initial.title_size ?? null,
    speed: initial.speed ?? 1,
    speedPitched: Boolean(initial.speed_pitched),
    tightenPauses: Boolean(canTightenPauses &&
      (initial.tighten_pauses ?? (job.options?.tighten_pauses === true))),
    background: initial.background || "black",
    gameplayFile: initial.gameplay_file ?? null,
    overlayList: initial.overlays || [],
    lineEdits: {},
  }), []);   // eslint-disable-line react-hooks/exhaustive-deps

  const history = useEditHistory(initialDoc);
  const doc = history.state;
  const {
    start, end, ratio, template, capStyle, capFont, capSize, capPos, capAnim,
    capWords, capColor, capActive, capOn, autoEmoji, autoZoom, autoTransitions,
    autoSfx, musicBed, autoBroll,
    translateTo, title, titleStyle, titleFont, titleSize, speed, speedPitched, tightenPauses,
    background, gameplayFile,
    overlayList, lineEdits,
  } = doc;

  /* The renderer's own preset registry, fetched once. `st` is the definition of
     whatever is currently selected, and every piece of the on-video preview
     below reads from it -- so preview and export cannot disagree about a colour
     or a size without the server being wrong about its own output. */
  const presetData = useCaptionPresets();
  const st = useMemo(() => {
    const base = presetData?.presets?.find((p) => p.id === capStyle) || FALLBACK_STYLE;
    if (!capColor && !capActive) return base;
    return {
      ...base,
      color: capColor || base.color,
      // The keyword colour is an orange designed against white text; on a
      // recoloured base it reads as a second highlight nobody asked for. The
      // renderer makes the same substitution (subtitles._recolour).
      keyword: capColor || base.keyword,
      // ...and keeps one keyword colour rather than the rotating palette.
      keywordColors: capColor ? null : base.keywordColors,
      active: capActive || base.active,
    };
  }, [presetData, capStyle, capColor, capActive]);

  // Setters keep the rest of this component unchanged. `discrete` marks
  // click-like actions so undo steps over them one at a time, while drags
  // coalesce into a single step.
  const set = (key) => (v, opts) => history.apply({ [key]: v }, opts);
  const setStart = set("start");
  const setEnd = set("end");
  const setRatio = (v) => history.apply({ ratio: v }, { discrete: true });
  const setTemplate = (v) => history.apply({ template: v }, { discrete: true });
  const setCapStyle = (v) => history.apply({ capStyle: v }, { discrete: true });
  const setCapFont = (v) => history.apply({ capFont: v }, { discrete: true });
  const setCapSize = (v) => history.apply({ capSize: v });
  const setCapPos = set("capPos");
  const setCapAnim = (v) => history.apply({ capAnim: v }, { discrete: true });
  const setCapWords = (v) => history.apply({ capWords: v }, { discrete: true });
  const setAutoZoom = (v) => history.apply({ autoZoom: v }, { discrete: true });
  const setAutoTransitions = (v) =>
    history.apply({ autoTransitions: v }, { discrete: true });
  const setAutoSfx = (v) => history.apply({ autoSfx: v }, { discrete: true });
  const setMusicBed = (v) => history.apply({ musicBed: v }, { discrete: true });
  const setAutoBroll = (v) => history.apply({ autoBroll: v }, { discrete: true });
  const setCapColor = (v) => history.apply({ capColor: v }, { discrete: true });
  const setCapActive = (v) => history.apply({ capActive: v }, { discrete: true });
  const setCapOn = (v) => history.apply({ capOn: v }, { discrete: true });
  const setAutoEmoji = (v) => history.apply({ autoEmoji: v }, { discrete: true });
  const setTranslateTo = (v) => history.apply({ translateTo: v }, { discrete: true });
  const setTitle = (v) => history.apply({ title: v });
  const setTitleStyle = (v) => history.apply({ titleStyle: v }, { discrete: true });
  const setTitleFont = (v) => history.apply({ titleFont: v }, { discrete: true });
  const setTitleSize = (v) => history.apply({ titleSize: v }, { discrete: true });
  const setSpeed = (v) => history.apply({ speed: v }, { discrete: true });
  const setSpeedPitched = (v) => history.apply({ speedPitched: v }, { discrete: true });
  const setTightenPauses = (v) =>
    history.apply({ tightenPauses: v }, { discrete: true });
  const setBackground = (v) => history.apply({ background: v }, { discrete: true });
  const setGameplayFile = (v) => history.apply({ gameplayFile: v }, { discrete: true });
  const setLineEdits = (v, opts = { discrete: true }) =>
    history.apply((p) => ({ ...p, lineEdits: typeof v === "function" ? v(p.lineEdits) : v }),
                  opts);
  const setOverlayList = (v, opts) =>
    history.apply((p) => ({ ...p, overlayList: typeof v === "function" ? v(p.overlayList) : v }),
                  opts);

  /* Whether this clip's captions contain Devanagari. Drives the font picker's
     warning, because a face with no Hindi glyphs renders empty boxes. */
  const hasDevanagari = useMemo(
    () => /[\u0900-\u097F]/.test(
      (transcript?.utterances || []).map((u) => u.text || "").join(" ")),
    [transcript]);

  /* Server fonts, so the preview can render in any installed face. */
  const fontList = useFonts();
  const fontCss = (id) =>
    fontList.find((f) => f.id === id)?.css || FONT_FALLBACK;

  const titleLooks = useTitleStyles();
  const titleLook = titleLooks.find((l) => l.id === titleStyle) || titleLooks[0];

  const [selected, setSelected] = useState(null);      // index into overlayList
  const [showSafe, setShowSafe] = useState(true);
  const [dragOverlay, setDragOverlay] = useState(null);
  const [dragCaption, setDragCaption] = useState(false);

  /* Freeze the page behind the editor for as long as it is open.
     The editor is `position: fixed`, which should make this unnecessary -- but
     the marketing page sets `overflow: clip` on <body>, and an element with
     `overflow: clip` is a containing block for fixed-position descendants. So
     the editor was positioned against the BODY, not the viewport, and scrolling
     the page underneath dragged the whole workspace up off the screen with it.
     Locking the scroll fixes the symptom and is what a full-screen workspace
     wants regardless: there is nothing behind it worth scrolling to. */
  useEffect(() => {
    const html = document.documentElement;
    const prev = html.style.overflow;
    const y = window.scrollY;
    html.style.overflow = "hidden";
    return () => {
      html.style.overflow = prev;
      window.scrollTo(0, y);          // put them back where they were
    };
  }, []);

  /* Selecting an overlay -- on the video or on the layer strip -- takes you to
     the section that can edit it. Those two surfaces live outside the inspector
     and are visible from every section, so without this you could click a
     handle from Captions, see it highlight, and find no controls anywhere. */
  function selectOverlay(i) {
    setSelected(i);
    if (i !== null) setSection("brand");
  }

  /* Templates that composite something on top of the source have to be shown
     composited, or the editor is previewing a different video than it exports.
     Split-screen is the loud case: half the frame is gameplay. */
  const TEMPLATE_FRAMES = { classic: "blur", fitvideo: "fit", gameplay: "gameplay",
                            podcast: "podcast", auto: "auto" };
  const activeTemplate = template || job.options?.template || "classic";
  const frame = TEMPLATE_FRAMES[activeTemplate]
    || job.options?.frame
    || (job.options?.template === "gameplay" ? "gameplay" : null);
  const isSplit = activeTemplate === "gameplay";
  /* render.render_clip defaults to "blur" when no frame is set, so the preview
     has to default the same way or it previews a mode nobody exports. */
  const frameMode = isSplit ? "gameplay" : (frame || "blur");

  const [now, setNow] = useState(initial.start ?? clip.start);
  const [playing, setPlaying] = useState(false);
  const [dragging, setDragging] = useState(null);
  const [exporting, setExporting] = useState(false);
  const [exported, setExported] = useState(false);
  const toast = useToast();
  const [exportError, setExportError] = useState("");
  const [status, setStatus] = useState("");

  const [gameplayList, setGameplayList] = useState([]);
  useEffect(() => {
    if (isSplit) getGameplayList().then(setGameplayList).catch(() => {});
  }, [isSplit]);

  const videoRef = useRef(null);
  const gameplayRef = useRef(null);
  const blurRef = useRef(null);
  const podcastTileRefs = useRef([]);
  const trackRef = useRef(null);
  const saveTimer = useRef(null);

  /* Real source dimensions, read off the proxy once it has metadata. The
     renderer's zoom policy is a function of them, so the preview cannot mirror
     the framing until they are known. */
  const [srcDims, setSrcDims] = useState(null);

  useEffect(() => {
    getTranscript(job.id).then(setTranscript).catch((e) => setLoadError(e.message));
  }, [job.id]);

  /* Speed is applied to the PREVIEW, not just the export. Hearing 1.5x before
     paying for a render is the entire point of an editor -- and preservesPitch
     mirrors the renderer: off means pitch rides with speed (the meme effect),
     on means atempo-style natural pitch. */
  useEffect(() => {
    for (const el of [videoRef.current, gameplayRef.current, ...podcastTileRefs.current]) {
      if (!el) continue;
      el.playbackRate = speed || 1;
      el.preservesPitch = !speedPitched;
      el.mozPreservesPitch = !speedPitched;
      el.webkitPreservesPitch = !speedPitched;
    }
  }, [speed, speedPitched, transcript]);

  function onMeta(e) {
    e.target.currentTime = toProxy(clipMap.outputToSource(0));
    setSrcDims({ w: e.target.videoWidth, h: e.target.videoHeight });
  }

  /* Share of the frame height the sharp layer occupies, matching the
     renderer's zoom policy exactly. */
  const [outW, outH] = ratio === "1:1" ? [1080, 1080]
    : ratio === "16:9" ? [1920, 1080] : [1080, 1920];
  /* Mirrors subtitles._fit_canvas: presets are designed on the 1080x1920
     canvas, so a square or landscape frame scales caption sizes down. */
  const canvasScale = outW > outH * 0.7 ? Math.min(1, Math.pow(outH / 1920, 0.6)) : 1;
  /* `fit` takes the same zoom as `blur` now -- the two differ in what fills the
     leftover height (flat colour vs a blurred copy), not in how big the picture
     is. It used to be the letterboxed height with no zoom at all. */
  const fgHeight = frameMode === "blur" || frameMode === "fit"
    ? contentHeightFrac(srcDims?.w, srcDims?.h, outW, outH)
    /* Auto falls back to `fit` when the planner finds nothing to frame, so
       without a plan it previews exactly that. */
    : frameMode === "auto"
    ? Math.min(contentHeightFrac(srcDims?.w, srcDims?.h, outW, outH), 1)
    /* Podcast never zooms -- cropping a two-shot removes a speaker -- so the
       block is exactly the letterboxed height, sat high. */
    : frameMode === "podcast"
    ? Math.min((outW * (srcDims?.h || 9)) / ((srcDims?.w || 16) * outH), 1)
    : 1;

  /* Top edge of the video block as a fraction of frame height, for the frames
     that sit their block off-centre. Mirrors the clamp in fit_filter and
     podcast_filter: centre the block on `bias`, then keep it inside the frame. */
  function blockTop(bias) {
    return Math.min(Math.max(bias - fgHeight / 2, 0), 1 - fgHeight);
  }

  /* The face-aware podcast plan, fetched once per clip range.
     Without this the editor composed the OLD letterbox and showed a layout the
     clip was never in -- the single worst kind of preview, because it looks
     deliberate. Null for every other template, and null when detection found
     nothing, which is exactly when the renderer falls back to the letterbox
     too, so the two stay in agreement either way. */
  const [reframePlan, setReframePlan] = useState(null);
  useEffect(() => {
    if ((frameMode !== "podcast" && frameMode !== "auto") || !job?.id) {
      setReframePlan(null); return;
    }
    let live = true;
    getClipReframe(job.id, index, null, null, activeTemplate, autoZoom, autoBroll)
      .then((d) => { if (live) setReframePlan(d.plan || null); })
      .catch(() => { if (live) setReframePlan(null); });
    return () => { live = false; };
  }, [job?.id, index, frameMode, activeTemplate, autoZoom, autoBroll]);

  /* One crop window as CSS, mirroring smart_filter's crop+scale.
     The renderer maps a `win` box from the source onto the whole tile; the same
     mapping in DOM is a video blown up by (source / window) and shifted so the
     window's centre lands at the tile's centre. Percentages are of the TILE, so
     this works for a full frame and for a stacked half without special-casing. */
  function cropStyle(winW, winH, cx, cy, sw, sh) {
    return {
      position: "absolute",
      width: `${(sw / winW) * 100}%`,
      height: `${(sh / winH) * 100}%`,
      left: `${50 - (cx / winW) * 100}%`,
      top: `${50 - (cy / winH) * 100}%`,
      objectFit: "fill",
      maxWidth: "none",
    };
  }

  /* Mirrors render's `fit` tiles: keep the crop's own aspect inside the tile
     rather than stretching it to fill. Used by the gaming stack's webcam band,
     where cropping to the tile's aspect would be a forehead close-up. */
  function fitStyle(winW, winH, cx, cy, sw, sh, rectW, rectH) {
    const tileW = outW * rectW;
    const tileH = outH * rectH;
    const scale = Math.min(tileW / winW, tileH / winH);
    return {
      position: "absolute",
      width: `${((sw * scale) / tileW) * 100}%`,
      height: `${((sh * scale) / tileH) * 100}%`,
      left: `${50 - ((cx * scale) / tileW) * 100}%`,
      top: `${50 - ((cy * scale) / tileH) * 100}%`,
      objectFit: "fill",
      maxWidth: "none",
    };
  }

  /* Crop centre at the current playhead. The plan is keyframed in clip-relative
     seconds, the same timebase the renderer's expressions use; holding the last
     key past the end matches how those expressions behave too. */
  function windowAt(keys, t) {
    if (!keys?.length) return null;
    return { cx: easedAt(keys, 1, t), cy: easedAt(keys, 2, t) };
  }

  /* The scale factor comes from the PLAN's own source dimensions, never from
     the <video> element. The proxy is a 640x360 downscale, while `win` is a box
     in full-resolution source pixels -- dividing one by the other produced a
     crop three times too small and a video that filled a third of its tile.
     The plan carries `src` precisely so the two numbers come from one place. */
  const smartFrame = (frameMode === "podcast" || frameMode === "auto")
    && reframePlan?.src ? reframePlan : null;
  const [planW, planH] = smartFrame ? smartFrame.src : [0, 0];
  /* `now` is a source timestamp; the plan's keys were rebased against the range
     it was COMPUTED for, which is the clip's own bounds. So the offset is
     clip.start -- not the trim start, which moves as the handles move and would
     slide the preview onto the wrong keyframe the moment anyone trimmed. */
  const smartT = Math.max(0, now - clip.start);
  const smartScene = smartFrame?.mode === "adaptive"
    ? (smartFrame.scenes || []).find((scene, i, all) => (
        smartT >= scene.start && (smartT < scene.end || i === all.length - 1)
      ))
    : null;
  const smartTiles = smartScene
    ? (smartScene.tiles || [])
    : smartFrame?.windows?.map((keys) => ({
        rect: smartFrame.mode === "stacked"
          ? [0, keys === smartFrame.windows[0] ? 0 : 0.5, 1, 0.5]
          : [0, 0, 1, 1],
        win: smartFrame.win,
        keys,
      })) || [];
  /* The caption baseline this scene wants, from the plan's own per-scene
     placement. Only used when the user has not dragged captions by hand. */
  const sceneCapPos = capPos === null
    ? (smartFrame?.captions || []).find((c, i, all) => (
        smartT >= c.start && (smartT < c.end || i === all.length - 1)
      ))?.pos ?? null
    : null;

  const visibleSmartTiles = smartTiles
    .map((tile) => ({ ...tile, point: windowAt(tile.keys, smartT) }))
    .filter((tile) => tile.point);

  /* The blurred backdrop is a second decode of the same file, so it has to be
     told where the playhead is. Exact frame-lock is unnecessary -- it is
     blurred at sigma 12 in the export and reads as wallpaper either way. */
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const peers = [blurRef.current, ...podcastTileRefs.current]
      .filter((el, i, all) => el && el !== v && all.indexOf(el) === i);
    for (const peer of peers) {
      if (Math.abs(peer.currentTime - v.currentTime) > 0.25) peer.currentTime = v.currentTime;
      peer.playbackRate = speed || 1;
      if (playing) peer.play().catch(() => {}); else peer.pause();
    }
  }, [now, playing, speed]);

  useEffect(() => {
    if (proxyReady) return;
    const t = setInterval(async () => {
      try {
        const r = await fetch(proxySrc, { method: "HEAD" });
        if (r.ok) { setProxyReady(true); clearInterval(t); }
      } catch { /* keep waiting */ }
    }, 1500);
    return () => clearInterval(t);
  }, [proxyReady, proxySrc]);

  const view = useMemo(() => {
    // `||`, not `??`: a source with no speech can report 0, which is not a length.
    const total = transcript?.duration || clip.end + CONTEXT;
    return { from: Math.max(0, clip.start - CONTEXT), to: Math.min(total, clip.end + CONTEXT) };
  }, [transcript, clip.start, clip.end]);

  const tightenMaxPause = job.options?.template === "podcast"
    ? PODCAST_TIGHTEN_MAX_PAUSE
    : job.options?.template === "auto"
    ? (PODCAST_TIGHTEN_MAX_PAUSE + TIGHTEN_MAX_PAUSE) / 2
    : TIGHTEN_MAX_PAUSE;
  const viewMap = useMemo(
    () => buildTimelineMap(
      transcript, view.from, view.to, lineEdits, tightenPauses, tightenMaxPause),
    [transcript, view.from, view.to, lineEdits, tightenPauses, tightenMaxPause],
  );
  const clipMap = useMemo(
    () => buildTimelineMap(
      transcript, start, end, lineEdits, tightenPauses, tightenMaxPause),
    [transcript, start, end, lineEdits, tightenPauses, tightenMaxPause],
  );
  const tightClipMap = useMemo(
    () => buildTimelineMap(
      transcript, start, end, lineEdits, true, tightenMaxPause),
    [transcript, start, end, lineEdits, tightenMaxPause],
  );
  const tightenedSeconds = tightClipMap.active
    ? Math.max(0, (end - start) - tightClipMap.duration)
    : 0;
  const tighteningAvailable = canTightenPauses && tightClipMap.active;
  const span = Math.max(viewMap.duration, 0.001);
  const pct = (t) => (viewMap.sourceToOutput(t) / span) * 100;
  const clipNow = clipMap.sourceToOutput(now);
  const clipDuration = Math.max(clipMap.duration, 0.1);

  /* Playback is confined to the tightened clip without touching the file:
     the proxy still contains source video, so the player jumps over the same
     dead-air gaps ffmpeg will remove during export. */
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    function onTime() {
      const sourceTime = fromProxy(v.currentTime);
      const next = clipMap.nextSource(sourceTime);
      if (sourceTime >= end || next === null) {
        v.currentTime = toProxy(clipMap.outputToSource(0));
        if (!playing) v.pause();
        setNow(clipMap.outputToSource(0));
        return;
      }
      if (Math.abs(next - sourceTime) > 0.03) {
        v.currentTime = toProxy(next);
        setNow(next);
        return;
      }
      setNow(sourceTime);
    }
    v.addEventListener("timeupdate", onTime);
    return () => v.removeEventListener("timeupdate", onTime);
  }, [end, playing, clipMap]);

  // Persist the recipe, debounced. Cheap (no render), so it can be automatic.
  useEffect(() => {
    if (!transcript) return;
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      const lines = (transcript.utterances || [])
        .map((u, i) => ({ ...u, i }))
        .filter((u) => u.end > start && u.start < end)
        .map((u) => ({
          start: Math.max(u.start, start),
          end: Math.min(u.end, end),
          text: lineEdits[u.i] ?? u.text,
          // Only rewritten lines give up their real per-word timings. Sending
          // every line as "edited" made one typo fix re-time the whole clip.
          edited: lineEdits[u.i] !== undefined,
        }));
      saveClipEdit(job.id, index, {
        start, end, ratio,
        template,
        caption_style: capStyle,
        caption_font: capFont,
        caption_size: capSize,
        caption_pos: capPos,
        caption_color: capColor,
        caption_active_color: capActive,
        captions_on: capOn,
        auto_emoji: autoEmoji,
        words_per_cue: capWords,
        auto_zoom: autoZoom,
        auto_transitions: autoTransitions,
        auto_sfx: autoSfx,
        music_bed: musicBed,
        auto_broll: autoBroll,
        translate_to: translateTo,
        title: title.trim() || null,
        title_style: titleStyle,
        title_font: titleFont,
        title_size: titleSize,
        caption_anim: capAnim,
        speed,
        speed_pitched: speedPitched,
        tighten_pauses: tightenPauses,
        background,
        gameplay_file: gameplayFile,
        lines: Object.keys(lineEdits).length ? lines : null,
        overlays: overlayList.length ? overlayList : null,
      })
        .then(() => setStatus("Saved"))
        .catch((e) => setStatus(e.message));
    }, 400);
    return () => clearTimeout(saveTimer.current);
  }, [doc, transcript, job.id, index]);

  // Captions drag vertically only -- they are a full-width block, so horizontal
  // freedom would just let people misalign them.
  useEffect(() => {
    if (!dragCaption) return;
    function onMove(e) {
      const stage = document.querySelector(".stage-frame");
      if (!stage) return;
      const r = stage.getBoundingClientRect();
      setCapPos(Math.min(Math.max(((e.clientY ?? 0) - r.top) / r.height, 0.05), 0.95));
    }
    const stop = () => setDragCaption(false);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", stop);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", stop);
    };
  }, [dragCaption]);

  /* Dragging an overlay writes back FRACTIONS of the stage, which is what the
     renderer consumes -- so what you drag is literally what gets burned in,
     at any output ratio. */
  useEffect(() => {
    if (dragOverlay === null) return;
    function onMove(e) {
      const stage = document.querySelector(".stage-frame");
      if (!stage) return;
      const r = stage.getBoundingClientRect();
      const x = Math.min(Math.max(((e.clientX ?? 0) - r.left) / r.width, 0), 1);
      const y = Math.min(Math.max(((e.clientY ?? 0) - r.top) / r.height, 0), 1);
      setOverlayList((list) =>
        list.map((o, i) => (i === dragOverlay ? { ...o, x, y } : o)));
    }
    const stop = () => setDragOverlay(null);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", stop);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", stop);
    };
  }, [dragOverlay]);

  function addText() {
    setOverlayList((l) => [...l, {
      type: "text", text: "@yourhandle",
      x: 0.5, y: 0.9, size: 0.045, color: "#ffffff", font: "anton", opacity: 1,
    }]);
    selectOverlay(overlayList.length);
  }

  async function addLogo(file) {
    if (!file) return;
    setStatus("Uploading logo…");
    try {
      const { file: name } = await uploadOverlayImage(job.id, file);
      setOverlayList((l) => [...l, {
        type: "image", file: name, x: 0.86, y: 0.09, size: 0.12, opacity: 1,
      }]);
      selectOverlay(overlayList.length);
      setStatus("Logo added");
    } catch (e) { setStatus(e.message); }
  }

  const patchOverlay = (i, patch) =>
    setOverlayList((l) => l.map((o, k) => (k === i ? { ...o, ...patch } : o)));
  const removeOverlay = (i) => {
    setOverlayList((l) => l.filter((_, k) => k !== i));
    setSelected(null);
  };

  function updateLineEdit(i, text, original) {
    setLineEdits((prev) => {
      const next = { ...prev };
      const cleaned = text.trim();
      if (!cleaned || cleaned === original) delete next[i];
      else next[i] = text;
      return next;
    }, { discrete: false });
  }

  useEffect(() => {
    if (!dragging) return;
    function onMove(e) {
      const rect = trackRef.current.getBoundingClientRect();
      const x = e.clientX ?? e.touches?.[0]?.clientX;
      const outT = Math.min(Math.max((x - rect.left) / rect.width, 0), 1) * span;
      const t = viewMap.outputToSource(outT);
      if (dragging === "start") {
        const v = Math.min(t, end - MIN_CLIP);
        setStart(v);
        if (videoRef.current) videoRef.current.currentTime = toProxy(v);
      } else if (dragging === "end") {
        setEnd(Math.max(t, start + MIN_CLIP));
      } else {
        // Scrubbing the playhead. Clamped to the trim window, because time
        // outside it is not part of the clip and cannot be previewed.
        seek(Math.min(Math.max(t, start), end));
      }
    }
    const stop = () => setDragging(null);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", stop);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", stop);
    };
  }, [dragging, start, end, span, viewMap]);

  /* Keyword colour slots for the whole clip, numbered by first appearance --
     the renderer numbers them across the clip, not per cue. */
  const kwSlots = useMemo(() => {
    const slots = new Map();
    if (!transcript?.utterances) return slots;
    const tokens = [];
    transcript.utterances.forEach((u, i) => {
      if (u.end <= start || u.start >= end) return;
      if (lineEdits[i] !== undefined) {
        tokens.push(...lineEdits[i].split(/\s+/).filter(Boolean));
      } else {
        tokens.push(...(u.words || []).filter((w) => w.e > start && w.t < end).map((w) => w.w));
      }
    });
    keywordRuns(tokens, keywordHits(tokens, clip.keywords)).forEach(({ key }) => {
      if (!slots.has(key)) slots.set(key, slots.size);
    });
    return slots;
  }, [transcript, start, end, lineEdits, clip.keywords]);

  /* The cue under the playhead, chunked exactly as the renderer will chunk it. */
  const cue = useMemo(() => {
    if (!transcript?.utterances) return null;
    // The preset decides how many words share a cue -- that is most of what
    // makes a Hormozi look a Hormozi look -- so it overrides the server's
    // generic rule rather than the other way round.
    const rules = transcript.cue_rules || { max_words: 4, max_seconds: 1.8 };

    const u = transcript.utterances.find((x) => now >= x.start && now <= x.end);
    if (!u) return null;
    const i = transcript.utterances.indexOf(u);

    // A rewritten line has no per-word timings any more, so spread its tokens
    // evenly -- the same compromise the backend makes for edited lines.
    let words = u.words;
    if (lineEdits[i] !== undefined || !words?.length) {
      const text = lineEdits[i] ?? u.text;
      const toks = text.split(/\s+/).filter(Boolean);
      const dt = (u.end - u.start) / Math.max(toks.length, 1);
      words = toks.map((w, k) => ({ t: u.start + k * dt, e: u.start + (k + 1) * dt, w }));
    }

    const cues = groupWords(words, capWords || st.maxWords || rules.max_words,
                            rules.max_seconds);
    const active = cues.find((c) => now >= c[0].t && now <= c[c.length - 1].e)
      || cues.find((c) => now < c[0].t)
      || cues[cues.length - 1];
    if (!active) return null;

    let activeIndex = active.findIndex((w) => now >= w.t && now <= w.e);
    if (activeIndex < 0) activeIndex = now > active[active.length - 1].e ? active.length - 1 : 0;
    const tokens = active.map((w) => w.w);
    const kw = new Map();
    keywordRuns(tokens, keywordHits(tokens, clip.keywords)).forEach(({ idx, key }) => {
      const slot = kwSlots.get(key) ?? 0;
      idx.forEach((i) => kw.set(i, slot));
    });
    return { words: tokens, activeIndex, kw };
  }, [transcript, now, lineEdits, st, capWords, clip.keywords, kwSlots]);

  /* One place that moves the playhead, so scrubbing from the trim bar, the
     layer strip or the transcript all behave identically -- including keeping
     the blurred backdrop in step. */
  function seek(t) {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = toProxy(t);
    setNow(t);
    if (blurRef.current) blurRef.current.currentTime = toProxy(t);
    for (const peer of podcastTileRefs.current) {
      if (peer) peer.currentTime = toProxy(t);
    }
  }

  function seekClipTime(t) {
    seek(clipMap.outputToSource(t));
  }

  function togglePlay() {
    const v = videoRef.current;
    if (!v) return;
    const g = gameplayRef.current;
    if (v.paused) {
      const at = fromProxy(v.currentTime);
      if (at < start || at >= end || clipMap.nextSource(at) === null) {
        v.currentTime = toProxy(clipMap.outputToSource(0));
        setNow(clipMap.outputToSource(0));
      }
      v.play(); g?.play().catch(() => {});
      for (const peer of podcastTileRefs.current) peer?.play().catch(() => {});
      setPlaying(true);
    } else {
      v.pause(); g?.pause();
      for (const peer of podcastTileRefs.current) peer?.pause();
      setPlaying(false);
    }
  }

  /* Keyboard shortcuts. An editor you have to mouse to for every play/pause is
     an editor nobody scrubs carefully in -- and scrubbing carefully is the
     whole job here. Text fields are excluded so typing a caption still types. */
  useEffect(() => {
    function onKey(e) {
      const el = document.activeElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA"
                 || el.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      const step = e.shiftKey ? 2 : 0.5;
      if (e.code === "Space") {
        e.preventDefault();
        togglePlay();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        seek(Math.max(start, now - step));
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        seek(Math.min(end, now + step));
      } else if (e.key.toLowerCase() === "c") {
        if (canEditCaptions) setCapOn(!capOn);
        else onUpgrade?.("caption_editing");
      } else if (e.key === "Escape") {
        onClose();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [now, start, end, capOn, playing, clipMap, canEditCaptions, onUpgrade]);

  async function doExport() {
    setExporting(true);
    setStatus("");
    setExportError("");
    try {
      // Flush the debounced save so the recipe on the server matches what
      // the editor shows right now. Without this, a style change followed
      // by a quick Export could render the OLD recipe.
      clearTimeout(saveTimer.current);
      const lines = (transcript.utterances || [])
        .map((u, i) => ({ ...u, i }))
        .filter((u) => u.end > start && u.start < end)
        .map((u) => ({
          start: Math.max(u.start, start),
          end: Math.min(u.end, end),
          text: lineEdits[u.i] ?? u.text,
          edited: lineEdits[u.i] !== undefined,
        }));
      await saveClipEdit(job.id, index, {
        start, end, ratio,
        template,
        caption_style: capStyle,
        caption_font: capFont,
        caption_size: capSize,
        caption_pos: capPos,
        caption_color: capColor,
        caption_active_color: capActive,
        captions_on: capOn,
        auto_emoji: autoEmoji,
        words_per_cue: capWords,
        auto_zoom: autoZoom,
        auto_transitions: autoTransitions,
        auto_sfx: autoSfx,
        music_bed: musicBed,
        auto_broll: autoBroll,
        translate_to: translateTo,
        title: title.trim() || null,
        title_style: titleStyle,
        title_font: titleFont,
        title_size: titleSize,
        caption_anim: capAnim,
        speed,
        speed_pitched: speedPitched,
        tighten_pauses: tightenPauses,
        background,
        gameplay_file: gameplayFile,
        lines: Object.keys(lineEdits).length ? lines : null,
        overlays: overlayList.length ? overlayList : null,
      });
      const updated = await exportClip(job.id, index);
      if (isPostHogConfigured) {
        posthog.capture("clip_exported", {
          ratio,
          template,
          captions_enabled: capOn,
          overlays_added: overlayList.length > 0,
          tighten_pauses: tightenPauses,
        });
      }
      onSaved(index, updated);
      // Rendering is the slow, expensive step -- finishing it deserves more
      // than a word of grey text. Confirm it, then return to the clips, which
      // is where someone goes next anyway (to watch or download it).
      setExported(true);
      toast("Clip exported", {
        detail: "Your edit is baked into the file and ready to download.",
        tone: "success" });
    } catch (e) {
      if (e.status === 402) {
        onUpgrade?.();
      } else if (e.status === 503) {
        /* The render slots are full, which is a queue, not a failure. Saying
           "we couldn't export that edit" would read as though the edit were at
           fault and send someone hunting for what they did wrong. Nothing is
           lost here -- the recipe is saved, and the same click works shortly. */
        setExportError(e.message);
        toast("Still busy — try again in a moment", {
          detail: e.message, tone: "info" });
      } else {
        setExportError(e.message);
        toast("We couldn't export that edit", { detail: e.message, tone: "error" });
      }
    } finally {
      setExporting(false);
    }
  }

  const duration = clipDuration;

  return createPortal(
    <div className="live-page">
      <div className="live">
        {exporting && (
          <div className="export-veil">
            <div className="export-card">
              <span className="export-spinner" aria-hidden="true" />
              <strong>Rendering your clip</strong>
              <p>Burning in captions and overlays at full quality. This is the
                 only step that takes real time.</p>
            </div>
          </div>
        )}

        {exported && (
          /* No click-to-dismiss on a confirmation: the backdrop used to call
             onClose, so a stray click on the panel someone was reading closed
             the whole editor. The two explicit choices are right there. */
          <div className="export-veil" data-testid="export-done">
            <div className="export-card done">
              <span className="export-tick" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="26" height="26" fill="none">
                  <path d="m5 13 4 4L19 7" stroke="currentColor" strokeWidth="2.6"
                        strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </span>
              <strong>Clip exported</strong>
              <p>Your changes are baked into the file and ready to download.</p>
              <div className="export-actions">
                <button className="btn btn-ghost btn-sm"
                        onClick={() => setExported(false)}>
                  Keep editing
                </button>
                <button className="btn btn-primary btn-sm" onClick={onClose}>
                  Back to clips
                </button>
              </div>
            </div>
          </div>
        )}

        <header className="live-head">
          <button className="live-back" onClick={onClose}>
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" aria-hidden="true">
              <path d="M15 5l-7 7 7 7" stroke="currentColor" strokeWidth="2.2"
                    strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Back to clips
          </button>
          <div className="live-head-mid">
            <input
              className="title-input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Untitled clip"
              maxLength={120}
              aria-label="Clip title"
            />
            <p className="editor-sub">
              Burned into the video · also the download filename
            </p>
          </div>
          <div className="live-head-tools">
            <button className="tool-btn" onClick={history.undo} disabled={!history.canUndo}
                    title="Undo (⌘Z)" aria-label="Undo">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none">
                <path d="M9 14 4 9l5-5M4 9h9a7 7 0 0 1 0 14h-3" stroke="currentColor"
                      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            <button className="tool-btn" onClick={history.redo} disabled={!history.canRedo}
                    title="Redo (⇧⌘Z)" aria-label="Redo">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none">
                <path d="m15 14 5-5-5-5m5 5h-9a7 7 0 0 0 0 14h3" stroke="currentColor"
                      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            <button className="tool-btn wide" onClick={history.reset} disabled={!history.isDirty}
                    title="Discard all edits and return to the original clip">
              Reset
            </button>
          </div>

          <div className="live-head-right">
            {exportError && <span className="live-status err">{exportError}</span>}
            {!exportError && status && <span className="live-status">{status}</span>}
            <button className="btn btn-primary btn-sm" onClick={doExport} disabled={exporting}
                    data-testid="export-clip-btn">
              {exporting ? "Rendering…" : "Export clip"}
            </button>
          </div>
        </header>

        <div className="live-body">
          {/* Section rail. Every control used to be stacked in one column, so
              choosing a caption style meant scrolling past speed, overlays and
              handles to find it, and nothing indicated where anything lived.
              Four sections, one visible at a time: the controls are the same
              controls, but you are only ever looking at the ones that belong to
              the decision you are making. */}
          <nav className="live-rail" aria-label="Editor sections">
            {SECTIONS.map((s) => (
              <button key={s.id} type="button"
                      className={`rail-btn ${section === s.id ? "on" : ""}`}
                      aria-current={section === s.id}
                      /* The label is built from two nested spans, one of which
                         is hidden at narrow widths, so the accessible name is
                         stated rather than inferred from whatever is visible. */
                      aria-label={`${s.name} — ${s.desc}`}
                      onClick={() => setSection(s.id)}>
                <span className="rail-icon" aria-hidden="true">{s.icon}</span>
                <span className="rail-text">
                  <strong>{s.name}</strong>
                  <em>{s.desc}</em>
                </span>
              </button>
            ))}
          </nav>

          {/* The clip and the two timelines that describe it are ONE column and
              scroll as one. Previously they were three separate grid items, one
              of which collided with the footer's row -- which is how the trim
              bar and the layer strip ended up painted over the video. */}
          <div className="live-left">
          {/* Stage: the proxy, framed exactly as the renderer frames it */}
          <div className="live-stage">
            <div className={`stage-frame frame-${frameMode} ratio-${ratio.replace(":", "-")}`}
                 style={{ aspectRatio: String(RATIOS[ratio] ?? 9 / 16),
                          background: (frameMode === "fit" || frameMode === "auto")
                            ? (background === "black" ? "#000" : background)
                            : "#000" }}>
              {isSplit ? (
                /* Speaker on top, looping gameplay below -- the same vstack the
                   renderer builds, so what you trim is what you export. */
                <div className="split-stage">
                  <video
                    ref={videoRef}
                    src={proxySrc}
                    playsInline
                    preload="auto"
                    onClick={togglePlay}
                    onLoadedMetadata={onMeta}
                  />
                  <video
                    ref={gameplayRef}
                    src={gameplayUrl(job.id, gameplayFile)}
                    playsInline
                    muted
                    loop
                    preload="auto"
                    aria-hidden="true"
                  />
                </div>
              ) : smartFrame && visibleSmartTiles.length ? (
                /* Camera-aware podcast. One face fills the frame; simultaneous
                   participants get a familiar split/grid. Every tile is another
                   view of the same proxy and is kept frame-locked above. */
                <>
                  {visibleSmartTiles.map((tile, tileIndex) => {
                    const [x, y, w, h] = tile.rect;
                    return (
                      <div key={`${smartScene?.start ?? 0}-${tileIndex}`}
                           className="stage-tile"
                           style={{ left: `${x * 100}%`, top: `${y * 100}%`,
                                    width: `${w * 100}%`, height: `${h * 100}%`,
                                    background: tile.backdrop
                                      ? "linear-gradient(165deg, #1a1f27, #08090b)"
                                      : undefined }}>
                        {!tile.backdrop && (
                          <video
                            ref={tileIndex === 0
                              ? videoRef
                              : (el) => { podcastTileRefs.current[tileIndex - 1] = el; }}
                            src={proxySrc}
                            playsInline muted={tileIndex > 0} preload="auto"
                            aria-hidden={tileIndex > 0 ? "true" : undefined}
                            onClick={togglePlay}
                            onLoadedMetadata={tileIndex === 0 ? onMeta : undefined}
                            style={tile.fit
                              ? fitStyle(tile.win[0], tile.win[1],
                                         tile.point.cx, tile.point.cy,
                                         planW, planH, w, h)
                              : cropStyle(tile.win[0], tile.win[1],
                                          tile.point.cx, tile.point.cy, planW, planH)}
                          />
                        )}
                      </div>
                    );
                  })}
                </>
              ) : (
                <>
                  {/* The default frame is a blurred copy of the source behind
                      the source itself. Drawing only the sharp layer left the
                      preview with black bars the export does not have. */}
                  {(frameMode === "blur" || frameMode === "podcast") && (
                    <video
                      ref={blurRef}
                      className="stage-blur"
                      src={proxySrc}
                      playsInline muted preload="auto" aria-hidden="true"
                    />
                  )}
                  <video
                    ref={videoRef}
                    className="stage-fg"
                    src={proxySrc}
                    playsInline
                    preload="auto"
                    onClick={togglePlay}
                    onLoadedMetadata={onMeta}
                    style={frameMode === "blur"
                      ? { height: `${fgHeight * 100}%`, top: `${(1 - fgHeight) * 50}%` }
                      /* Podcast: same layer, but sat at its own bias rather
                         than centred, matching podcast_filter's overlay y. */
                      : frameMode === "podcast"
                      ? { height: `${fgHeight * 100}%`,
                          top: `${blockTop(PODCAST_CONTENT_BIAS) * 100}%`,
                          objectFit: "contain" }
                      /* Fit: the same zoomed block as blur, sat at its own bias
                         so the taller bar underneath is the caption band. */
                      : (frameMode === "fit" || frameMode === "auto")
                      ? { height: `${fgHeight * 100}%`,
                          top: `${blockTop(FIT_CONTENT_BIAS) * 100}%` }
                      : undefined}
                  />
                </>
              )}
              {!proxyReady && (
                <div className="stage-wait">
                  <span className="stage-spinner" />
                  <strong>Preparing the editor preview</strong>
                  <em>Your clips are already done — this is the scrubbing copy.</em>
                </div>
              )}
              {/* The title card, drawn on the same schedule the renderer uses
                  (subtitles.TITLE_STYLE: top 7%, first 4.5s). Editing the title
                  or its look now shows up here instead of only after an
                  export. */}
              {title.trim() && titleStyle !== "none" && clipNow < TITLE_SECONDS && (
                <div className="stage-title" style={{ top: `${TITLE_TOP * 100}%` }}>
                  <span style={{
                    fontFamily: fontCss(titleFont),
                    fontSize: `${((titleSize ?? TITLE_PX) / outW) * 100}cqw`,
                    ...titleLookCss(titleLook),
                  }}>
                    {title}
                  </span>
                </div>
              )}

              {capOn && cue && (
                <div
                  className={`stage-caption draggable cap-enter-${st.entrance || "none"} ${st.position === "middle" && capPos === null ? "mid" : ""}`}
                  style={capPos !== null
                    ? { top: `${capPos * 100}%`, bottom: "auto", transform: "translateY(-50%)" }
                    : sceneCapPos !== null
                    ? { top: `${sceneCapPos * 100}%`, bottom: "auto",
                        transform: "translateY(-100%)" }
                    : undefined}
                  onMouseDown={(e) => {
                    e.stopPropagation();
                    if (canEditCaptions) setDragCaption(true);
                    else onUpgrade?.("caption_editing");
                  }}
                  title={canEditCaptions ? "Drag to move captions" : "Upgrade to edit captions"}
                >
                  <span
                    key={cue.words.join(" ")}
                    className="cap-inner"
                    style={{
                      /* The preset's own typeface unless one was picked by
                         hand -- the renderer resolves it in the same order. */
                      fontFamily: fontCss(capFont ?? st.font),
                      fontSize: `${((capSize ?? st.px) * canvasScale / outW) * 100}cqw`,
                      fontWeight: 900,
                      fontStyle: st.italic ? "italic" : undefined,
                      color: st.color,
                      letterSpacing: st.spacing ? `${st.spacing * 0.02}em` : undefined,
                      transform: st.rotate ? `rotate(${-st.rotate}deg)` : undefined,
                      background: st.box === "line" ? st.boxColor : "transparent",
                      /* A backdrop band is wider than tall (renderer: \xbord). */
                      padding: st.box === "line" && st.padX
                        ? `${(st.outline || 0) / (st.px || 76)}em ${st.padX / (st.px || 76)}em`
                        : undefined,
                      /* The renderer's outline is a stroke around the glyphs, so
                         preview it as one. A blanket drop shadow made every
                         preset look identically "shadowed" regardless of the
                         outline width it actually exports with. */
                      textShadow: strokeShadow(st),
                      textTransform: st.caps ? "uppercase" : "none",
                    }}
                  >
                    {st.mode === "headline" ? (() => {
                      /* Mirrors the renderer's headline: the first word(s) big
                         over a highlighter bar, the rest small underneath. */
                      const k = Math.min(headlineSplit(cue.words), cue.words.length);
                      const word = (w, i) => (
                        <span key={i} style={previewWordStyle(st, i, cue.activeIndex, cue.words.length, cue.kw?.get(i))}>
                          {w}{" "}
                        </span>
                      );
                      return (
                        <>
                          <span className="cap-headline" style={st.bar ? {
                            background: `linear-gradient(to bottom, transparent 52%, ${st.bar} 52%)`,
                          } : undefined}>
                            {cue.words.slice(0, k).map((w, i) => word(w, i))}
                          </span>
                          {k < cue.words.length && (
                            <span className="cap-headline-small"
                                  style={{ fontSize: `${(st.stackSmall || 58) / 100}em` }}>
                              {cue.words.slice(k).map((w, i) => word(w, i + k))}
                            </span>
                          )}
                        </>
                      );
                    })() : cue.words.map((w, i) => (
                      <span key={i} style={previewWordStyle(st, i, cue.activeIndex, cue.words.length, cue.kw?.get(i))}>
                        {w}{" "}
                      </span>
                    ))}
                  </span>
                </div>
              )}

              {/* Safe areas: where the platform puts its own buttons */}
              {showSafe && (
                <div className="safe-guides" aria-hidden="true">
                  <span className="safe-band" style={{ top: 0, height: `${SAFE.top * 100}%` }} />
                  <span className="safe-band" style={{ bottom: 0, height: `${SAFE.bottom * 100}%` }} />
                  <span className="safe-band right" style={{ right: 0, width: `${SAFE.right * 100}%` }} />
                </div>
              )}

              {overlayList.map((o, i) => {
                /* Overlay windows are clip-output-relative, after the same
                   pause tightening the renderer applies. */
                const rel = clipNow;
                const on = rel >= (o.t_start ?? 0) && rel <= (o.t_end ?? clipDuration);
                // Outside its window an overlay is GONE from the export. A
                // selected one is still drawn, faint, so it can be positioned --
                // but never at full strength, which would claim it is on screen.
                if (!on && selected !== i) return null;
                return (
                <div
                  key={i}
                  className={`ov ${selected === i ? "sel" : ""} ${on ? "" : "ghost"}`}
                  style={{ left: `${o.x * 100}%`, top: `${o.y * 100}%`,
                           opacity: on ? (o.opacity ?? 1) : 0.28 }}
                  onMouseDown={(e) => { e.stopPropagation(); selectOverlay(i); setDragOverlay(i); }}
                >
                  {o.type === "image" ? (
                    <img src={overlayImageUrl(job.id, o.file)} alt=""
                         style={{ height: `${(o.size ?? 0.12) * 100}cqh` }} draggable={false} />
                  ) : (
                    <span className={o.platform ? "ov-handle" : undefined} style={{
                      fontFamily: fontCss(o.font),
                      fontSize: `${(o.size ?? 0.045) * 100}cqh`,
                      color: o.color || "#fff",
                      /* Mirrors platform_logos.compose_handle: dark glass pill
                         with the platform accent as its edge. */
                      ...(o.platform ? {
                        background: "rgba(18,18,22,0.84)",
                        border: `0.055em solid ${PLATFORMS.find((p) => p.id === o.platform)?.accent || "#fff"}`,
                        padding: "0.30em 0.40em",
                        borderRadius: "999px",
                        textShadow: "none",
                      } : {
                        background: o.plate || "transparent",
                        padding: o.plate ? "0.1em 0.35em" : 0,
                        borderRadius: o.plate ? 4 : 0,
                        textShadow: o.plate ? "none" : "0 2px 0 #000, 0 0 10px rgba(0,0,0,.8)",
                      }),
                      whiteSpace: "nowrap",
                    }}>
                      {/* The export draws the brand mark beside the handle, so
                          the preview has to as well -- otherwise the editor is
                          showing a different overlay than the one rendered. */}
                      {o.platform && (
                        /* The renderer's own PNG, not a lookalike icon -- the
                           preview then shows the exact mark that gets burned in. */
                        <img className="ov-logo" alt="" draggable={false}
                             src={`/api/platform-logos/${o.platform}.png`} />
                      )}
                      {o.text}
                    </span>
                  )}
                </div>
                );
              })}
            </div>

            <div className="stage-controls">
              <button className="stage-play" onClick={togglePlay}>
                {playing ? "❚❚" : "▶"}
              </button>
              <span className="stage-time">
                {fmt(clipNow)} / {duration.toFixed(1)}s
              </span>
            </div>
          </div>


        {/* Timeline */}
        <div className="live-timeline">
          <div className="tl-track" ref={trackRef}
               onMouseDown={(e) => {
                 // Clicking the bar scrubs. Handles stop propagation, so
                 // grabbing an in/out point still trims rather than seeks.
                 const r = trackRef.current.getBoundingClientRect();
                 const t = viewMap.outputToSource(
                   Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1) * span);
                 seek(Math.min(Math.max(t, start), end));
                 setDragging("scrub");
               }}>
            {viewMap.words.map((w, i) => (
              <span key={i} className="tl-tick"
                    style={{ left: `${pct(w.start)}%`, width: `${Math.max(pct(w.end) - pct(w.start), 0.3)}%` }} />
            ))}
            <span className="tl-selection" style={{ left: `${pct(start)}%`, width: `${pct(end) - pct(start)}%` }} />
            {/* Grabbable, so you can step back through a moment instead of
                replaying the clip from the top to reach it. */}
            <span className="tl-playhead" style={{ left: `${pct(now)}%` }}
                  onMouseDown={(e) => { e.stopPropagation(); setDragging("scrub"); }}
                  role="slider" aria-label="Playhead" />
            <span className="tl-handle" style={{ left: `${pct(start)}%` }}
                  onMouseDown={(e) => { e.stopPropagation(); setDragging("start"); }}
                  role="slider" aria-label="Clip start" />
            <span className="tl-handle end" style={{ left: `${pct(end)}%` }}
                  onMouseDown={(e) => { e.stopPropagation(); setDragging("end"); }}
                  role="slider" aria-label="Clip end" />
          </div>
          <div className="tl-scale">
            <span>{fmt(0)}</span>
            <span>{fmt(viewMap.duration)}</span>
          </div>
        </div>

        <TimelineTracks
          clipDuration={clipDuration}
          now={Math.min(Math.max(clipNow, 0), clipDuration)}
          overlays={overlayList}
          selected={selected}
          onSelect={selectOverlay}
          onSeek={seekClipTime}
          onChange={(i, patch) => patchOverlay(i, patch)}
        />
          </div>

          {/* The inspector shows ONE section at a time, chosen on the rail. */}
          <div className="live-inspector">
          <div className="insp-head">
            <h2>{SECTIONS.find((s) => s.id === section)?.name}</h2>
            <p>{SECTIONS.find((s) => s.id === section)?.desc}</p>
          </div>

          {/* The transcript belongs to Captions: it is where caption TEXT is
              edited, and it doubles as a scrubber. Showing it under Brand or
              Video was just noise between you and the control you came for. */}
          {section === "captions" && !canEditCaptions && (
          <div className="caption-editor-lock">
            <span className="caption-editor-lock-badge">Creator</span>
            <h3>Your chosen captions are already applied.</h3>
            <p>Upgrade to change the words, style, colours, size, language or position in the editor.</p>
            <button className="btn btn-primary btn-sm" onClick={() => onUpgrade?.("caption_editing")}>
              View plans
            </button>
          </div>
          )}

          {section === "captions" && canEditCaptions && (
          <div className="live-transcript">
            {loadError && <div className="editor-error">{loadError}</div>}
            {!transcript && !loadError && <div className="editor-loading">Loading…</div>}
            {transcript && !transcript.utterances?.length && (
              <div className="editor-loading">No speech in this clip, so there are no captions to edit.</div>
            )}
            {transcript?.utterances?.map((u, i) => {
              const inClip = u.end > start && u.start < end;
              const active = now >= u.start && now <= u.end;
              return (
                <div
                  key={i}
                  className={`tx-line ${inClip ? "in" : ""} ${active ? "playing" : ""}`}
                  onClick={() => seek(Math.min(Math.max(u.start, start), end))}
                >
                  <span className="tx-time">{fmt(u.start)}</span>
                  <input
                    className="tx-text tx-input live-cap-input"
                    value={lineEdits[i] ?? u.text}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => updateLineEdit(i, e.target.value, u.text)}
                    aria-label={`Caption at ${fmt(u.start)}`}
                  />
                </div>
              );
            })}
          </div>
          )}

        {/* Controls — every one of these is instant */}
        {section === "layout" && (
        <div className="live-controls">
          <div className="ctl">
            <span className="ctl-label">Sound</span>
            <button type="button"
                    className={`chip-btn ${autoSfx ? "on" : ""}`}
                    onClick={() => setAutoSfx(!autoSfx)}
                    title="Whoosh on each layout cut, mixed under the real audio."
                    data-testid="auto-sfx-toggle">
              Auto SFX
            </button>
            <button type="button"
                    className={`chip-btn ${musicBed ? "on" : ""}`}
                    onClick={() => setMusicBed(!musicBed)}
                    title="A quiet, ducked music bed under the voice."
                    data-testid="music-bed-toggle">
              Music bed
            </button>
          </div>
          <div className="ctl">
            <span className="ctl-label">Look good</span>
            <button type="button"
                    className={`chip-btn ${autoZoom ? "on" : ""}`}
                    onClick={() => setAutoZoom(!autoZoom)}
                    title="Punch in on the clip's key words."
                    data-testid="auto-zoom-toggle">
              Auto Zoom
            </button>
            <button type="button"
                    className={`chip-btn ${autoTransitions ? "on" : ""}`}
                    onClick={() => setAutoTransitions(!autoTransitions)}
                    title="A short dip through black at every layout cut."
                    data-testid="auto-transitions-toggle">
              Auto transitions
            </button>
            <button type="button"
                    className={`chip-btn ${autoBroll ? "on" : ""}`}
                    onClick={() => setAutoBroll(!autoBroll)}
                    title="Cut to the full game frame for a beat at action peaks."
                    data-testid="auto-broll-toggle">
              B-roll
            </button>
          </div>
          <div className="ctl">
            <span className="ctl-label">Template</span>
            {[
              { id: "auto",     label: "Auto" },
              { id: "podcast",  label: "Podcast" },
              { id: "classic",  label: "Classic" },
              { id: "gameplay", label: "Split" },
              { id: "fitvideo", label: "Fit video" },
            ].map((t) => (
              <button key={t.id}
                      className={`chip-btn ${activeTemplate === t.id ? "on" : ""}`}
                      onClick={() => setTemplate(t.id)}>{t.label}</button>
            ))}
          </div>
          {isSplit && gameplayList.length > 1 && (
          <div className="ctl">
            <span className="ctl-label">Gameplay</span>
            <div className="gameplay-picker">
              {gameplayList.map((g) => (
                <button
                  key={g.file}
                  className={`gameplay-thumb ${gameplayFile === g.file ? "on" : ""}`}
                  onClick={() => setGameplayFile(g.file)}
                  title={g.name}
                  onMouseEnter={(e) => { const v = e.currentTarget.querySelector("video"); if (v) v.play(); }}
                  onMouseLeave={(e) => { const v = e.currentTarget.querySelector("video"); if (v) { v.pause(); v.currentTime = 0; } }}
                >
                  <video
                    src={gameplayFileUrl(g.file)}
                    muted
                    loop
                    playsInline
                    preload="metadata"
                  />
                  <span className="gameplay-thumb-name">{g.name}</span>
                </button>
              ))}
            </div>
          </div>
          )}
          <div className="ctl">
            <span className="ctl-label">Ratio</span>
            {Object.keys(RATIOS).map((r) => (
              <button key={r} className={`chip-btn ${ratio === r ? "on" : ""}`} onClick={() => setRatio(r)}>{r}</button>
            ))}
          </div>
        </div>
        )}

        {section === "captions" && canEditCaptions && (
        <div className="live-controls">
          <div className="ctl ctl-block">
            <div className="cap-onoff">
              <span className="cap-onoff-copy">
                <strong>Burn captions in</strong>
                <span>{capOn
                  ? "Word-timed captions are rendered into the file."
                  : "This clip exports clean — no captions in the video."}</span>
              </span>
              <button
                className="switch"
                role="switch"
                aria-checked={capOn}
                aria-label="Burn captions into the video"
                data-testid="captions-toggle"
                onClick={() => setCapOn(!capOn)}
              />
            </div>
          </div>
        </div>
        )}

        {section === "captions" && canEditCaptions && capOn && (
        <div className="live-controls">
          <div className="ctl ctl-block">
            <span className="ctl-label">Caption style</span>
            <CaptionPresetPicker value={capStyle} onChange={setCapStyle}
                                 data={presetData} />
          </div>
          <div className="ctl">
            <span className="ctl-label">Words</span>
            <div className="segmented words-seg" role="group" aria-label="Words per cue">
              {[1, 2, 3].map((n) => {
                const current = capWords || st.maxWords || 3;
                return (
                  <button key={n} type="button"
                          className={`seg ${current === n ? "on" : ""}`}
                          aria-pressed={current === n}
                          onClick={() => setCapWords(n)}
                          data-testid={`cap-words-${n}`}>
                    {n === 1 ? "1 at a time" : n}
                  </button>
                );
              })}
            </div>
          </div>
          <div className="ctl">
            <span className="ctl-label">Emoji</span>
            <button
              type="button"
              className={`chip-btn ${autoEmoji ? "on" : ""}`}
              onClick={() => setAutoEmoji(!autoEmoji)}
              title={autoEmoji
                ? "Emoji on the few lines that earn one. Click for none."
                : "No emoji in the export. Click to allow them again."}
              data-testid="emoji-toggle"
            >{autoEmoji ? "On key lines" : "Off"}</button>
          </div>
        </div>
        )}

        {/* Colour is separate from the preset on purpose: a preset is a whole
            design (size, casing, cadence, how the spoken word is marked), and
            people want that design in their own colours rather than a new
            preset per palette. */}
        {section === "captions" && canEditCaptions && capOn && (
        <div className="live-controls">
          <div className="ctl">
            <span className="ctl-label">Text colour</span>
            <ColourRow value={capColor} fallback={st.color} onChange={setCapColor}
                       testid="cap-text-colour" />
          </div>
          <div className="ctl">
            <span className="ctl-label">Spoken word</span>
            <ColourRow value={capActive} fallback={st.active} onChange={setCapActive}
                       testid="cap-active-colour" />
          </div>
        </div>
        )}

        {section === "captions" && canEditCaptions && !capOn && (
          <p className="captions-off-note">
            Caption styling is hidden while captions are off. The transcript
            above still drives the cut, so trims stay word-accurate.
          </p>
        )}

        {/* The title is its own design decision, not a second caption -- a
            white plate is right for a talking-head clip and wrong for a gaming
            one, so the look and the typeface are both choices here. */}
        {section === "captions" && canEditCaptions && (
        <div className="live-controls">
          <div className="ctl">
            <span className="ctl-label">Title look</span>
            <div className="title-looks">
              {titleLooks.map((l) => (
                <button key={l.id}
                        className={`title-look ${titleStyle === l.id ? "on" : ""}`}
                        onClick={() => setTitleStyle(l.id)}
                        title={l.id === "none" ? "No title burned in" : l.id}>
                  {/* "Off" is a look like any other: it is where you go when you
                      want the cut and the captions but intend to write the hook
                      yourself. Burning one in cannot be undone after export. */}
                  {l.id === "none"
                    ? <span className="title-look-off">Off</span>
                    : <span style={titleLookCss(l)}>Aa</span>}
                </button>
              ))}
            </div>
          </div>
          {/* The export silently drops emoji unless a font on libass's fallback
              path has the glyph, and the preview drew them regardless -- so they
              looked fine in the editor and were simply missing afterwards. Say
              so where the text is, instead of letting the render be the first
              time anyone finds out. */}
          {presetData && presetData.emoji_supported === false
            && EMOJI_RE.test(title) && (
            <p className="ctl-warn">
              Emoji won’t burn into the video. The renderer has no emoji face it
              can actually draw — colour fonts like <em>Noto Color Emoji</em> and
              Apple Color Emoji store the picture in a format this build of
              libass doesn’t rasterise, so they map the character and then draw
              nothing. Drop the <strong>monochrome</strong> <em>Noto Emoji</em>
              (<code>NotoEmoji-Regular.ttf</code>) into{" "}
              <code>backend/assets/fonts</code> and it starts working with no
              other change.
            </p>
          )}
          <div className="ctl">
            <span className="ctl-label">Title font</span>
            <FontPicker value={titleFont} onChange={setTitleFont}
                        needsDevanagari={/[ऀ-ॿ]/.test(title)} />
          </div>
          <div className="ctl">
            <span className="ctl-label">Title size</span>
            <input type="range" min={40} max={160} step={4}
                   value={titleSize ?? 92}
                   onChange={(e) => setTitleSize(Number(e.target.value))} />
            <span className="ctl-value">{titleSize ?? 92}px</span>
          </div>
        </div>
        )}

        {section === "captions" && canEditCaptions && capOn && (
        <div className="live-controls type-bar">
          <div className="ctl">
            <span className="ctl-label">Font</span>
            <FontPicker value={capFont} onChange={setCapFont}
                        needsDevanagari={hasDevanagari} />
          </div>
          <div className="ctl">
            <span className="ctl-label">Size</span>
            <SizePicker value={capSize} defaultPx={st.px} onChange={setCapSize} />
            {capSize !== null && (
              <button className="chip-btn subtle" onClick={() => setCapSize(null)}>Auto</button>
            )}
          </div>
          <div className="ctl">
            <span className="ctl-label">Subtitles</span>
            <LanguagePicker value={translateTo} onChange={setTranslateTo} />
          </div>
          <div className="ctl">
            <span className="ctl-label">Position</span>
            <span className="ctl-hint">drag captions on the video</span>
            {capPos !== null && (
              <button className="chip-btn subtle" onClick={() => setCapPos(null)}>Reset</button>
            )}
          </div>
        </div>
        )}

        {/* The standalone Animation control is gone. Entrance is part of what a
            preset IS -- "Hormozi Punch" names a specific arrival as much as a
            specific colour -- and offering the two as orthogonal menus mostly
            produced combinations nobody wanted. Each preset carries its own. */}

        {section === "video" && (
        <div className="live-controls">
          <div className="ctl ctl-block pacing-control">
            <div className="pacing-copy">
              <span className="ctl-label">Pacing</span>
              <span className="ctl-hint">
                {!tightClipMap.active
                  ? "No long pauses found in this clip"
                  : tightenPauses
                  ? `${tightenedSeconds.toFixed(1)}s of dead air removed`
                  : `${tightenedSeconds.toFixed(1)}s of removable dead air`}
              </span>
            </div>
            <div className="pacing-segment" role="group" aria-label="Clip pacing">
              <button type="button" className={!tightenPauses ? "on" : ""}
                      aria-pressed={!tightenPauses}
                      onClick={() => setTightenPauses(false)}>
                Original
              </button>
              <button type="button"
                      className={`${tightenPauses ? "on" : ""} ${canTightenPauses ? "" : "locked"} ${canTightenPauses && !tightClipMap.active ? "unavailable" : ""}`}
                      aria-pressed={tightenPauses}
                      aria-disabled={!tighteningAvailable}
                      title={canTightenPauses && !tightClipMap.active
                        ? "This clip has no long pauses to remove"
                        : undefined}
                      onClick={() => {
                        if (tighteningAvailable) setTightenPauses(true);
                        else if (!canTightenPauses) onUpgrade?.("tighten_pauses");
                      }}>
                Tight
                {!canTightenPauses && <span className="pacing-plan">Creator</span>}
              </button>
            </div>
          </div>
          <div className="ctl">
            <span className="ctl-label">Speed</span>
            {[0.5, 0.75, 1, 1.25, 1.5, 2].map((sp) => (
              <button key={sp} className={`chip-btn ${speed === sp ? "on" : ""}`}
                      onClick={() => setSpeed(sp)}>
                {sp}×
              </button>
            ))}
            {/* Two genuinely different effects, so it is a choice, not a toggle
                buried in settings. */}
            <button className={`chip-btn ${speedPitched ? "on" : ""}`}
                    onClick={() => setSpeedPitched(!speedPitched)}
                    title="Let pitch move with speed — the chipmunk/deep meme effect">
              {speedPitched ? "Pitch: meme" : "Pitch: natural"}
            </button>
          </div>

          {/* Deliberately NOT a toggle. The server decides the watermark from
              the plan alone (see main.py), which is the only safe place for it
              to be decided -- a switch here that the renderer ignored would be
              a lie, and one it obeyed would hand the watermark off to anyone.
              So this reports the real state, and for Free it is the upgrade
              path. It sits in Video because that is where someone looks when
              they are deciding what the exported file will look like.

              The preview never shows the watermark either way: it plays the
              proxy, and the credit is burned at export. Hence "exports", not
              "this clip". */}
          <div className="ctl ctl-block pacing-control">
            <div className="pacing-copy">
              <span className="ctl-label">Watermark</span>
              <span className="ctl-hint">
                {canRemoveWatermark
                  ? "Your exports have no KlipCut credit"
                  : "Free exports carry a small KlipCut credit"}
              </span>
            </div>
            {canRemoveWatermark ? (
              <span className="pacing-state" data-testid="watermark-clear">Removed ✓</span>
            ) : (
              <button type="button" className="pacing-segment-solo"
                      data-testid="watermark-upgrade"
                      onClick={() => onUpgrade?.("no_watermark")}>
                Remove it
                <span className="pacing-plan">Creator</span>
              </button>
            )}
          </div>
        </div>
        )}

        {/* The bars are part of how the frame is composed, not how the video
            plays, so they live with Ratio rather than with Speed. */}
        {section === "layout" && (
          <div className="live-controls">
            <div className="ctl">
              <span className="ctl-label">Bars</span>
              <div className="bg-row">
                {BACKGROUNDS.map((b) => (
                  <button key={b.id}
                          className={`bg-dot ${background === b.id ? "on" : ""}`}
                          style={{ background: b.css }}
                          onClick={() => setBackground(b.id)}
                          title={b.label} aria-label={b.label} />
                ))}
              </div>
            </div>
            <div className="ctl">
              <span className="ctl-label">Safe areas</span>
              <button className={`chip-btn ${showSafe ? "on" : ""}`}
                      onClick={() => setShowSafe((v) => !v)}>
                {showSafe ? "On" : "Off"}
              </button>
              <span className="ctl-hint">the bands the platforms cover</span>
            </div>
          </div>
        )}

        {section === "brand" && (
        <div className="live-controls ov-panel">
          <div className="ctl">
            <span className="ctl-label">Overlays</span>
            <button className="chip-btn" onClick={addText}>+ Text</button>
            <label className="chip-btn as-label">
              + Logo
              <input type="file" accept="image/png,image/jpeg,image/webp" hidden
                     onChange={(e) => addLogo(e.target.files?.[0])} />
            </label>
          </div>

          <div className="ctl">
            <span className="ctl-label">Handle</span>
            {/* One click puts a correctly branded handle in the right place --
                platform colours and a placement clear of the platform's own UI. */}
            {PLATFORMS.map((pf) => (
              <button key={pf.id} className="platform-btn"
                      style={{ "--pf": pf.plate }}
                      title={`Add ${pf.name} handle`}
                      onClick={() => {
                        setOverlayList((l) => [...l,
                          { ...platformOverlay(pf, ""), t_start: 0, t_end: clipDuration }],
                          { discrete: true });
                        selectOverlay(overlayList.length);
                      }}>
                <img className="pf-logo" src={logoSrc(pf.id)} alt="" />
                <span>{pf.name}</span>
              </button>
            ))}
          </div>

          {selected !== null && overlayList[selected] && (
            <div className="ov-edit">
              {overlayList[selected].type === "text" && (
                <>
                  <input
                    className="ov-text"
                    value={overlayList[selected].text || ""}
                    onChange={(e) => patchOverlay(selected, { text: e.target.value })}
                    placeholder="@yourhandle"
                  />
                  <FontPicker
                    value={overlayList[selected].font ?? null}
                    onChange={(id) => patchOverlay(selected, { font: id })}
                  />
                  <SizePicker
                    value={Math.round((overlayList[selected].size ?? 0.045) * 1920)}
                    defaultPx={86}
                    onChange={(px) => patchOverlay(selected, { size: px / 1920 })}
                  />
                  <input type="color" className="ov-color"
                         value={overlayList[selected].color || "#ffffff"}
                         onChange={(e) => patchOverlay(selected, { color: e.target.value })}
                         aria-label="Text colour" />
                  <button className={`chip-btn ${overlayList[selected].plate ? "on" : ""}`}
                          onClick={() => patchOverlay(selected,
                            { plate: overlayList[selected].plate ? null : "#000000" })}>
                    Plate
                  </button>
                </>
              )}
              {overlayList[selected].type === "image" && (
                <SizePicker
                  value={Math.round((overlayList[selected].size ?? 0.12) * 1920)}
                  defaultPx={230}
                  onChange={(px) => patchOverlay(selected, { size: px / 1920 })}
                />
              )}
              <button className="chip-btn danger" onClick={() => removeOverlay(selected)}>
                Delete
              </button>
            </div>
          )}
        </div>
        )}

        </div>
        </div>

        <footer className="live-foot">
          <span className="live-hint">
            {clip.rendered === false
              ? "Unsaved changes — export to update the downloadable file"
              : "Export is up to date"}
          </span>
          <span className="live-keys">
            <kbd>Space</kbd> play
            <kbd>←</kbd><kbd>→</kbd> scrub
            <kbd>C</kbd> captions
            <kbd>⌘Z</kbd> undo
          </span>
        </footer>
      </div>
    </div>,
    document.body
  );
}
