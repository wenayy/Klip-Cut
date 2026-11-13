import { useEffect, useRef, useState } from "react";
import { getCaptionOptions, API_BASE } from "./api";

/* Only the "keep the style's font" entry is fixed. Everything else is whatever
   is installed on the server right now -- the old hardcoded list is exactly why
   eight fonts sat in assets/fonts without ever appearing here. */
export const FONT_LIST = [
  { id: null, name: "Style default", css: "'Archivo Black', sans-serif" },
];

/* Declares @font-face for every server font, pointing at the server's own ttf.
   The preview then uses the exact file the renderer burns in, instead of a
   similarly-named webfont that kerns differently. */
export function installFontFaces(fonts) {
  const id = "clipper-font-faces";
  if (document.getElementById(id)) return;
  const el = document.createElement("style");
  el.id = id;
  el.textContent = fonts
    .filter((f) => f.id && f.id !== "impact")
    .map((f) => `@font-face{font-family:'${f.family}';src:url('${API_BASE}/fonts/${f.id}.ttf') format('truetype');font-display:swap;}`)
    .join("\n");
  document.head.appendChild(el);
}

/** Server font list, loaded once, each face previewed in itself. */
export function useFonts() {
  const [fonts, setFonts] = useState(FONT_LIST);
  useEffect(() => {
    let alive = true;
    getCaptionOptions()
      .then((o) => {
        if (!alive || !o.fonts) return;
        installFontFaces(o.fonts);
        setFonts([
          FONT_LIST[0],
          ...o.fonts
            .filter((f) => f.id !== "impact")
            .map((f) => ({
              id: f.id,
              name: f.label,
              css: `'${f.family}', sans-serif`,
              devanagari: f.devanagari,
            })),
        ]);
      })
      .catch(() => {});
    return () => { alive = false; };
  }, []);
  return fonts;
}

/* Fallback shapes only. The real list is served from subtitles.TITLE_LOOKS, so
   adding a look on the backend makes it selectable without touching this file
   -- the same reason the font list is no longer hardcoded. */
const TITLE_STYLE_FALLBACK = [
  { id: "plate", text: "#101010", edge: "#FFFFFF", boxed: true },
];

/** Title looks, served by the renderer so the swatches cannot drift. */
export function useTitleStyles() {
  const [looks, setLooks] = useState(TITLE_STYLE_FALLBACK);
  useEffect(() => {
    let alive = true;
    getCaptionOptions()
      .then((o) => { if (alive && o.title_styles?.length) setLooks(o.title_styles); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);
  return looks;
}

/** CSS that makes a preview of a title look -- box, stroke or shadow. */
export function titleLookCss(look) {
  if (!look) return {};
  if (look.boxed) {
    return { color: look.text, background: look.edge, textShadow: "none",
             padding: "0.12em 0.4em" };
  }
  if (look.id === "shadow") {
    return { color: look.text, background: "transparent",
             textShadow: `0.03em 0.04em 0.05em ${look.edge}` };
  }
  if (look.id === "clean") {
    return { color: look.text, background: "transparent", textShadow: "none" };
  }
  // Outline: four offsets approximate a stroke without -webkit-text-stroke,
  // which thins glyphs rather than growing them the way libass does.
  const e = look.edge;
  return {
    color: look.text,
    background: "transparent",
    textShadow: `0.04em 0 0 ${e}, -0.04em 0 0 ${e}, 0 0.04em 0 ${e}, 0 -0.04em 0 ${e}`,
  };
}

/* Sizes in px on the 1080x1920 canvas -- the same number the renderer writes
   into the subtitle file, so the field is not a mystery multiplier. */
export const SIZE_PRESETS = [40, 48, 56, 64, 72, 80, 90, 100, 120, 140];
export const MIN_PX = 28;
export const MAX_PX = 160;

const LANGUAGES = [
  { code: "en", name: "English", native: "English" },
  { code: "hi", name: "Hindi", native: "हिंदी" },
  /* Latin script, so it sits next to Hindi but escapes the Devanagari-glyph
     warning below -- any font can draw it, which is half the reason creators
     caption this way in the first place. */
  { code: "hi-latn", name: "Hinglish", native: "Hinglish" },
  { code: "es", name: "Spanish", native: "Español" },
  { code: "pt", name: "Portuguese", native: "Português" },
  { code: "fr", name: "French", native: "Français" },
  { code: "de", name: "German", native: "Deutsch" },
  { code: "ar", name: "Arabic", native: "العربية" },
  { code: "id", name: "Indonesian", native: "Bahasa Indonesia" },
  { code: "ja", name: "Japanese", native: "日本語" },
];

function useOutsideClose(open, onClose) {
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const down = (e) => { if (!ref.current?.contains(e.target)) onClose(); };
    const key = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", key);
    };
  }, [open, onClose]);
  return ref;
}

