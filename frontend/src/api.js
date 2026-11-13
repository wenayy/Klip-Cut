/* Same-origin "/api" by default, which is what the dev server proxies and what
   a single-origin ingress serves. A frontend deployed SEPARATELY from its API
   -- static hosting on one domain, the API on another -- has no such path, and
   a relative URL there resolves against the frontend's own domain: the login
   POST went to the static host and came back 404, never reaching the API at
   all. VITE_API_BASE overrides it with the API's absolute origin.

   Vite inlines this at BUILD time, so it must be set in the host's environment
   before the build, and changing it needs a redeploy. Include any prefix the
   API is mounted under (see backend/server.py, which mounts at /api). */
export const API_BASE = import.meta.env.VITE_API_BASE || "/api";

/* Keep implementation failures on the server. A creator only needs the next
   useful action, not the name of the downloader or transcription provider. */
function friendlyMessage(status, detail, fallback) {
  if (status === 402) return "This needs a plan with more room.";
  if (status === 413) return "That file is larger than this plan allows.";
  /* 503 is the server saying "busy, come back", which is a queue rather than a
     fault, and it arrives with a specific explanation worth keeping. The
     blanket 5xx line below exists to avoid leaking internals from crashes --
     but this detail is written for users, so flattening it into the same
     apology would throw away the one message that tells them retrying works. */
  if (status === 503) {
    return detail || "We're finishing another render right now. Try again in a moment.";
  }
  if (status >= 500) return "We couldn't finish that right now. Please try again in a moment.";
  if (/stalled|traffic|rate.?limit|too many requests|yt-dlp|ffmpeg|deepgram|openai|api key|cookie|traceback|exception/i.test(detail)) {
    return "That link is busy right now. Try again shortly, or upload the video file instead.";
  }
  return detail || fallback;
}

function authHeaders() {
  const token = localStorage.getItem("clipper_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}


export async function submitJob({ url, nClips, mode, burnSubtitles, burnTitle, autoCensor, multilingual, tightenPauses, voice, language, template, captionStyle, ratio, lengthPref, genre, intent, highQuality, advancedModel, output, highlightMinutes, highlightMemes, highlightTags, turnstileToken }) {
  const resp = await fetch(`${API_BASE}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      url,
      n_clips: nClips,
      mode,
      burn_subtitles: burnSubtitles,
      burn_title: burnTitle !== false,
      auto_censor: autoCensor !== false,
      multilingual: multilingual === true,
      tighten_pauses: tightenPauses === true,
      voice,
      language,
      template,
      caption_style: captionStyle,
      ratio,
      length_pref: lengthPref,
      genre: genre || "auto",
      intent,
      high_quality: highQuality === true,
      advanced_model: advancedModel === true,
      output: output || "clips",
      highlight_minutes: highlightMinutes || null,
      highlight_memes: highlightMemes !== false,
      highlight_tags: highlightTags !== false,
      turnstile_token: turnstileToken || "",
    }),
  });
  if (!resp.ok) throw await readError(resp, "We couldn't start that clip yet.");
  return resp.json(); // { job_id }
}

/** Uploads a local source video, then starts the exact same clipping pipeline.
    The options travel as JSON in multipart form so the video never needs to be
    held in browser memory or encoded as base64.

    XMLHttpRequest rather than fetch, purely for `upload.onprogress`: fetch
    still cannot report how much of a request body has gone out. On a phone
    pushing a 500 MB file that is the difference between a progress bar and
    several silent minutes that look like a hang -- and the plan ceilings mean
    the biggest allowed upload is 4 GB, so this is the normal case, not an edge
    one. `onProgress` receives 0..1, or -1 when the browser cannot size the
    body. Deliberately not null for that case: null is the caller's "no upload
    in flight" signal, so reusing it would make the bar disappear mid-upload on
    exactly the connections least able to spare the reassurance. */
export function uploadJob(file, options, onProgress, turnstileToken) {
  const body = new FormData();
  body.append("file", file);
  body.append("options", JSON.stringify(options));
  if (turnstileToken) body.append("turnstile_token", turnstileToken);

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/jobs/upload`);
    for (const [k, v] of Object.entries(authHeaders())) xhr.setRequestHeader(k, v);

    xhr.upload.onprogress = (e) => {
      onProgress?.(e.lengthComputable ? e.loaded / e.total : -1);
    };
    /* The bytes are gone but the server has not answered yet -- transcoding
       checks, the duration gate. Report 1 so the bar completes rather than
       resting at 99% through a wait that has nothing to do with uploading. */
    xhr.upload.onload = () => onProgress?.(1);

    xhr.onload = () => {
      let detail = "";
      try {
        const parsed = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) return resolve(parsed);
        detail = Array.isArray(parsed.detail)
          ? parsed.detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
          : parsed.detail || "";
      } catch {
        if (xhr.status >= 200 && xhr.status < 300) {
          return reject(new Error("The upload finished but the reply was unreadable."));
        }
      }
      // Same shape readError produces, so callers can treat both identically.
      const err = new Error(friendlyMessage(
        xhr.status, detail, `Could not upload video (${xhr.status})`));
      err.status = xhr.status;
      err.detail = detail;
      reject(err);
    };
    xhr.onerror = () => reject(new Error(
      "The upload was interrupted. Check your connection and try again."));
    xhr.onabort = () => reject(new Error("Upload cancelled."));

    xhr.send(body);
  });
}

