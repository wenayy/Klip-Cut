import { useRef, useState } from "react";
import { clipUrl } from "./api";

/* The result of a highlight-video job: ONE video, so one player -- with the
   moments it is made of listed as chapters you can jump to. */

function fmt(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/* YouTube reads chapters from the description, and only shows them when the
   first starts at 0:00, there are at least three, and each lasts 10 s or more.
   The ~7 s cold open is too short to be its own chapter, so the first moment's
   chapter starts at 0:00 and takes the teaser with it. */
const MIN_CHAPTER = 10;

function youtubeChapters(chapters) {
  const lines = [];
  let last = -Infinity;
  chapters.forEach((c, i) => {
    const at = lines.length ? c.at : 0;
    if (at - last < MIN_CHAPTER) return;
    lines.push(`${fmt(at)} ${c.title || `Moment ${i + 1}`}`);
    last = at;
  });
  return lines.join("\n");
}

export default function HighlightResult({ job }) {
  const info = job.options?.highlight;
  const videoRef = useRef(null);
  const [copied, setCopied] = useState(false);
  if (!info?.file) return null;

  const chapters = info.chapters || [];
  const ratio = (job.options?.ratio || "16:9").replace(":", "x");
  const base = (job.options?.source_title || "video").replace(/[\\/:*?"<>|]+/g, " ").trim().slice(0, 60);
  const download = `${base || "video"} - highlights.mp4`;

  function seek(at) {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = at;
    v.play().catch(() => {});
  }

  async function copyChapters() {
    try {
      await navigator.clipboard.writeText(youtubeChapters(chapters));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  return (
    <article className={`highlight-result ratio-${ratio}`} data-testid="highlight-video">
      <video ref={videoRef} className="highlight-player" src={clipUrl(job.id, info.file)}
             controls playsInline preload="metadata" />
      <div className="highlight-side">
        <span className="highlight-eyebrow">Highlight video</span>
        <h3>{fmt(info.duration)} of the best parts</h3>
        <p>
          {info.segments} moment{info.segments === 1 ? "" : "s"} in the order they happened
          {info.memes ? `, with ${info.memes} meme beat${info.memes === 1 ? "" : "s"}` : ""}.
        </p>
        <div className="highlight-actions">
          <a className="btn btn-primary btn-shine"
             href={clipUrl(job.id, info.file, null, download)} download={download}
             data-testid="highlight-download">
            Download video
          </a>
          {chapters.length >= 3 && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={copyChapters}
                    data-testid="highlight-copy-chapters">
              {copied ? "Copied" : "Copy YouTube chapters"}
            </button>
          )}
        </div>
        {chapters.length > 0 && (
          <ol className="highlight-chapters" aria-label="Moments in this video">
            {chapters.map((c, i) => (
              <li key={i}>
                <button type="button" onClick={() => seek(c.at)}>
                  <span className="chap-time">{fmt(c.at)}</span>
                  <span className="chap-title">{c.title || `Moment ${i + 1}`}</span>
                  <span className="chap-source">from {fmt(c.source_start)}</span>
                </button>
              </li>
            ))}
          </ol>
        )}
      </div>
    </article>
  );
}