/** Font picker. Closed it shows the current face in that face. */
export function FontPicker({ value, onChange, needsDevanagari = false }) {
  const [open, setOpen] = useState(false);
  const ref = useOutsideClose(open, () => setOpen(false));
  const fonts = useFonts();
  const current = fonts.find((f) => f.id === value) || fonts[0];

  return (
    <div className="picker" ref={ref}>
      <button className="picker-trigger" onClick={() => setOpen((v) => !v)}
              aria-haspopup="listbox" aria-expanded={open}>
        <span className="picker-value" style={{ fontFamily: current.css }}>
          {current.name}
        </span>
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" aria-hidden="true">
          <path d="m6 9 6 6 6-6" stroke="currentColor" strokeWidth="2.4"
                strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <ul className="picker-menu" role="listbox">
          {fonts.map((f) => {
            /* A face with no Devanagari glyphs renders Hindi as empty boxes, so
               say so before it is picked rather than after an export. */
            const cantDraw = needsDevanagari && f.id && !f.devanagari;
            return (
              <li key={f.id || "default"}>
                <button
                  role="option"
                  aria-selected={f.id === value}
                  className={`${f.id === value ? "on" : ""} ${cantDraw ? "font-warn" : ""}`}
                  style={{ fontFamily: f.css }}
                  onClick={() => { onChange(f.id); setOpen(false); }}
                  title={cantDraw ? "This font has no Hindi glyphs" : undefined}
                >
                  {f.name}
                  {cantDraw && <span className="font-flag">no हिंदी</span>}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** Numeric size in px: type it, step it, or pick a preset. */
export function SizePicker({ value, defaultPx, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useOutsideClose(open, () => setOpen(false));
  const shown = value ?? defaultPx;

  const clamp = (n) => Math.max(MIN_PX, Math.min(MAX_PX, Math.round(n)));

  return (
    <div className="size-field" ref={ref}>
      <button className="size-step" onClick={() => onChange(clamp(shown - 2))}
              aria-label="Smaller">−</button>
      <input
        className="size-input"
        type="number"
        min={MIN_PX}
        max={MAX_PX}
        value={shown}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (!Number.isNaN(n)) onChange(clamp(n));
        }}
        aria-label="Caption size in pixels"
      />
      <span className="size-unit">px</span>
      <button className="size-step" onClick={() => onChange(clamp(shown + 2))}
              aria-label="Bigger">+</button>
      <button className="size-caret" onClick={() => setOpen((v) => !v)} aria-label="Size presets">
        <svg viewBox="0 0 24 24" width="12" height="12" fill="none">
          <path d="m6 9 6 6 6-6" stroke="currentColor" strokeWidth="2.4"
                strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <ul className="picker-menu size-menu" role="listbox">
          {SIZE_PRESETS.map((n) => (
            <li key={n}>
              <button role="option" aria-selected={n === shown}
                      className={n === shown ? "on" : ""}
                      onClick={() => { onChange(n); setOpen(false); }}>
                {n} px
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Subtitle language.
 *
 * Translating replaces word-by-word karaoke with whole lines, because the
 * translated words are different words -- highlighting them on the original
 * timings would be inventing timing that does not exist. Said plainly in the
 * panel rather than discovered after an export.
 */
export function LanguagePicker({ value, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useOutsideClose(open, () => setOpen(false));
  const current = LANGUAGES.find((l) => l.code === value);

  return (
    <div className="picker lang" ref={ref}>
      <button className="picker-trigger" onClick={() => setOpen((v) => !v)}
              aria-haspopup="dialog" aria-expanded={open}>
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true">
          <path d="M4 5h10M9 3v2c0 4-2.5 7-5 8m3-4c0 2.5 2.5 5 6 6M14 20l4-9 4 9m-6.5-2h5"
                stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <span className="picker-value">{current ? current.native : "Original"}</span>
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" aria-hidden="true">
          <path d="m6 9 6 6 6-6" stroke="currentColor" strokeWidth="2.4"
                strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && (
        <div className="lang-panel" role="dialog" aria-label="Subtitle language">
          <div className="lang-head">
            <strong>Subtitle language</strong>
            <span className="pro-badge">Pro</span>
          </div>
          <p className="lang-note">
            Audio stays in the original language — only the captions change.
          </p>

          <button
            className={`lang-row ${value === null ? "on" : ""}`}
            onClick={() => { onChange(null); setOpen(false); }}
          >
            <span>Original</span>
            <span className="lang-tag">as spoken</span>
          </button>

          <div className="lang-list">
            {LANGUAGES.map((l) => (
              <button
                key={l.code}
                className={`lang-row ${value === l.code ? "on" : ""}`}
                onClick={() => { onChange(l.code); setOpen(false); }}
              >
                <span>{l.native}</span>
                <span className="lang-tag">{l.name}</span>
              </button>
            ))}
          </div>

          {value && (
            <p className="lang-warn">
              Translated captions show whole lines instead of word-by-word
              highlighting — translated words don’t match the original timings.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
