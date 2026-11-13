import { Fragment, useEffect, useRef, useState } from "react";
import { submitJob, uploadJob, getSourcePreview, getJob, clipUrl } from "./api";
import HighlightResult from "./HighlightResult";
import LiveEditor from "./LiveEditor";
import Dashboard from "./Dashboard";
import Pricing from "./Pricing";
import AuthModal from "./AuthModal";
import { fetchMe, logout, createShare, API_BASE } from "./api";
import ThemeToggle, { useTheme } from "./ThemeToggle";
import { useToast } from "./Toast";
import StudioWizard from "./StudioWizard";
import useTurnstile from "./useTurnstile";
import UpgradeModal from "./UpgradeModal";
import WelcomeUpgrade, { PaymentProcessing } from "./WelcomeUpgrade";
import { usePlan } from "./usePlan";
import posthog, { isPostHogConfigured } from "./posthog";
const STAGES = [
  { key: "downloading", label: "Getting your video ready", blurb: "Your video is being prepared." },
  { key: "transcribing", label: "Listening to the conversation", blurb: "KlipCut is following what is being said." },
  { key: "analyzing", label: "Choosing the best moments", blurb: "Looking for clear hooks, stories and payoffs." },
  { key: "rendering", label: "Finishing your clips", blurb: "Adding your layout and caption style." },
];

const STAGE_ORDER = ["queued", "downloading", "transcribing", "analyzing", "rendering", "done"];

function identifyUser(user) {
  const distinctId = user?.id;
  if (!isPostHogConfigured || distinctId === undefined || distinctId === null) return;

  const personProperties = {};
  if (user.plan) personProperties.plan = user.plan;
  if (user.email) personProperties.email = user.email;
  posthog.identify(String(distinctId), personProperties);
}

function stageState(stageKey, jobStatus) {
  const current = STAGE_ORDER.indexOf(jobStatus);
  const mine = STAGE_ORDER.indexOf(stageKey);
  if (jobStatus === "done" || mine < current) return "done";
  if (mine === current) return "active";
  return "pending";
}

/* ---------- animation helpers ---------- */

/* Adds .in when the element scrolls into view */
function useReveal() {
  useEffect(() => {
    const els = document.querySelectorAll(".reveal");
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        });
      },
      { threshold: 0.12 }
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);
}

/* Button that leans toward the cursor */
function Magnetic({ children, className = "", strength = 0.35, ...rest }) {
  const ref = useRef(null);

  function onMove(e) {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const x = (e.clientX - r.left - r.width / 2) * strength;
    const y = (e.clientY - r.top - r.height / 2) * strength;
    el.style.transform = `translate(${x}px, ${y}px)`;
  }

  function onLeave() {
    const el = ref.current;
    if (el) el.style.transform = "translate(0, 0)";
  }

  return (
    <button ref={ref} className={`magnetic ${className}`} onMouseMove={onMove} onMouseLeave={onLeave} {...rest}>
      {children}
    </button>
  );
}

/* Number that counts up when it enters the viewport */
function CountUp({ to, suffix = "", duration = 1200 }) {
  const ref = useRef(null);
  const [val, setVal] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        io.disconnect();
        const t0 = performance.now();
        const tick = (t) => {
          const p = Math.min((t - t0) / duration, 1);
          const eased = 1 - Math.pow(1 - p, 3);
          setVal(Math.round(to * eased));
          if (p < 1) requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      },
      { threshold: 0.6 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [to, duration]);

  return (
    <span ref={ref}>
      {val}
      {suffix}
    </span>
  );
}

/* Card with a light spotlight that follows the cursor */
function SpotlightCard({ children, className = "", style }) {
  const ref = useRef(null);

  function onMove(e) {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty("--sx", `${e.clientX - r.left}px`);
    el.style.setProperty("--sy", `${e.clientY - r.top}px`);
  }

  return (
    <div ref={ref} className={`spotlight ${className}`} style={style} onMouseMove={onMove}>
      {children}
    </div>
  );
}

/** Filename for a downloaded clip, built from its title.
 *
 * Keeps non-Latin scripts intact -- a Hindi clip should land in Downloads with
 * its Hindi title, not a transliteration. Only characters that actually break
 * filesystems are stripped. */
function downloadName(title, index) {
  const cleaned = (title || "")
    .replace(/[/\\?%*:|"<>]/g, "")   // illegal on macOS/Windows
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 70);
  return `${cleaned || `clip_${index + 1}`}.mp4`;
}

/* True when the model scored this clip; highlights picked from sound and motion
   carry an empty scores object. */
function hasScores(clip) {
  return Boolean(clip.scores && Object.keys(clip.scores).length);
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/* Ticking mm:ss since the job started */
function Elapsed({ since }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return <>{fmtDuration((now - since) / 1000)}</>;
}

/* Circular percent ring */
function ProgressRing({ percent }) {
  const R = 54;
  const C = 2 * Math.PI * R;
  return (
    <div className="ring-wrap">
      <svg className="ring" viewBox="0 0 128 128">
        <circle className="ring-bg" cx="64" cy="64" r={R} />
        <circle
          className="ring-fg"
          cx="64"
          cy="64"
          r={R}
          strokeDasharray={C}
          strokeDashoffset={C - (C * Math.min(Math.max(percent, 0), 100)) / 100}
        />
      </svg>
      <span className="ring-label">
        <strong>{Math.round(percent)}</strong>
        <small>%</small>
      </span>
    </div>
  );
}

const PROCESSING_TIPS = [
  "You can close this page. We’ll keep working and add the finished clips to your Library.",
  "Your layout and caption choices are being applied to every clip.",
  "The strongest clips will appear first, ready for you to review.",
];

function friendlyProgress(message = "", status = "") {
  const text = String(message).toLowerCase();
  const percent = String(message).match(/\d+(?:\.\d+)?%/)?.[0];
  const suffix = percent ? ` ${percent}` : "";
  if (text.includes("deepgram") || text.includes("transcrib")) return "Listening to the conversation...";
  if (text.includes("download") || text.includes("fetching audio") || text.includes("disk space")) return `Getting your video ready...${suffix}`;
  if (text.includes("render") || text.includes("ffmpeg")) return "Finishing your clips...";
  if (text.includes("scor") || text.includes("energy") || text.includes("reading")) return "Comparing the moments worth posting...";
  if (message) return message;
  if (status === "analyzing") return "Finding the strongest moments...";
  return "Working on your clips...";
}

function ProcessingTip() {
  const [tip, setTip] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTip((value) => (value + 1) % PROCESSING_TIPS.length), 7000);
    return () => window.clearInterval(timer);
  }, []);
  return <span>{PROCESSING_TIPS[tip]}</span>;
}