export async function getSourcePreview(url, signal) {
  const resp = await fetch(`${API_BASE}/sources/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ url }),
    signal,
  });
  if (!resp.ok) throw await readError(resp, "Could not preview that video");
  return resp.json();
}

/* Face-aware crop windows for a podcast clip, so the editor previews the
   composition the clip was actually rendered in. Returns {plan: null} for every
   other template, and whenever detection found nothing to track. */
export async function getClipReframe(jobId, index, start, end, template, autoZoom, autoBroll) {
  const params = new URLSearchParams();
  if (start != null && end != null) {
    params.set("start", start);
    params.set("end", end);
  }
  if (template) params.set("template", template);
  if (autoZoom) params.set("auto_zoom", "1");
  if (autoBroll === false) params.set("auto_broll", "0");
  const q = params.toString() ? `?${params}` : "";
  const resp = await fetch(`${API_BASE}/jobs/${jobId}/reframe/${index}${q}`);
  if (!resp.ok) return { plan: null };
  return resp.json();
}

export async function getJob(jobId) {
  const resp = await fetch(`${API_BASE}/jobs/${jobId}`);
  if (!resp.ok) throw new Error(`Failed to fetch job (${resp.status})`);
  return resp.json();
}

/* Public pricing AND the per-plan ceilings. The studio needs the free limits
   before anyone has signed in, so they cannot come from /auth/me.
   (Defined once, further down — see getPlans.) */

/* Mints (or returns) the public page for one clip. 402 when the plan does not
   include it -- the caller turns that into the upgrade prompt. */
export async function createShare(jobId, index) {
  const resp = await fetch(`${API_BASE}/jobs/${jobId}/clips/${index}/share`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!resp.ok) {
    const err = await readError(resp, "Could not create a share link");
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

export async function getShare(token) {
  const resp = await fetch(`${API_BASE}/share/${token}`);
  if (!resp.ok) throw await readError(resp, "This share link is not valid");
  return resp.json();
}

/* `version` is the rendered file's mtime. Re-exporting rewrites the same
   filename, so without it the browser keeps showing the cached pre-edit video
   -- the reason an edited title updated on the card but not in the picture. */
export function clipUrl(jobId, filename, version, download) {
  const base = `${API_BASE}/clips/${jobId}/${filename}`;
  const params = new URLSearchParams();
  if (version) params.set("v", version);
  /* The name the user should see when saving. It has to travel to the server
     rather than living only in the <a download> attribute, because a browser
     DISCARDS that attribute as soon as the request redirects to another origin
     -- which is exactly what happens once clips are served from object storage.
     The server signs it into the URL instead, so the file arrives named. */
  if (download) params.set("download", download);
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

export async function getTranscript(jobId) {
  const resp = await fetch(`${API_BASE}/jobs/${jobId}/transcript`);
  if (!resp.ok) throw await readError(resp, `Could not load transcript (${resp.status})`);
  return resp.json();
}

export async function rerenderClip(jobId, index, start, end, opts = {}) {
  const resp = await fetch(`${API_BASE}/jobs/${jobId}/clips/${index}/rerender`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      start,
      end,
      caption_style: opts.captionStyle || null,
      caption_font: opts.captionFont || null,
      translate_to: opts.translateTo || null,
      caption_lines: opts.captionLines || null,
    }),
  });
  if (!resp.ok) throw await readError(resp, `Re-render failed (${resp.status})`);
  return resp.json();
}

/* ---- billing ---- */

async function readError(resp, fallback) {
  let detail = "";
  try {
    const body = await resp.json();
    // FastAPI validation errors arrive as a list of objects, not a string.
    detail = Array.isArray(body.detail)
      ? body.detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
      : body.detail || "";
  } catch { /* non-JSON body */ }
  const error = new Error(friendlyMessage(resp.status, detail, fallback));
  error.status = resp.status;
  error.detail = detail;
  return error;
}

export async function getPlans() {
  const resp = await fetch(`${API_BASE}/billing/plans`);
  if (!resp.ok) throw await readError(resp, `Could not load plans (${resp.status})`);
  return resp.json();
}

export async function getBilling() {
  const resp = await fetch(`${API_BASE}/billing/me`, { headers: authHeaders() });
  if (!resp.ok) throw await readError(resp, `Could not load billing (${resp.status})`);
  return resp.json();
}

export async function startCheckout({ planId, credits, interval = "monthly" }) {
  const resp = await fetch(`${API_BASE}/billing/checkout`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ plan_id: planId, credits, interval }),
  });
  if (!resp.ok) throw await readError(resp, `Checkout failed (${resp.status})`);
  return resp.json();
}

/* ---- auth ---- */

export async function register(email, password) {
  const resp = await fetch(`${API_BASE}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!resp.ok) throw await readError(resp, `Sign up failed (${resp.status})`);
  const data = await resp.json();
  localStorage.setItem("clipper_token", data.token);
  return data.user;
}

