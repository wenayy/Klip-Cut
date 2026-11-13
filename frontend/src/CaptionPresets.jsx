import { useEffect, useMemo, useState } from "react";
import { getCaptionOptions } from "./api";
import { installFontFaces } from "./CaptionToolbar";

/**
 * The caption preset picker.
 *
 * Every card is a live sample of the preset it selects -- same colours, same
 * casing, same active-word effect, animating on the same cadence the export
 * uses. The old picker was a row of chips reading "classic / fitbox / tech",
 * which told you nothing about what you were choosing: the only way to see a
 * look was to pick it, wait for a render, and look at the result.
 *
 * The definitions come from the server (`/caption-options`), not from a copy
 * kept here. That is the whole point -- the editor and the renderer used to keep
 * separate style tables and they disagreed about sizes and colours.
 */

/* Sample text per category. A podcast preset shown on "THIS IS AMAZING" reads
   as a bad podcast preset; each family gets a line it is actually for. */
const SAMPLES = {
  signature: ["that", "was", "insane"],
  karaoke:   ["this", "is", "amazing"],
  hormozi:   ["you", "need", "this"],
  hype:      ["that", "was", "insane"],
  podcast:   ["the", "biggest", "mistake"],
  social:    ["you", "have", "to", "see"],
  cinematic: ["some", "things", "end"],
  neon:      ["next", "level"],
  story:     ["and", "then", "it", "happened"],
};

const RAMP = ["#8040f0", "#d060e0", "#f080d0", "#60d0f0"];

/** Inline style for one sample word, given the preset and the word's index. */
function wordStyle(p, i, active) {
  const base = { "--i": i };
  if (p.effect === "gradient") {
    return { ...base, color: RAMP[i % RAMP.length] };
  }
  if (!active) return { ...base, color: p.color };

  const s = {
    ...base,
    color: p.effect === "marker" ? p.color : p.active,
  };
  if (p.effect === "marker") {
    s.background = p.boxColor || p.active;
    s.color = p.color;
  }
  if (p.effect === "glow") {
    s.textShadow = `0 0 0.3em ${p.active}, 0 0 0.6em ${p.active}`;
  }
  return s;
}

function PresetCard({ preset, selected, onSelect, fontFamily }) {
  const p = preset;
  const words = SAMPLES[p.category] || SAMPLES.karaoke;
  const stacked = p.mode === "stack";
  /* The card is a fixed size, so preset px (a number on the 1080-wide output
     canvas) has to be scaled to it rather than used raw -- otherwise a 112px
     Hormozi preset would render at 112px in a 150px-wide tile. */
  const fontSize = `${Math.max(9, Math.min(19, p.px * 0.16))}px`;

  const cls = [
    "cap-card",
    selected ? "on" : "",
    `fx-${p.effect}`,
    `mode-${p.mode}`,
    p.box === "line" ? "boxed" : "",
    stacked ? "stacked" : "",
    p.reveal ? "reveal" : "",
  ].join(" ");

  const stage = {
    fontSize,
    letterSpacing: p.spacing ? `${p.spacing * 0.02}em` : undefined,
    textTransform: p.caps ? "uppercase" : "none",
    transform: p.rotate ? `rotate(${-p.rotate}deg)` : undefined,
    background: p.box === "line" ? p.boxColor : undefined,
    /* Outline is the renderer's stroke width in output px; as a share of the
       font size it survives the scaling above. */
    "--stroke": `${Math.min(0.09, ((p.textOutline ?? (p.box === "word" ? 0 : p.outline)) / p.px) * 0.9)}em`,
    "--ink": p.outlineColor || "#000",
    /* Hard offset shadow and halo, the two depth treatments the signature
       looks are built on. Both collapse to nothing for every older preset. */
    "--drop": p.shadowColor ? `${(p.shadow / p.px + 0.05).toFixed(3)}em` : "0",
    "--drop-ink": p.shadowColor || "transparent",
    "--halo": p.glow || "transparent",
    "--active": p.active,
    fontFamily: fontFamily || undefined,
  };

  return (
    <button type="button" className={cls} onClick={() => onSelect(p.id)}
            aria-pressed={selected} title={p.name}>
      <span className="cap-card-stage">
        <span className="cap-sample" style={stage}>
          {/* Each word is drawn twice: the resting copy, and the "lit" copy
              stacked exactly on top of it. Cycling the lit copy's opacity is
              what animates the karaoke sweep, and because both copies occupy
              the same box the line never reflows as the highlight moves --
              animating colour on a single span made every card twitch when the
              active word changed width under a scale effect. */}
          {words.map((w, i) => (
            <span key={i}
                  className={`cap-word ${stacked && i === words.length - 1 ? "big" : ""}`}
                  style={{ "--i": i, "--n": words.length,
                           ...(stacked && i < words.length - 1
                             ? { fontSize: `${(p.stackSmall || 58) / 100}em` } : null) }}>
              <span className="cap-word-rest" style={wordStyle(p, i, false)}>{w}</span>
              <span className="cap-word-lit" style={wordStyle(p, i, true)}>{w}</span>
            </span>
          ))}
        </span>
      </span>
      <span className="cap-card-name">{p.name}</span>
    </button>
  );
}

export function useCaptionPresets() {
  const [data, setData] = useState(null);
  useEffect(() => {
    let alive = true;
    getCaptionOptions()
      .then((o) => {
        if (!alive || !o.presets?.length) return;
        // Cards preview each preset in its own typeface, so the faces have to
        // be declared wherever the picker is shown, not only inside the editor.
        if (o.fonts) installFontFaces(o.fonts);
        setData(o);
      })
      .catch(() => {});
    return () => { alive = false; };
  }, []);
  return data;
}

export default function CaptionPresetPicker({ value, onChange, data }) {
  const [cat, setCat] = useState(null);

  const presets = data?.presets || [];
  const categories = data?.categories || [];
  const families = useMemo(
    () => Object.fromEntries((data?.fonts || []).map((f) => [f.id, `'${f.family}', sans-serif`])),
    [data],
  );

  /* Open on the category the current preset belongs to, so re-entering the
     editor shows you where you already are instead of resetting to the first
     tab and hiding your own selection two tabs away. */
  const activeCat = cat
    || presets.find((p) => p.id === value)?.category
    || categories[0]?.id;

  const shown = useMemo(
    () => presets.filter((p) => p.category === activeCat)
                 .sort((a, b) => (a.legacy === b.legacy ? 0 : a.legacy ? 1 : -1)),
    [presets, activeCat],
  );

  if (!presets.length) {
    return <div className="cap-picker-empty">Loading caption styles…</div>;
  }

  const meta = categories.find((c) => c.id === activeCat);

  return (
    <div className="cap-picker">
      <div className="cap-tabs" role="tablist">
        {categories.map((c) => (
          <button key={c.id} role="tab" aria-selected={c.id === activeCat}
                  className={`cap-tab ${c.id === activeCat ? "on" : ""}`}
                  onClick={() => setCat(c.id)}>
            {c.name}
          </button>
        ))}
      </div>
      {meta && <p className="cap-cat-desc">{meta.desc}</p>}
      <div className="cap-grid">
        {shown.map((p) => (
          <PresetCard key={p.id} preset={p} selected={p.id === value}
                      onSelect={onChange} fontFamily={families[p.font]} />
        ))}
      </div>
    </div>
  );
}