/* Full-width working panel shown while a job runs */
function ProcessingStudio({ job, startedAt, onCancel }) {
  const currentIdx = Math.max(
    STAGES.findIndex((s) => stageState(s.key, job.status) === "active"),
    0
  );
  const failed = job.status === "failed";
  /* Waiting for a worker is not step one. Rendering it as "Getting your video
     ready" claimed work that had not started, so a queue of three looked like
     a job frozen at the first stage -- the exact reading that makes someone
     reload, resubmit, and pay twice. */
  const queued = job.status === "queued";
  const ahead = job.queue_position;
  const stageBase = (STAGE_ORDER.indexOf(job.status) - 1) / STAGES.length;
  const overall = failed || queued
    ? 0
    : Math.min(99, Math.max(0, stageBase * 100 + (job.percent || 0) / STAGES.length));

  return (
    <div className={`studio ${failed ? "failed" : ""}`} aria-live="polite">
      <div className="studio-top">
        <ProgressRing percent={failed ? 0 : overall} />

        <div className="studio-head">
          <span className="studio-eyebrow">
            {failed ? "Something went wrong"
              : queued ? "In the queue"
              : `Step ${currentIdx + 1} of ${STAGES.length}`}
          </span>
          <h3>
            {failed ? "We couldn't finish this one"
              : queued ? (ahead ? `${ahead} job${ahead === 1 ? "" : "s"} ahead of you`
                                : "Starting now")
              : STAGES[currentIdx].label}
          </h3>
          <p className="studio-blurb">
            {failed ? "The details are below — most of the time it's an unsupported or speechless video."
              : queued ? "Your place is saved. You can close this page — we'll keep it and add the clips to your Library."
              : STAGES[currentIdx].blurb}
          </p>
          <div className="studio-meta">
            <span className="studio-live">
              <span className="live-dot" aria-hidden="true" />
              {friendlyProgress(job.progress_message, job.status)}
            </span>
            <span className="studio-timer">
              <Elapsed since={startedAt} /> elapsed
            </span>
          </div>
        </div>
      </div>

      {!failed && (
        <div className="studio-away-note">
          <strong>Safe to leave</strong>
          <ProcessingTip />
        </div>
      )}

      <ol className="stage-rail">
        {STAGES.map((s, i) => {
          const state = failed && i === currentIdx ? "error" : stageState(s.key, job.status);
          return (
            <li key={s.key} className={`rail-item ${state}`}>
              <span className="rail-dot">
                {state === "done" ? (
                  <svg viewBox="0 0 24 24" width="12" height="12" fill="none">
                    <path d="m5 13 4 4L19 7" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                ) : state === "error" ? "!" : i + 1}
              </span>
              <span className="rail-label">{s.label}</span>
            </li>
          );
        })}
      </ol>

      {!failed && (
        <div className="skeleton-row" aria-hidden="true">
          {/* capped: at 10 clips a full row of skeletons becomes slivers */}
          {Array.from({ length: job.options?.output === "highlight" ? 1
                                : Math.min(job.options?.n_clips || 3, 5) }).map((_, i) => (
            <div className={`skel-card ${job.options?.output === "highlight" ? "wide" : ""}`}
                 key={i} style={{ animationDelay: `${i * 160}ms` }}>
              <div className="skel-shimmer" />
              <span className="skel-line w70" />
              <span className="skel-line w45" />
            </div>
          ))}
        </div>
      )}

      {failed && (
        <>
          <div className="error-box">{job.error}</div>
          <button className="btn btn-primary" onClick={onCancel} style={{ marginTop: 18 }}>
            Try another video
          </button>
        </>
      )}
    </div>
  );
}

// Layout and captions are separate choices. These cards only decide how the
// source fills the frame; the caption picker in Preferences decides the words.
const TEMPLATES = [
  {
    /* The default. A creator pasting a link rarely knows or cares which of the
       other six is "right"; the pipeline can look at the footage and decide, and
       every existing template is still one click away as an override. */
    id: "auto", name: "Auto", frame: "auto", scene: "scene-black",
    words: ["Here", "is", "your"], color: "#d8f24e", capClass: "cap-classic",
    note: "Detects podcast, gameplay, facecam",
  },
  {
    id: "classic", name: "Classic", frame: "blur", scene: "scene-neutral",
    words: ["Here", "is", "your"], color: "#d8f24e", capClass: "cap-classic",
    note: "Full video · soft background",
  },
  {
    id: "fitvideo", name: "Fit video", frame: "fit", scene: "scene-black",
    words: ["Here", "is", "your"], color: "#ffffff", chip: "#2b6cf6",
    capClass: "cap-fitbox", note: "Full video · solid bars",
  },
  {
    id: "tech", name: "Tech", frame: "fill", scene: "scene-azure",
    words: ["Here", "is", "your"], color: "#bfe9ff", capClass: "cap-tech",
    note: "Fill frame · close crop",
  },
  {
    id: "business", name: "Business", frame: "fill", scene: "scene-warm",
    words: ["Here", "is", "your"], color: "#1e1a1a", capClass: "cap-business",
    note: "Fill frame · warm crop",
  },
  {
    id: "gameplay", name: "Split screen", frame: "gameplay", scene: "scene-sunset",
    words: ["HERE", "IS", "YOUR"], color: "#3ad43a", capClass: "cap-gameplay",
    pop: true, note: "Talking head over a gameplay loop",
  },
  {
    id: "podcast", name: "Podcast", frame: "podcast", scene: "scene-studio",
    words: ["Here", "is", "your"], color: "#e8f0f5", capClass: "cap-podcast",
    note: "Keep both speakers visible",
  },
];

/** A framing-only preview. Caption samples do not belong here because the
 * caption preset is selected independently and always wins in the render. */
function TemplatePreview({ template }) {
  const isSplit = template.frame === "gameplay";
  /* A podcast is a two-shot -- that IS the reason this template exists, since
     every other one zooms in far enough to lose the second person. A card
     showing one speaker would be advertising the wrong thing. */
  const isPodcast = template.frame === "podcast";
  /* Classic composites a subject band over a blurred copy of itself. The card
     used to draw one flat scene edge to edge, so it showed neither the blur nor
     the band -- which meant changing how much of the frame the video gets
     changed nothing visible here. */
  const isBlur = template.frame === "blur";
  /* Auto is not one composition: it is the choice between them. The card shows
     the gaming stack -- gameplay, webcam, caption band -- because that is the
     one no other card on the row depicts. */
  const isAuto = template.frame === "auto";
  return (
    <span className={`tpl-frame ${isSplit ? "is-split" : ""} ${isPodcast ? "is-podcast" : ""} ${isBlur ? "is-blur" : ""} ${isAuto ? "is-auto" : ""} ${template.scene}`}>
      <span className="tpl-scene" aria-hidden="true">
        <span className="tpl-head" />
        <span className="tpl-body" />
        {isPodcast && <><span className="tpl-head two" />
                       <span className="tpl-body two" /></>}
      </span>
      {isSplit && (
        <span className="tpl-game" aria-hidden="true">
          <span className="tpl-game-lane" />
          <span className="tpl-game-lane" />
          <span className="tpl-game-player" />
        </span>
      )}
      {isAuto && (
        <span className="tpl-auto" aria-hidden="true">
          <span className="tpl-auto-game"><i /><i /></span>
          <span className="tpl-auto-cam"><b /></span>
          <span className="tpl-auto-caption"><i /><i /></span>
        </span>
      )}
    </span>
  );
}

const MARQUEE_ITEMS = [
  "Best moments picked",
  "Vertical, square or wide",
  "Caption styles included",
  "Edit before you post",
  "No editing experience needed",
  "Keep the real voice",
  "Add your logo or handle",
  "Link or upload",
];

const HEADLINE = [
  { text: "Turn", cls: "" },
  { text: "any", cls: "" },
  { text: "video", cls: "" },
  { text: "into", cls: "" },
  { text: "clips", cls: "serif" },
  { text: "that", cls: "serif" },
  { text: "go", cls: "serif" },
  { text: "viral.", cls: "serif grad" },
];

/* Segmented control. The thumb is a sibling of the labels rather than a
   background on the selected one, so the selection can slide -- and hovering
   the unselected side tints text instead of painting a second selection. */
function SourceTabs({ value, onChange, className = "" }) {
  return (
    <div className={`source-tabs ${className}`} data-active={value}
         role="tablist" aria-label="Video source" data-testid="source-tabs">
      <span className="tab-thumb" aria-hidden="true" />
      {[["link", "Link"], ["upload", "Upload"]].map(([id, label]) => (
        <button key={id} type="button" role="tab" aria-selected={value === id}
                className={value === id ? "on" : ""}
                data-testid={`source-tab-${id}`}
                onClick={() => onChange(id)}>
          {label}
        </button>
      ))}
    </div>
  );
}

/* A file field needs somewhere to drop a file. Upload mode used to reuse the
   URL bar's single row, so the only way in was a "Browse" button that looked
   like part of a text input. */
function DropZone({ file, onFile, disabled }) {
  const [over, setOver] = useState(false);
  const inputRef = useRef(null);

  function take(list) {
    const f = Array.from(list || []).find((x) => x.type.startsWith("video/"));
    if (f) onFile(f);
  }

  return (
    <label
      className={`dropzone ${over ? "over" : ""}`}
      data-testid="dropzone"
      onDragOver={(e) => { e.preventDefault(); if (!disabled) setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        if (!disabled) take(e.dataTransfer.files);
      }}
    >
      <input ref={inputRef} type="file" disabled={disabled}
             accept="video/mp4,video/quicktime,video/x-m4v,video/webm,video/x-matroska,.mp4,.mov,.m4v,.webm,.mkv"
             onChange={(e) => take(e.target.files)} data-testid="dropzone-input" />
      <span className="dropzone-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
          <path d="M12 16V4m0 0 4.5 4.5M12 4 7.5 8.5M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15"
                stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                strokeLinejoin="round" />
        </svg>
      </span>
      {file ? (
        <>
          <strong>{file.name}</strong>
          <span className="dz-file">
            {Math.max(1, Math.round(file.size / (1024 * 1024)))} MB · click to replace
          </span>
        </>
      ) : (
        <>
          <strong>Drop a video here, or <em>browse</em></strong>
          <span>MP4, MOV, WebM or MKV</span>
        </>
      )}
    </label>
  );
}