export async function login(email, password) {
  const resp = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!resp.ok) throw await readError(resp, `Sign in failed (${resp.status})`);
  const data = await resp.json();
  localStorage.setItem("clipper_token", data.token);
  return data.user;
}

export async function fetchMe() {
  const token = localStorage.getItem("clipper_token");
  if (!token) return null;
  const resp = await fetch(`${API_BASE}/auth/me`, { headers: authHeaders() });
  if (!resp.ok) {
    localStorage.removeItem("clipper_token");   // expired or forged
    return null;
  }
  return resp.json();
}

export function logout() {
  localStorage.removeItem("clipper_token");
}

export async function listJobs() {
  const resp = await fetch(`${API_BASE}/jobs`, { headers: authHeaders() });
  if (!resp.ok) throw await readError(resp, `Could not load history (${resp.status})`);
  return resp.json();
}

/* ---- live (non-destructive) editing ---- */

/* Each clip has its own proxy, covering only the window its trim handles can
   reach. `clipIndex` is the editor's 0-based index; the files on disk are
   1-based (preview_1.mp4, next to clip_1.mp4). */
export function previewUrl(jobId, clipIndex) {
  const q = clipIndex === undefined || clipIndex === null
    ? "" : `?clip=${clipIndex + 1}`;
  return `${API_BASE}/jobs/${jobId}/preview${q}`;
}

/** Saves the clip recipe. No render, no cost -- safe to call on every change. */
export async function saveClipEdit(jobId, index, recipe) {
  const resp = await fetch(`${API_BASE}/jobs/${jobId}/clips/${index}/edit`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(recipe),
  });
  if (!resp.ok) throw await readError(resp, `Could not save edit (${resp.status})`);
  return resp.json();
}

/** Renders the stored recipe to a real MP4. The only expensive call here. */
export async function exportClip(jobId, index) {
  const resp = await fetch(`${API_BASE}/jobs/${jobId}/clips/${index}/export`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!resp.ok) throw await readError(resp, `Export failed (${resp.status})`);
  return resp.json();
}

export function overlayImageUrl(jobId, file) {
  return `${API_BASE}/jobs/${jobId}/overlays/${file}`;
}

/** Uploads a logo for this job. Returns { file, url }. */
export async function uploadOverlayImage(jobId, file) {
  const body = new FormData();
  body.append("file", file);
  const resp = await fetch(`${API_BASE}/jobs/${jobId}/overlays/upload`, {
    method: "POST", headers: authHeaders(), body,
  });
  if (!resp.ok) throw await readError(resp, `Upload failed (${resp.status})`);
  return resp.json();
}

export function gameplayUrl(jobId, file) {
  if (file) return `${API_BASE}/jobs/${jobId}/gameplay?file=${encodeURIComponent(file)}`;
  return `${API_BASE}/jobs/${jobId}/gameplay`;
}

export function gameplayFileUrl(filename) {
  return `${API_BASE}/gameplay-file/${encodeURIComponent(filename)}`;
}

export async function getGameplayList() {
  const resp = await fetch(`${API_BASE}/gameplay-list`);
  if (!resp.ok) throw new Error("Could not load gameplay list");
  return resp.json();
}

/* Fonts, animations and speed range come from the renderer, so a .ttf dropped
   into assets/fonts appears here without a matching frontend edit. */
let _capOptions = null;
export async function getCaptionOptions() {
  if (_capOptions) return _capOptions;
  const resp = await fetch(`${API_BASE}/caption-options`);
  if (!resp.ok) throw new Error("Could not load caption options");
  _capOptions = await resp.json();
  return _capOptions;
}
