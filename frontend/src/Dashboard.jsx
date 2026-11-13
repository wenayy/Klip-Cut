import { useEffect, useState } from "react";
import { listJobs, getBilling, startCheckout, clipUrl } from "./api";
import posthog, { isPostHogConfigured } from "./posthog";

function when(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return then.toLocaleDateString();
}

function sourceLabel(url) {
  if (url.startsWith("upload://")) return url.slice("upload://".length);
  try {
    const u = new URL(url);
    return (u.searchParams.get("v") || u.pathname.replace("/", "")) || u.hostname;
  } catch {
    return url.slice(0, 40);
  }
}

function expiresIn(iso, now) {
  if (!iso) return "";
  const remaining = new Date(iso).getTime() - now;
  if (remaining <= 0) return "Expiring now";
  const mins = Math.ceil(remaining / 60000);
  if (mins < 60) return `Expires in ${mins}m`;
  const hours = Math.ceil(mins / 60);
  if (hours < 24) return `Expires in ${hours}h`;
  return `Expires in ${Math.ceil(hours / 24)}d`;
}

function minutes(seconds) {
  const m = Math.round((seconds || 0) / 60);
  return m >= 1 ? `${m} min` : `${Math.round(seconds || 0)}s`;
}

/**
 * Signed-in home: credit balance, top-ups, and every past job.
 *
 * Persistence is what makes this possible -- before jobs were stored, closing
 * the tab lost the clips.
 */
export default function Dashboard({ user, onOpenJob, onClose, onUpgrade }) {
  const [jobs, setJobs] = useState(null);
  const [billing, setBilling] = useState(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(null);
  const [clock, setClock] = useState(Date.now());

  useEffect(() => {
    listJobs().then(setJobs).catch(() => setJobs([]));
    getBilling().then(setBilling).catch(() => {});
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 60000);
    return () => window.clearInterval(timer);
  }, []);

  async function buy(credits) {
    setBusy(credits);
    setNotice("");
    try {
      const r = await startCheckout({ credits });
      if (r.checkout_url) {
        if (isPostHogConfigured) {
          posthog.capture("checkout_started", {
            purchase_type: "credit_pack",
            credit_amount: credits,
          });
        }
        setNotice("Opening secure checkout…");
        window.location.assign(r.checkout_url);
        return;
      }
      setNotice("This credit pack is not available yet. Please choose another one.");
    } catch (e) {
      setNotice(e.message);
    } finally {
      setBusy(null);
    }
  }

  const done = (jobs || []).filter((j) => j.status === "done");
  const totalClips = done.reduce((n, j) => n + (j.clips?.length || 0), 0);

  return (
    <div className="dash">
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
              <em>Library</em>
            </div>
            <h2>Your clips</h2>
            <p className="dash-sub">Everything you've made, ready to edit or download.</p>
            <span className="dash-account">{user.email}</span>
          </div>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>
            New clip
          </button>
        </header>

        <div className="dash-stats">
          <div className="dash-stat">
            <strong>{billing?.credits ?? user.credits}</strong>
            <span>credits left</span>
          </div>
          <div className="dash-stat">
            <strong>{done.length}</strong>
            <span>videos processed</span>
          </div>
          <div className="dash-stat">
            <strong>{totalClips}</strong>
            <span>clips made</span>
          </div>
        </div>

        {/* Top-ups live here rather than only on the pricing page: this is where
            someone is when they notice they are running low. */}
        <section className="dash-section dash-credit-section">
          <div className="dash-section-copy">
            <h3>Add credits</h3>
            <p className="dash-hint">One-off top-ups that never expire.</p>
          </div>
          <div className="topup-row">
            {[
              { credits: 500, usd: 12 },
              { credits: 1500, usd: 30 },
              { credits: 5000, usd: 90 },
            ].map((t) => (
              <button
                key={t.credits}
                className="topup topup-buy"
                onClick={() => buy(t.credits)}
                disabled={busy === t.credits}
              >
                <strong>{t.credits.toLocaleString()}</strong>
                <span>credits</span>
                <em>{busy === t.credits ? "…" : `$${t.usd}`}</em>
              </button>
            ))}
          </div>
          {notice && <p className="dash-notice">{notice}</p>}
        </section>

        <section className="dash-section">
          <h3>History</h3>

          {jobs === null && <p className="dash-hint">Loading…</p>}

          {jobs !== null && jobs.length === 0 && (
            <div className="dash-empty">
              <p>No videos yet.</p>
              <button className="btn btn-primary btn-sm" onClick={onClose}>
                Clip your first video
              </button>
            </div>
          )}

          <div className="job-list">
            {(jobs || []).map((job) => {
              const available = job.status === "done";
              return (
              <div
                key={job.id}
                className={`job-row ${available ? "" : "disabled"}`}
                role={available ? "button" : undefined}
                tabIndex={available ? 0 : undefined}
                onClick={() => available && onOpenJob(job)}
                onKeyDown={(event) => {
                  if (available && (event.key === "Enter" || event.key === " ")) {
                    event.preventDefault();
                    onOpenJob(job);
                  }
                }}
              >
                <span className={`job-status s-${job.status}`}>
                  {job.status === "done" ? "✓"
                    : job.status === "failed" ? "!"
                    : job.status === "expired" ? "–"
                    : "•"}
                </span>

                <span className="job-main">
                  <span className="job-title">
                    {job.clips?.[0]?.title || job.options?.source_title || sourceLabel(job.url)}
                  </span>
                  <span className="job-meta">
                    {job.status === "done" && job.options?.output === "highlight"
                      ? `Highlight video · ${minutes(job.options.highlight?.duration)} · ${expiresIn(job.expires_at, clock)}`
                      : job.status === "done"
                      ? `${job.clips.length} clip${job.clips.length === 1 ? "" : "s"} · ${expiresIn(job.expires_at, clock)}`
                      : job.status === "expired"
                      ? "Expired · Re-run this video to make fresh clips"
                      : job.status === "failed"
                      ? job.error?.slice(0, 70) || "Failed"
                      : job.progress_message || job.status}
                    {" · "}
                    {when(job.created_at)}
                  </span>
                </span>

                {available && user.plan === "free" && (
                  <button
                    type="button"
                    className="job-retention-upgrade"
                    onClick={(event) => {
                      event.stopPropagation();
                      onUpgrade?.();
                    }}
                  >
                    Upgrade to keep for 3 days
                  </button>
                )}

                {job.status === "done" && job.options?.highlight?.file && (
                  <span className="job-thumbs wide">
                    <video src={clipUrl(job.id, job.options.highlight.file)}
                           muted preload="metadata" aria-hidden="true" />
                  </span>
                )}

                {job.status === "done" && job.clips?.length > 0 && (
                  <span className="job-thumbs">
                    {job.clips.slice(0, 3).map((c, i) => (
                      <video
                        key={i}
                        src={clipUrl(job.id, c.file)}
                        muted
                        preload="metadata"
                        aria-hidden="true"
                      />
                    ))}
                  </span>
                )}
              </div>
            );})}
          </div>
        </section>
      </div>
    </div>
  );
}