function SourcePreview({ preview, loading, error, uploadUrl, uploadFile, onUploadMetadata, onSwitchToUpload }) {
  if (uploadUrl && uploadFile) {
    return (
      <div className="source-preview" aria-label="Selected upload preview">
        <video src={uploadUrl} muted playsInline preload="metadata"
               onLoadedMetadata={(e) => onUploadMetadata?.(e.currentTarget.duration)} />
        <div className="source-preview-copy">
          <strong>{uploadFile.name}</strong>
          <span>{Math.max(1, Math.round(uploadFile.size / (1024 * 1024)))} MB · Local upload</span>
        </div>
      </div>
    );
  }
  if (loading) return <div className="source-preview loading">Checking this video…</div>;
  if (error) return (
    <div className="source-preview recovery" role="status">
      <span className="source-recovery-icon" aria-hidden="true">!</span>
      <div className="source-preview-copy">
        <strong>We couldn't check this link</strong>
        <span>Try a public video link, or upload the video file instead.</span>
      </div>
      <button type="button" className="source-recovery-action" onClick={onSwitchToUpload}>
        Upload file
      </button>
    </div>
  );
  if (!preview) return null;
  return (
    <div className="source-preview" aria-label="Link preview">
      {preview.thumbnail ? <img src={preview.thumbnail} alt="" /> : <span className="source-preview-fallback">Video</span>}
      <div className="source-preview-copy">
        <strong>{preview.title}</strong>
        <span>{[preview.creator, preview.duration != null ? fmtDuration(preview.duration) : null, preview.platform]
          .filter(Boolean).join(" · ")}</span>
      </div>
    </div>
  );
}

export default function App() {
  const [url, setUrl] = useState("");
  const [sourceMode, setSourceMode] = useState("link");
  const [uploadFile, setUploadFile] = useState(null);
  const [linkPreview, setLinkPreview] = useState(null);
  const [previewedUrl, setPreviewedUrl] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [uploadPreviewUrl, setUploadPreviewUrl] = useState("");
  const [uploadDuration, setUploadDuration] = useState(null);
  const [nClips, setNClips] = useState(3);
  const [burnSubtitles, setBurnSubtitles] = useState(true);
  // With captions off, keyword phrases + emoji still pop up unless this is off.
  const [keywordCaptions, setKeywordCaptions] = useState(true);
  const [burnTitle, setBurnTitle] = useState(true);
  const [autoCensor, setAutoCensor] = useState(true);
  const [multilingual, setMultilingual] = useState(false);
  const [template, setTemplate] = useState("auto");
  const [captionStyle, setCaptionStyle] = useState("pro_caps");
  const [ratio, setRatio] = useState("9:16");
  const [lengthPref, setLengthPref] = useState("any");
  /* What kind of video this is. "auto" lets the server decide from the title
     and how much of the runtime is speech; naming it picks the rubric outright. */
  const [genre, setGenre] = useState("auto");
  const [intent, setIntent] = useState("");
  const [tightenPauses, setTightenPauses] = useState(false);
  /* Paid extras. Both default off: the cheaper path is what every job has
     always had, and defaulting either on would charge for something nobody
     chose. Priced in credits rather than gated, so Free can try them. */
  const [highQuality, setHighQuality] = useState(false);
  const [advancedModel, setAdvancedModel] = useState(false);
  /* Clips and a highlight video are different products: N short standalone
     posts, or ONE longer video of the best parts. Each has its own settings. */
  const [output, setOutput] = useState("clips");
  const [highlightMinutes, setHighlightMinutes] = useState(null);   // null = auto
  const [highlightMemes, setHighlightMemes] = useState(true);
  const [highlightTags, setHighlightTags] = useState(true);
  const [highlightMusic, setHighlightMusic] = useState(true);
  // The studio is a 3-step wizard now: 1 source, 2 preferences, 3 review.
  const [step, setStep] = useState(1);
  // The feature someone just reached for, so the upgrade dialog can name it.
  const [upgradeFor, setUpgradeFor] = useState(null);
  const [pricingOpen, setPricingOpen] = useState(false);
  const [sharing, setSharing] = useState(null);
  const [editing, setEditing] = useState(null);   // { clip, index } while the editor is open
  const [showDash, setShowDash] = useState(false);
  const [studioOpen, setStudioOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const accountRef = useRef(null);
  const { getToken: getTurnstileToken, Turnstile } = useTurnstile();

  useEffect(() => {
    if (!accountOpen) return;
    function onDown(e) {
      if (!accountRef.current?.contains(e.target)) setAccountOpen(false);
    }
    function onKey(e) {
      if (e.key === "Escape") setAccountOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [accountOpen]);
  const [user, setUser] = useState(null);
  const [justUpgraded, setJustUpgraded] = useState(null);
  const [paymentStatus, setPaymentStatus] = useState(null); // "processing" | "success" | "failed" | null
  const [authOpen, setAuthOpen] = useState(false);
  const [authMode, setAuthMode] = useState("signin");
  const [authIntent, setAuthIntent] = useState(null);
  const [theme, toggleTheme] = useTheme();
  const toast = useToast();

  function showAppView(view, push = true) {
    setShowDash(view === "dashboard");
    setStudioOpen(view === "studio");
    if (push && window.history.state?.klipcutView !== view) {
      const hash = view === "dashboard" ? "#clips" : "#studio";
      window.history.pushState(
        { ...window.history.state, klipcutView: view },
        "",
        hash,
      );
    }
  }

  function leaveAppView() {
    if (window.history.state?.klipcutView) window.history.back();
    else showAppView(null, false);
  }

  /* A refresh, or a shared #clips / #studio link, used to drop the user on the
     marketing page: views were restored from history.state only, which a fresh
     page load does not have. Restore from the hash once the session is known. */
  const restoredFromHash = useRef(false);
  useEffect(() => {
    if (!user || restoredFromHash.current) return;
    restoredFromHash.current = true;
    const view = { "#clips": "dashboard", "#studio": "studio" }[window.location.hash];
    if (!view) return;
    window.history.replaceState({ ...window.history.state, klipcutView: view }, "",
                                window.location.hash);
    showAppView(view, false);
  }, [user]);

  useEffect(() => {
    function restoreView() {
      const view = window.history.state?.klipcutView;
      setShowDash(view === "dashboard");
      setStudioOpen(view === "studio");
      if (view !== "studio") setEditing(null);
    }
    window.addEventListener("popstate", restoreView);
    return () => window.removeEventListener("popstate", restoreView);
  }, []);

  // Restores the session from a stored token; clears it if the token expired.
  // Polar redirects here after payment. Its webhook can arrive a moment later,
  // so refresh the account a few times before removing the return marker.
  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams(window.location.search);

    const returningFromPayment = params.get("payment") === "success"
      || params.has("customer_session_token")
      || params.has("checkout_id");
    const checkoutId = params.get("checkout_id");

    /* Clean URL immediately — we've captured what we need */
    if (returningFromPayment) {
      params.delete("payment");
      params.delete("checkout_id");
      params.delete("customer_session_token");
      const query = params.toString();
      window.history.replaceState(window.history.state, "",
        `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);
    }

    async function confirmWithRetry(id, retries = 5) {
      const token = localStorage.getItem("clipper_token");
      if (!token) return null;
      // The snapshot taken before redirecting (Pricing.jsx). Sending it lets the
      // server confirm THIS payment by the account actually changing, instead of
      // falsely reporting success off any other recent grant.
      let before = null;
      try { before = JSON.parse(window.sessionStorage.getItem("billingBeforeCheckout") || "null"); }
      catch { /* private mode: server falls back to the recent-grant check */ }
      for (let i = 0; i <= retries; i++) {
        if (i > 0) await new Promise((r) => setTimeout(r, 2000));
        if (cancelled) return null;
        try {
          const resp = await fetch(`${API_BASE}/billing/confirm`, {
            method: "POST",
            headers: { "Content-Type": "application/json",
                       Authorization: `Bearer ${token}` },
            body: JSON.stringify({ checkout_id: id, before }),
          });
          if (!resp.ok) continue;
          const result = await resp.json();
          if (result.status === "applied" || result.status === "already_applied")
            return result;
        } catch { /* retry */ }
      }
      return null;
    }

    async function boot() {
      if (returningFromPayment) {
        setPaymentStatus("processing");

        // Dodo redirects back with ?payment=success but no checkout_id; the
        // webhook does the granting, and confirm just waits for it to land, so
        // we no longer depend on a provider-specific id being present.
        let result = null;
        try { result = await confirmWithRetry(checkoutId); }
        catch { /* confirm failed entirely */ }
        if (cancelled) return;

        let freshUser = null;
        try { freshUser = await fetchMe(); }
        catch { /* fetchMe network error */ }
        if (cancelled) return;
        if (freshUser) {
          identifyUser(freshUser);
          setUser(freshUser);
        }

        if (result) {
          try { window.sessionStorage.removeItem("billingBeforeCheckout"); }
          catch { /* ignore */ }
          setPaymentStatus("success");
          setJustUpgraded(freshUser || { plan: result.plan, credits: result.credits });
        } else {
          setPaymentStatus("failed");
        }
        return;
      }

      /* Normal boot — not returning from payment */
      try {
        const u = await fetchMe();
        if (!cancelled) {
          if (u) identifyUser(u);
          setUser(u);
        }
      } catch { /* offline or server down */ }
    }

    boot();
    return () => { cancelled = true; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  /* Signed out means no plan, which means the free entitlement set. Read from
     the server's list rather than comparing plan names here, so adding a tier
     is a backend-only change. */
  const plan = usePlan(user);
  const canMultilingual = plan.can("multilingual");

  /* Clip count is a ceiling, not a switch, so it has to be corrected rather
     than blocked: the default of 3 is above the free ceiling of 2, and someone
     who signs out of a paid plan would otherwise keep a choice the server now
     rejects -- discovered only after pressing Generate. */
  useEffect(() => {
    setNClips((n) => Math.min(n, plan.limits.max_clips));
  }, [plan.limits.max_clips]);

  // Pause tightening is paid, so it defaults on only where it is available.
  useEffect(() => {
    if (plan.can("tighten_pauses")) setTightenPauses(true);
  }, [plan.plan, plan.isAdmin]);

  function openAuth(mode = "signin", intent = null) {
    setAuthMode(mode);
    setAuthIntent(intent);
    setAuthOpen(true);
  }

  function handleAuthed(nextUser) {
    const pending = authIntent;
    identifyUser(nextUser);
    setUser(nextUser);
    setAuthIntent(null);
    if (pending === "studio") enterStudio();
  }

  function requestUpgrade(feature = "general") {
    setUpgradeFor(feature);
  }

  /* Copies a public link to one clip. A 402 is the plan boundary, not an error:
     turn it into the upgrade dialog rather than a red toast, because the person
     just told us exactly which feature they wanted. */
  async function shareClip(index) {
    if (!user) { openAuth("signin"); return; }
    if (!plan.can("share_pages")) { requestUpgrade("share_pages"); return; }
    setSharing(index);
    try {
      const { path } = await createShare(job.id, index);
      if (isPostHogConfigured) posthog.capture("share_link_created");
      const link = `${window.location.origin}${path}`;
      try {
        await navigator.clipboard.writeText(link);
        toast("Share link copied", { detail: link, tone: "success" });
      } catch {
        // Clipboard needs a permission this browser did not grant; the link is
        // useless if we swallow it, so show it instead.
        toast("Share link ready", { detail: link, tone: "success", ttl: 15000 });
      }
    } catch (e) {
      if (e.status === 402) requestUpgrade("share_pages");
      else toast("Couldn't create a share link", { detail: e.message, tone: "error" });
    } finally {
      setSharing(null);
    }
  }

  // Persistence makes a job reachable again after the tab is closed, so ?job=<id>
  // reopens one. Also how a shared or bookmarked result gets back on screen.
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("job");
    if (!id) return;
    getJob(id).then((j) => {
      setJob(j);
      window.history.replaceState(
        { ...window.history.state, klipcutView: null },
        "",
        window.location.href,
      );
      showAppView("studio");
    }).catch(() => {});
  }, []);
  // Written by backend/make_previews.py; absent on a fresh checkout, in which
  // case each card falls back to a single <id>.mp4 and then the CSS mockup.
  const [previewManifest, setPreviewManifest] = useState({});

  useEffect(() => {
    fetch("/previews/manifest.json")
      .then((r) => (r.ok ? r.json() : {}))
      .then(setPreviewManifest)
      .catch(() => setPreviewManifest({}));
  }, []);
  const [job, setJob] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  /* 0..1 while a local file is going up, null when nothing is uploading or the
     browser cannot size the body. Kept apart from `submitting` because the
     upload is only the first part of starting a job -- the gates and probes
     that follow have no percentage, and conflating them would strand the bar. */
  const [uploadPct, setUploadPct] = useState(null);
  const [submitError, setSubmitError] = useState(null);
  const [scrolled, setScrolled] = useState(false);
  const [startedAt, setStartedAt] = useState(Date.now());
  const [progress, setProgress] = useState(0);
  const [openFaq, setOpenFaq] = useState(0);
  const pollRef = useRef(null);
  const trackedJobRef = useRef(null);
  const resultsRef = useRef(null);
  const urlbarRef = useRef(null);

  const jobActive = job && !["done", "failed"].includes(job.status);
  const sourceVerified = sourceMode === "upload"
    ? Boolean(uploadFile && Number.isFinite(uploadDuration) && uploadDuration > 0)
    : Boolean(linkPreview && previewedUrl === url.trim() && !previewLoading && !previewError);
  const linkTooLong = sourceMode === "link" && linkPreview?.duration > plan.limits.max_source_minutes * 60;
  const uploadTooLong = sourceMode === "upload" && uploadDuration > plan.limits.max_source_minutes * 60;
  const sourceTooLong = linkTooLong || uploadTooLong;
  const sourceCanContinue = sourceVerified && !sourceTooLong;

  useEffect(() => {
    if (!uploadFile) { setUploadPreviewUrl(""); setUploadDuration(null); return undefined; }
    setUploadDuration(null);
    const objectUrl = URL.createObjectURL(uploadFile);
    setUploadPreviewUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [uploadFile]);

  useEffect(() => {
    const link = url.trim();
    if (sourceMode !== "link" || !/^https?:\/\//i.test(link)) {
      setLinkPreview(null);
      setPreviewedUrl("");
      setPreviewLoading(false);
      setPreviewError("");
      return undefined;
    }
    // A preview only belongs to the exact URL it was fetched for. As soon as
    // the field changes, it cannot be used to unlock the next step.
    setLinkPreview(null);
    setPreviewedUrl("");
    setPreviewError("");
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setPreviewLoading(true);
      setPreviewError("");
      getSourcePreview(link, controller.signal)
        .then((data) => {
          setLinkPreview(data);
          setPreviewedUrl(link);
        })
        .catch((err) => {
          if (err.name !== "AbortError") {
            setLinkPreview(null);
            setPreviewError(err.message);
          }
        })
        .finally(() => { if (!controller.signal.aborted) setPreviewLoading(false); });
    }, 650);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [url, sourceMode]);

  useReveal();

  /* nav shrink + scroll progress */
  useEffect(() => {
    const onScroll = () => {
      setScrolled(window.scrollY > 24);
      const max = document.documentElement.scrollHeight - window.innerHeight;
      setProgress(max > 0 ? window.scrollY / max : 0);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (!job || !jobActive) return;
    pollRef.current = setInterval(async () => {
      try {
        const fresh = await getJob(job.id);
        setJob(fresh);
        if (["done", "failed"].includes(fresh.status)) {
          clearInterval(pollRef.current);
          if (fresh.status === "done" && trackedJobRef.current === fresh.id) {
            if (isPostHogConfigured) {
              posthog.capture("clip_generation_completed", {
                clip_count: fresh.clips?.length || 0,
              });
            }
            trackedJobRef.current = null;
          }
        }
      } catch {
        // transient poll failure -- keep trying
      }
    }, 1200);
    return () => clearInterval(pollRef.current);
  }, [job?.id, jobActive]);

  useEffect(() => {
    if (job?.status === "done" && resultsRef.current) {
      resultsRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
      if (job.options?.output === "highlight") {
        toast("Highlight video ready", { detail: "Watch it or download it below.", tone: "success" });
      } else {
        toast(`${job.clips?.length || 0} clip${job.clips?.length === 1 ? "" : "s"} ready`, {
          detail: "Preview, trim, or download them below.", tone: "success" });
      }
    }
    if (job?.status === "failed") {
      toast("That video couldn't be clipped", { detail: job.error, tone: "error" });
    }
  }, [job?.status]);

  async function handleSubmit() {
    if (!sourceCanContinue || submitting) return;
    if (!user) { openAuth("signin", "studio"); return; }
    if (sourceTooLong) { requestUpgrade("length"); return; }
    setSubmitting(true);
    setSubmitError(null);
    setJob(null);
    setStartedAt(Date.now());
    try {
      const turnstileToken = await getTurnstileToken();
      const options = {
        nClips,
        mode: "original",
        burnSubtitles,
        keywordCaptions,
        burnTitle,
        autoCensor,
        // Never send a gated flag on a plan that does not include it: the server
        // rejects the whole job rather than quietly downgrading it.
        multilingual: multilingual && canMultilingual,
        tightenPauses: tightenPauses && plan.can("tighten_pauses"),
        voice: "onyx",
        language: "English",
        template,
        captionStyle,
        ratio,
        lengthPref,
        genre,
        intent: intent.trim(),
        highQuality,
        advancedModel,
        output,
        highlightMinutes: output === "highlight" ? highlightMinutes : null,
        highlightMemes,
        highlightTags,
        highlightMusic,
      };
      const result = sourceMode === "upload"
        ? await uploadJob(uploadFile, {
            url: "upload://local-video",
            n_clips: options.nClips,
            mode: options.mode,
            burn_subtitles: options.burnSubtitles,
            keyword_captions: options.keywordCaptions,
            burn_title: options.burnTitle,
            auto_censor: options.autoCensor,
            multilingual: options.multilingual,
            tighten_pauses: options.tightenPauses,
            voice: options.voice,
            language: options.language,
            template: options.template,
            caption_style: options.captionStyle,
            ratio: options.ratio,
            length_pref: options.lengthPref,
            genre: options.genre,
            intent: options.intent,
            high_quality: options.highQuality,
            advanced_model: options.advancedModel,
            output: options.output,
            highlight_minutes: options.highlightMinutes,
            highlight_memes: options.highlightMemes,
            highlight_tags: options.highlightTags,
            highlight_music: options.highlightMusic,
          }, setUploadPct, turnstileToken)
        : await submitJob({ url: url.trim(), ...options, turnstileToken });
      const { job_id } = result;
      trackedJobRef.current = job_id;
      if (isPostHogConfigured) {
        posthog.capture("clip_generation_started", {
          source_type: sourceMode,
          clip_count: nClips,
          template,
          ratio,
          captions_enabled: burnSubtitles,
          high_quality: highQuality,
          advanced_model: advancedModel,
        });
      }
      const fresh = await getJob(job_id);
      setJob(fresh);
      if (fresh.status === "done" && trackedJobRef.current === fresh.id) {
        if (isPostHogConfigured) {
          posthog.capture("clip_generation_completed", {
            clip_count: fresh.clips?.length || 0,
          });
        }
        trackedJobRef.current = null;
      }
    } catch (e) {
      if (e.status === 402) {
        requestUpgrade(e.detail?.toLowerCase().includes("source") ? "length" : "clips");
      } else {
        setSubmitError(e.message);
        toast("We couldn't start that clip", { detail: e.message, tone: "error" });
      }
    } finally {
      setSubmitting(false);
      setUploadPct(null);
    }
  }

  function focusUrlbar() {
    urlbarRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => urlbarRef.current?.querySelector("input")?.focus(), 450);
  }

  /* One entry point into the studio, so the hero, the CTA and Enter in the URL
     field all behave the same -- they used to disagree about whether Enter
     submitted a job immediately or opened the settings step. */
  function enterStudio() {
    showAppView("studio");
    // A job already running means this is a way back to its progress, not a new
    // run -- leaving it disabled stranded anyone who closed the studio mid-job.
    // With a source already in hand, skip straight to the choices.
    if (!jobActive) {
      // A FINISHED job is not where Continue should land. The studio shows a
      // done job's results instead of the wizard, so a new video pasted after
      // a run opened the previous run's clips until the page was reloaded.
      // Old results stay one click away in My clips.
      if (job) setJob(null);
      setSubmitError(null);
      setStep(sourceCanContinue ? 2 : 1);
    }
  }

  function openStudio() {
    if (!user && !jobActive) {
      openAuth("signin", "studio");
      return;
    }
    enterStudio();
  }

  function openPricingSection() {
    setPricingOpen(false);
    setUpgradeFor(null);
    setStudioOpen(false);
    setShowDash(false);
    setEditing(null);
    window.history.pushState(
      { ...window.history.state, klipcutView: null },
      "",
      "#pricing",
    );
    requestAnimationFrame(() => {
      document.getElementById("pricing")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  const continueBtn = (
    <button
      className="btn btn-primary btn-shine source-continue"
      onClick={openStudio}
      disabled={submitting || (!jobActive && !sourceCanContinue)}
      title={!jobActive && !sourceCanContinue
        ? (sourceMode === "upload"
          ? (uploadTooLong ? "Choose a plan for longer videos"
            : !uploadFile ? "Choose a video file first" : "Checking the video file")
          : (!url.trim() ? "Paste a public video link first"
            : linkTooLong ? "Choose a plan for longer videos"
            : previewLoading ? "Checking this video" : previewError
              ? "Use another link or upload the video file" : "Waiting for a video preview"))
        : undefined}
      data-testid="hero-continue-btn"
    >
      {submitting ? "Starting..." : jobActive ? "View progress" : "Continue"}
      {!submitting && !jobActive && (
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" aria-hidden="true">
          <path d="M5 12h14m0 0-6-6m6 6-6 6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )}
    </button>
  );

  return (
    <>
      <div className="scroll-progress" style={{ transform: `scaleX(${progress})` }} aria-hidden="true" />

      {/* ---------- Nav ---------- */}
      <header className={`nav-wrap ${scrolled ? "scrolled" : ""} ${studioOpen || showDash ? "stowed" : ""}`}>
        <nav className="nav-pill">
          <a className="logo" href="#top">
            <span className="logo-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none">
                <path d="M6 4.5v15l13-7.5-13-7.5Z" fill="currentColor" />
              </svg>
            </span>
            KlipCut
          </a>
          <div className="nav-links">
            <a href="#how">What you do</a>
            <a href="#features">Features</a>
            <a href="#faq">FAQ</a>
            <a href="#pricing">Pricing</a>
          </div>
          <div className="nav-cta">
            <ThemeToggle theme={theme} onToggle={toggleTheme} />

            {user ? (
              /* Signed in: balance stays visible because it is the one number
                 that changes what you can do next. Everything else folds into
                 the account menu -- five separate nav buttons wrapped onto two
                 lines as soon as the bar shrank on scroll. */
              <div className="account" ref={accountRef}>
                {/* The way back to your own clips. It used to live only behind
                    the avatar menu and the credit pill, so a returning user
                    landed on the marketing page with no visible route to
                    their work. */}
                <button className="btn btn-ghost btn-sm nav-myclips"
                        onClick={() => showAppView("dashboard")}>
                  My clips
                </button>
                <button
                  className="credit-pill"
                  onClick={() => showAppView("dashboard")}
                  title={`${user.credits} credits left`}
                >
                  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" aria-hidden="true">
                    <path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12l1-8.5Z" fill="currentColor" />
                  </svg>
                  {user.credits}
                </button>

                <button
                  className={`avatar ${accountOpen ? "on" : ""}`}
                  onClick={() => setAccountOpen((v) => !v)}
                  aria-haspopup="menu"
                  aria-expanded={accountOpen}
                  aria-label="Account menu"
                >
                  {user.email[0].toUpperCase()}
                </button>

                {accountOpen && (
                  <div className="account-menu" role="menu">
                    <div className="account-who">
                      <strong>{user.email}</strong>
                      <span>{user.credits} credits left</span>
                    </div>
                    <button role="menuitem" onClick={() => { showAppView("dashboard"); setAccountOpen(false); }}>
                      My clips
                    </button>
                    <button role="menuitem" onClick={() => {
                      setAccountOpen(false);
                      setPricingOpen(true);
                    }}>
                      Plans &amp; credits
                    </button>
                    <button role="menuitem" className="danger" onClick={() => {
                      if (isPostHogConfigured) posthog.reset();
                      logout(); setUser(null); setAccountOpen(false); setShowDash(false);
                    }}>
                      Sign out
                    </button>
                  </div>
                )}
              </div>
            ) : (
              <>
                <button className="btn btn-ghost btn-sm" onClick={() => openAuth("signin")}>
                  Sign in
                </button>
                <Magnetic className="btn btn-primary btn-sm" onClick={focusUrlbar} strength={0.25}>
                  Try KlipCut
                </Magnetic>
              </>
            )}
          </div>
        </nav>
      </header>

      <main id="top">
        {/* ---------- Hero ---------- */}
        <section className="hero">
          <div className="hero-blobs" aria-hidden="true">
            <span className="blob b1" />
            <span className="blob b2" />
          </div>

          <div className="container hero-inner">
            <span className="hero-badge rise r0">
              <span className="badge-dot" aria-hidden="true" />
              AI clipping studio — start free, no card needed
            </span>

            <h1 className="hero-title" aria-label="Turn any video into clips that go viral.">
              {HEADLINE.map((w, i) => (
                <Fragment key={i}>
                  <span className="word-clip">
                    <span className={`word ${w.cls}`} style={{ animationDelay: `${0.12 + i * 0.07}s` }}>
                      {w.text}
                    </span>
                  </span>
                  {/* a real space between words, outside the clipping mask, so the
                      headline stays selectable and copies as normal text */}
                  {i < HEADLINE.length - 1 ? " " : null}
                </Fragment>
              ))}
            </h1>

            <p className="sub rise r4">
              Add a video, choose how your clips should look, and let KlipCut find the moments worth posting.
              You can change the captions, layout and branding before you download.
            </p>

            {/* The working element: URL bar */}
            <div className="urlbar-wrap rise r5" ref={urlbarRef}>
              <div className="urlbar-glow" aria-hidden="true" />
              <SourceTabs value={sourceMode} onChange={setSourceMode} />
              {sourceMode === "link" ? (
                <div className="urlbar">
                  <svg className="urlbar-icon" viewBox="0 0 24 24" width="20" height="20" fill="none" aria-hidden="true">
                    <path d="M10.6 13.4a4.5 4.5 0 0 0 6.36 0l3-3a4.5 4.5 0 1 0-6.36-6.36l-1.5 1.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                    <path d="M13.4 10.6a4.5 4.5 0 0 0-6.36 0l-3 3a4.5 4.5 0 1 0 6.36 6.36l1.5-1.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                  </svg>
                  <input type="url" placeholder="Paste a YouTube, X or TikTok link..." value={url}
                         onChange={(e) => setUrl(e.target.value)}
                         onKeyDown={(e) => e.key === "Enter" && sourceCanContinue && openStudio()}
                         disabled={jobActive} aria-label="Public video URL"
                         data-testid="hero-url-input" />
                  {continueBtn}
                </div>
              ) : (
                <>
                  <DropZone file={uploadFile} onFile={setUploadFile} disabled={jobActive} />
                  <div className="dropzone-actions" style={{ justifyContent: "center" }}>
                    {continueBtn}
                  </div>
                </>
              )}
              {sourceMode === "link" && (
                <SourcePreview preview={linkPreview} loading={previewLoading}
                               error={previewError} uploadUrl="" uploadFile={null}
                               onSwitchToUpload={() => setSourceMode("upload")} />
              )}
              {sourceMode === "upload" && (
                <SourcePreview preview={null} loading={false} error="" uploadUrl={uploadPreviewUrl}
                               uploadFile={uploadFile} onUploadMetadata={setUploadDuration} />
              )}
              {sourceTooLong && (
                <div className="hero-source-limit" role="status">
                  <span>This video is longer than the {plan.limits.max_source_minutes}-minute limit on your plan.</span>
                  <button type="button" onClick={() => setPricingOpen(true)}>View plans</button>
                </div>
              )}

            </div>

            {/* Human check before a job spends money. Renders only when the
                backend has Turnstile configured; otherwise it is a no-op. */}
            <div className="turnstile-row" style={{ display: "flex", justifyContent: "center", marginTop: 14 }}>
              <Turnstile />
            </div>

            {/* Deliberately OUTSIDE .urlbar-wrap: that wrapper owns a blurred
                spinning halo sized to itself, so anything nested inside it gets
                bathed in the glow on hover. */}
            {/* Interactive clip collage */}
          </div>
        </section>

        {/* ---------- Marquee ---------- */}
        <div className="marquee" aria-hidden="true">
          <div className="marquee-track">
            {[...MARQUEE_ITEMS, ...MARQUEE_ITEMS].map((item, i) => (
              <span className="marquee-item" key={i}>
                <span className="marquee-star">✦</span>
                {item}
              </span>
            ))}
          </div>
        </div>

        {/* ---------- Stats ---------- */}
        <section className="stats container">
          {[
            { num: <CountUp to={10} suffix="×" />, label: "Less time spent editing" },
            { num: "9:16", label: "Vertical, ready for Shorts" },
            { num: <CountUp to={100} suffix="%" />, label: "Captions follow the voice" },
            { num: "0", label: "Editing skills required" },
          ].map((s, i) => (
            <div className="stat reveal" key={i} style={{ transitionDelay: `${i * 70}ms` }}>
              <span className="stat-num">{s.num}</span>
              <span className="stat-label">{s.label}</span>
            </div>
          ))}
        </section>

        {/* ---------- How it works ---------- */}
        <section className="how container" id="how">
          <div className="section-head reveal">
            <span className="section-eyebrow">What you do</span>
            <h2 className="section-title">
              From video to ready-to-post in <em className="serif grad">four steps.</em>
            </h2>
          </div>
          <div className="how-grid">
            {[
              {
                n: "01",
                title: "Add your video",
                body: "Paste a public video link or upload a video from your device.",
              },
              {
                n: "02",
                title: "Choose your look",
                body: "Pick how many clips you want, their size, length, layout and caption style.",
              },
              {
                n: "03",
                title: "Get the best moments",
                body: "KlipCut finds the strongest parts and turns them into complete, easy-to-watch clips.",
              },
              {
                n: "04",
                title: "Edit and download",
                body: "Change the words, colours, timing, logo or speed, then export a clip ready to post.",
              },
            ].map((card, i) => (
              <div className="how-card reveal" key={card.n} style={{ transitionDelay: `${i * 80}ms` }}>
                <span className="how-num">{card.n}</span>
                <span className="how-line" aria-hidden="true" />
                <h3>{card.title}</h3>
                <p>{card.body}</p>
              </div>
            ))}
          </div>
        </section>

        {/* ---------- Features (dark band, spotlight cards) ---------- */}
        <section className="features" id="features">
          <div className="container">
            <div className="section-head reveal">
              <span className="section-eyebrow lime">Why KlipCut</span>
              <h2 className="section-title light">
                Everything you need,
                <br />
                <em className="serif">nothing you don't.</em>
              </h2>
            </div>
            <div className="feature-grid">
              {[
                {
                  icon: "◉",
                  title: "The moments worth posting",
                  body: "KlipCut looks for strong openings, useful points and satisfying endings.",
                },
                {
                  icon: "▥",
                  title: "Layouts for every platform",
                  body: "Make vertical, square or wide clips without losing the important part of the video.",
                },
                {
                  icon: "≡",
                  title: "Caption styles that feel different",
                  body: "Choose karaoke, bold, clean, social, cinematic and more before you generate.",
                },
                {
                  icon: "◎",
                  title: "A real editor when you need it",
                  body: "Fix caption text, trim the clip, move elements, add a logo and preview every change.",
                },
                {
                  icon: "▸",
                  title: "Cleaner pacing",
                  body: "Remove long pauses and keep the speaker's real voice and energy.",
                },
                {
                  icon: "⇣",
                  title: "Links and uploads",
                  body: "Start from a supported public link or upload your own video file.",
                },
                {
                  icon: "◆",
                  title: "Keep your brand on every clip",
                  body: "Add your logo, handle and colors once and KlipCut carries them across every clip it makes.",
                },
                {
                  icon: "⚑",
                  title: "Multilingual captions",
                  body: "Auto-detect the spoken language and generate accurate captions, even when it switches mid-video.",
                },
              ].map((f, i) => (
                <SpotlightCard className="feature-card reveal" key={f.title} style={{ transitionDelay: `${(i % 3) * 80}ms` }}>
                  <span className="feature-icon" aria-hidden="true">{f.icon}</span>
                  <h3>{f.title}</h3>
                  <p>{f.body}</p>
                </SpotlightCard>
              ))}
            </div>
          </div>
        </section>

        {/* ---------- Comparison ---------- */}
        <section className="container" id="compare">
          <div className="section-head reveal">
            <span className="section-eyebrow">How we compare</span>
            <h2 className="section-title">
              The same clips,
              <br />
              <em className="serif">on your terms.</em>
            </h2>
          </div>
          <div className="reveal" style={{ overflowX: "auto" }}>
            <table style={{
              width: "100%", borderCollapse: "collapse", minWidth: "520px",
              fontSize: "15px", color: "var(--ink)",
            }}>
              <thead>
                <tr>
                  {["", "KlipCut", "OpusClip", "Vizard.ai"].map((h, i) => (
                    <th key={h || "feat"} style={{
                      textAlign: i === 0 ? "left" : "center", padding: "14px 16px",
                      fontWeight: 600,
                      color: i === 1 ? "var(--violet)" : "var(--ink-faint)",
                      borderBottom: "1px solid var(--line)",
                    }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[
                  ["Pay only for clips you keep (per-clip credits)", true, false, false],
                  ["Buy any amount — no forced monthly plan", true, false, false],
                  ["Full editor before export (trim, captions, logo)", true, true, false],
                  ["Multilingual captions, auto-detected", true, true, true],
                  ["Vertical, square & wide layouts", true, true, true],
                  ["Upload your own file or paste a link", true, true, true],
                ].map((row, r) => (
                  <tr key={r}>
                    {row.map((cell, c) => (
                      <td key={c} style={{
                        textAlign: c === 0 ? "left" : "center", padding: "14px 16px",
                        borderBottom: "1px solid var(--line)",
                        color: c === 0 ? "var(--ink-soft)" : "inherit",
                        background: c === 1 ? "rgba(109,40,217,0.06)" : "transparent",
                      }}>
                        {c === 0 ? cell : (cell
                          ? <span style={{ color: "var(--success)", fontWeight: 700 }} aria-label="Yes">✓</span>
                          : <span style={{ color: "var(--ink-faint)" }} aria-label="No">—</span>)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            <p style={{
              marginTop: "14px", fontSize: "13px", color: "var(--ink-faint)",
            }}>
              Comparison based on publicly listed features as of 2026. Names belong to their owners.
            </p>
          </div>
        </section>

        {/* ---------- FAQ ---------- */}
        <section className="faq container" id="faq">
          <div className="section-head reveal">
            <span className="section-eyebrow">FAQ</span>
            <h2 className="section-title">
              Questions, <em className="serif grad">answered.</em>
            </h2>
          </div>
          <div className="faq-list reveal">
            {[
              {
                q: "What do I need to get started?",
                a: "Paste a supported public link or upload a video. Then choose your clip settings and press Generate.",
              },
              {
                q: "What kind of videos work best?",
                a: "Podcasts, interviews, streams, lessons and commentary work best because there are clear spoken moments to choose from.",
              },
              {
                q: "Can I change the captions and layout?",
                a: "Yes. Choose a starting style before generating, then change the text, colours, size, position, ratio or layout in the editor.",
              },
              {
                q: "How do credits work?",
                a: "One finished clip uses 10 credits. You pay for the clips you receive, not for every minute in the source video.",
              },
              {
                q: "Can I use my own branding?",
                a: "Yes. Creator lets you add a logo or social handle and removes the KlipCut watermark.",
              },
              {
                q: "Can I edit before downloading?",
                a: "Yes. Every clip opens in the editor. Free includes one edited export; Creator includes unlimited edited exports.",
              },
            ].map((item, i) => (
              <div className={`faq-item ${openFaq === i ? "open" : ""}`} key={item.q}>
                <button
                  className="faq-q"
                  onClick={() => setOpenFaq(openFaq === i ? -1 : i)}
                  aria-expanded={openFaq === i}
                >
                  {item.q}
                  <span className="faq-plus" aria-hidden="true">+</span>
                </button>
                <div className="faq-a-wrap">
                  <div className="faq-a">
                    <p>{item.a}</p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* ---------- Pricing ---------- */}
        <Pricing user={user} onRequireAuth={() => openAuth("signup")} />

        {/* ---------- Big CTA ---------- */}
        <section className="cta-band">
          <div className="container cta-inner reveal">
            <h2>
              Ready to clip your
              <br />
              <em className="serif">first video?</em>
            </h2>
            <p>Paste a link and get vertical, captioned clips in minutes.</p>
            <Magnetic className="btn btn-white btn-lg btn-shine" onClick={focusUrlbar}>
              Try KlipCut — it's free
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
                <path d="M5 12h14m0 0-6-6m6 6-6 6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </Magnetic>
          </div>
        </section>
      </main>

      <footer className="footer">
        <div className="container footer-inner">
          <span className="logo small">
            <span className="logo-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="13" height="13" fill="none">
                <path d="M6 4.5v15l13-7.5-13-7.5Z" fill="currentColor" />
              </svg>
            </span>
            KlipCut
          </span>
          <nav className="footer-links" aria-label="Footer">
            <a href="#how">How it works</a>
            <a href="#features">Features</a>
            <a href="#compare">Compare</a>
            <a href="#pricing">Pricing</a>
            <a href="#faq">FAQ</a>
          </nav>
          <span className="footer-note">Built for creators who publish often.</span>
        </div>
      </footer>


      {/* ---------- Studio: the whole make-a-clip flow, off the landing page ---------- */}
      {studioOpen && (
        <div className="workspace">
          <div className="container">
            <header className="dash-head">
              <div>
                <div className="studio-brand">
                  <span className="logo-mark" aria-hidden="true">
                    <svg viewBox="0 0 24 24" width="15" height="15" fill="none">
                      <path d="M6 4.5v15l13-7.5-13-7.5Z" fill="currentColor" />
                    </svg>
                  </span>
                  <span>KlipCut</span>
                  <em>Studio</em>
                </div>
                <h2>Make something new</h2>
                <p className="dash-sub">Add a video, choose clips or a highlight video, then review and generate.</p>
              </div>
              <button className="btn btn-ghost btn-sm" onClick={leaveAppView}>
                Exit studio
              </button>
            </header>

            {!jobActive && (!job || job.status === "failed") && (
              <StudioWizard
                step={step}
                setStep={setStep}
                source={{
                  mode: sourceMode, setMode: setSourceMode,
                  url, setUrl,
                  file: uploadFile, setFile: setUploadFile,
                  preview: linkPreview, loading: previewLoading, error: previewError,
                  tooLong: sourceTooLong, verified: sourceCanContinue,
                  uploadUrl: uploadPreviewUrl, setUploadDuration,
                }}
                prefs={{
                  nClips, setNClips, ratio, setRatio, lengthPref, setLengthPref,
                  genre, setGenre,
                  burnSubtitles, setBurnSubtitles, keywordCaptions, setKeywordCaptions,
                  burnTitle, setBurnTitle,
                  autoCensor, setAutoCensor,
                  multilingual, setMultilingual, tightenPauses, setTightenPauses,
                  template, setTemplate, intent, setIntent,
                  captionStyle, setCaptionStyle,
                  highQuality, setHighQuality,
                  advancedModel, setAdvancedModel,
                  output, setOutput, highlightMinutes, setHighlightMinutes,
                  highlightMemes, setHighlightMemes, highlightTags, setHighlightTags,
                  highlightMusic, setHighlightMusic,
                  sourceSeconds: sourceMode === "link" ? linkPreview?.duration : uploadDuration,
                }}
                plan={plan}
                templates={TEMPLATES}
                previewManifest={previewManifest}
                TemplatePreview={TemplatePreview}
                SourceTabs={SourceTabs}
                DropZone={DropZone}
                SourcePreview={SourcePreview}
                submitting={submitting}
                uploadPct={uploadPct}
                submitError={submitError}
                onGenerate={handleSubmit}
                onUpgrade={requestUpgrade}
                onViewPlans={() => setPricingOpen(true)}
                signedIn={Boolean(user)}
                onRequireAuth={() => openAuth("signin", "studio")}
              />
            )}

            {/* Working panel */}
            {job && job.status !== "done" && (
              <ProcessingStudio job={job} startedAt={startedAt} onCancel={() => setJob(null)} />
            )}
          </div>

        {/* ---------- Results ---------- */}
        {job?.status === "done" && (
          <section className="results container" ref={resultsRef}>
            <div className="results-head">
              <span className="results-badge">
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true">
                  <path d="m5 13 4 4L19 7" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                {job.options?.output === "highlight"
                  ? "Highlight video ready"
                  : `${job.clips.length} clip${job.clips.length === 1 ? "" : "s"} ready`}
              </span>
              <h2 className="section-title">
                Fresh out of <em className="serif grad">the studio.</em>
              </h2>
              <p className="section-sub">
                {job.options?.output === "highlight"
                  ? "Watch it, jump to any moment, or download it for YouTube."
                  : "Preview each clip, open the editor for changes, or download it when it feels right."}
              </p>
            </div>

            <HighlightResult job={job} />

            <div className="clip-grid">
              {job.clips.map((clip, i) => (
                <article className="clip-card" key={clip.file} style={{ animationDelay: `${i * 110}ms` }}>
                  <div className="clip-stage">
                    <video
                      className="clip-video"
                      src={clipUrl(job.id, clip.file, clip.version)}
                      key={clip.version}
                      controls
                      playsInline
                      preload="metadata"
                      onMouseEnter={(e) => {
                        e.currentTarget.play().catch(() => {});
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.pause();
                        e.currentTarget.currentTime = 0;
                      }}
                    />
                    <span className="clip-index">#{i + 1}</span>
                    <span className="clip-duration">
                      {fmtDuration(clip.duration || (clip.end - clip.start))}
                    </span>
                  </div>
                  <div className="clip-meta">
                    {/* The score is why you would pick this clip, so it sits
                        beside the title rather than under six progress bars. */}
                    <div className="clip-headline">
                      <div>
                        {/* Highlights found from motion and sound (silent
                            gameplay) have no title or hook -- there was nothing
                            to read. Say what they are instead of an empty card. */}
                        <h3 className="clip-title">
                          {clip.title || `Action highlight · ${fmtDuration(clip.start || 0)}`}
                        </h3>
                        <p className="clip-hook">
                          {clip.hook || (!hasScores(clip)
                            ? "Picked from the action itself: the sharpest burst of sound and motion in the video."
                            : "")}
                        </p>
                      </div>
                      {/* The number is the model's judgement of the moment. A
                          clip it never judged (a sound-and-motion highlight)
                          gets no number rather than an invented one. */}
                      {clip.score != null && hasScores(clip) && (
                        <div className="clip-score-badge" title="Virality score">
                          <strong>{clip.score.toFixed(1)}</strong>
                          <span>Virality</span>
                        </div>
                      )}
                    </div>

                    {hasScores(clip) && (
                      <div className="clip-scores">
                        <div className="scores-head">
                          <span>Why this one</span>
                        </div>
                        {/* Must track analyzer.WEIGHTS -- a key that no longer
                            exists renders as a silently empty bar. */}
                        {[
                          ["Hook", "hook"],
                          ["Stands alone", "standalone"],
                          ["Emotion", "emotion"],
                          ["Ending", "ending"],
                          ["Payoff", "payoff"],
                          ["Shareable", "share"],
                        ]
                          .filter(([, key]) => clip.scores?.[key] != null)
                          .map(([label, key]) => (
                            <div className="score-row" key={key}>
                              <span className="score-label">{label}</span>
                              <span className="score-bar">
                                <i style={{ width: `${Math.min(Number(clip.scores[key]) || 0, 10) * 10}%` }} />
                              </span>
                            </div>
                          ))}
                      </div>
                    )}
                    <div className="clip-actions">
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm clip-edit"
                        onClick={() => {
                          if (isPostHogConfigured) posthog.capture("clip_editor_opened");
                          setEditing({ clip, index: i });
                        }}
                        title="Trim or extend this clip"
                        data-testid={`clip-trim-${i}`}
                      >
                        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" aria-hidden="true">
                          <path d="M4 8h16M4 16h16M9 5v6M15 13v6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
                        </svg>
                        Trim
                      </button>
                      {/* Sharing publishes the score breakdown, which until now
                          only its owner ever saw -- so it is the paid plan's
                          distribution feature, not a nicety. */}
                      <button
                        type="button"
                        className={`btn btn-ghost btn-sm clip-share ${plan.can("share_pages") ? "" : "locked"}`}
                        onClick={() => shareClip(i)}
                        disabled={sharing === i}
                        title={plan.can("share_pages")
                          ? "Copy a public link to this clip and its score"
                          : "Upgrade to share this clip"}
                        data-testid={`clip-share-${i}`}
                      >
                        {plan.can("share_pages") ? (
                          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" aria-hidden="true">
                            <path d="M8.7 13.3a3.5 3.5 0 0 0 5 0l3-3a3.5 3.5 0 1 0-5-5l-1 1" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                            <path d="M15.3 10.7a3.5 3.5 0 0 0-5 0l-3 3a3.5 3.5 0 1 0 5 5l1-1" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                          </svg>
                        ) : (
                          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true">
                            <rect x="5" y="11" width="14" height="9" rx="2.2" stroke="currentColor" strokeWidth="2" />
                            <path d="M8.5 11V8a3.5 3.5 0 0 1 7 0v3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                          </svg>
                        )}
                        {sharing === i ? "Linking..." : "Share"}
                      </button>
                      <a
                        className="btn btn-primary btn-shine clip-download"
                        href={clipUrl(job.id, clip.file, clip.version,
                                      downloadName(clip.title, i))}
                        download={downloadName(clip.title, i)}
                        data-testid={`clip-download-${i}`}
                        onClick={() => {
                          if (isPostHogConfigured) posthog.capture("clip_downloaded");
                        }}
                      >
                        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" aria-hidden="true">
                          <path d="M12 4v11m0 0 4-4m-4 4-4-4M5 19h14" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                        Download
                      </a>
                    </div>
                  </div>
                </article>
              ))}
            </div>

            {editing && (
              <LiveEditor
                job={job}
                clip={editing.clip}
                index={editing.index}
                canEditCaptions={plan.can("caption_editing")}
                canTightenPauses={plan.can("tighten_pauses")}
                canRemoveWatermark={plan.can("no_watermark")}
                onUpgrade={(reason) => requestUpgrade(reason || "editor_export")}
                onClose={() => setEditing(null)}
                onSaved={(index, updated) =>
                  setJob((prev) => {
                    if (!prev) return prev;
                    const clips = [...prev.clips];
                    // cache-bust: the filename is unchanged but the bytes are not
                    clips[index] = { ...clips[index], ...updated, _v: Date.now() };
                    return { ...prev, clips };
                  })
                }
              />
            )}

            <button
              className="btn btn-ghost results-again"
              onClick={() => {
                setJob(null);
                setUrl("");
                setUploadFile(null);
                // Back to the start of the wizard, not to whatever step the last
                // run happened to end on.
                setStep(1);
                focusUrlbar();
              }}
            >
              Clip another video
            </button>
          </section>
        )}
        </div>
      )}

      {showDash && user && (
        <Dashboard
          user={user}
          onClose={() => { leaveAppView(); setStep(1); }}
          onOpenJob={(j) => { setJob(j); showAppView("studio"); }}
          onUpgrade={() => setPricingOpen(true)}
        />
      )}

      {upgradeFor !== null && (
        <UpgradeModal
          reason={upgradeFor}
          limits={plan.limits}
          onClose={() => setUpgradeFor(null)}
          onSeePlans={() => {
            setUpgradeFor(null);
            setPricingOpen(true);
          }}
        />
      )}

      {paymentStatus && (
        <PaymentProcessing
          status={paymentStatus === "success" ? "success" : paymentStatus}
          user={justUpgraded}
          onClose={() => { setPaymentStatus(null); setJustUpgraded(null); }}
          onStart={() => {
            setPaymentStatus(null);
            setJustUpgraded(null);
            document.querySelector('input[type="url"]')?.focus();
            window.scrollTo({ top: 0, behavior: "smooth" });
          }}
        />
      )}

      {pricingOpen && (
        <div className="pricing-veil" onClick={() => setPricingOpen(false)} role="presentation">
          <div className="pricing-dialog" onClick={(e) => e.stopPropagation()} role="dialog"
               aria-modal="true" aria-label="Plans and pricing">
            <Pricing user={user} onRequireAuth={() => openAuth("signup")} modal
                     onClose={() => setPricingOpen(false)}
                     onViewAll={openPricingSection} />
          </div>
        </div>
      )}

      {authOpen && (
        <AuthModal
          onClose={() => { setAuthOpen(false); setAuthIntent(null); }}
          onAuthed={handleAuthed}
          initialMode={authMode}
        />
      )}
    </>
  );
}
