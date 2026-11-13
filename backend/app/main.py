"""FastAPI backend for the clipping tool.

Run with:
    uvicorn app.main:app --reload --port 8000

Endpoints:
    POST /jobs            submit a public video URL, returns a job id
    POST /jobs/upload     upload a local video and return a job id
    GET  /jobs/{job_id}    poll status + get clip results once done
    GET  /clips/{job_id}/{filename}   download a rendered clip
"""

from app import env  # noqa: F401  -- loads .env BEFORE anything reads keys
import contextlib
import json
import os
import uuid
import subprocess
import random
import secrets
import threading
import time
import traceback
import shutil
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.responses import (FileResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, billing, dodo_payments, editing, queue, storage, turnstile, workspace
from app.db import SessionLocal, get_session, init_db
from app.jobs import (create_job, expire_due_jobs, get_job, get_job_transcript,
                      list_jobs, update_job)
from app.models import Clip, FreeEditorExport, Job, Share, User, _iso_utc
from app.pipeline import (analyzer, caption_emoji, caption_presets, censor,
                          content, emoji_overlay, energy, downloader, highlight,
                          hinglish, music, pacing, reframe, render, scoring,
                          subtitles, transcriber, voiceover)
from app.dodo_catalog import subscription_product

app = FastAPI(title="Clipper API")

# The frontend normally reaches this API same-origin (a dev-server proxy, or an
# ingress that routes /api here), so CORS is only in play for a direct
# cross-origin call. CLIPPER_CORS_ORIGINS covers hosted frontends without
# needing a code change; the local dev servers are the default.
_cors = os.environ.get("CLIPPER_CORS_ORIGINS", "").strip()
app.add_middleware(
    CORSMiddleware,
    allow_origins=([o.strip() for o in _cors.split(",") if o.strip()]
                   or ["http://localhost:3000", "http://localhost:5173"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "storage")
# Drop royalty-safe gameplay loops (mp4/mov) in here to enable the split layout.
GAMEPLAY_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "gameplay")

# Uploads are streamed to disk, never read into RAM. Keep this configurable for
# a production host with object storage or a stricter reverse-proxy limit.
MAX_UPLOAD_BYTES = int(os.environ.get("CLIPPER_MAX_UPLOAD_BYTES", 4 * 1024 * 1024 * 1024))
VIDEO_UPLOAD_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

# How many renders may run at once in THIS process.
#
# Nothing used to limit this: BackgroundTasks starts every submitted job
# immediately, and the editor's export renders inline on the request thread. Two
# people pressing Export at the same moment therefore put two ffmpeg processes
# in one container, and since a single render already peaks near 800 MB of a
# 1 GB limit, the second one does not fail politely -- the kernel kills both.
#
# One at a time is the honest capacity of a 2-vCPU container: ffmpeg already
# saturates both cores, so a second concurrent render buys no throughput and
# only adds memory pressure. Raise this only alongside the memory limit.
MAX_CONCURRENT_RENDERS = max(1, int(os.environ.get("CLIPPER_MAX_CONCURRENT_RENDERS", "1")))
_RENDER_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_RENDERS)

# How long a REQUEST-time render (the editor's export) will wait for a slot
# before giving up. A queued background job can wait forever because the user
# is watching a progress bar, but an HTTP request cannot: holding the
# connection past a proxy's idle timeout turns a busy server into a mystery
# 502. Better to say "busy, try again" honestly and quickly.
EXPORT_SLOT_WAIT_SECONDS = float(os.environ.get("CLIPPER_EXPORT_SLOT_WAIT", "20"))


@contextlib.contextmanager
def _render_slot(job_id: str = None, wait: float = None):
    """Hold one of the render slots for the duration of the block.

    With `wait` set, gives up after that many seconds and raises 503 -- for
    request-time renders. Without it, waits indefinitely and marks the job
    "queued" so a waiting user sees a queue rather than a stall.
    """
    acquired = _RENDER_SLOTS.acquire(blocking=False)
    if not acquired:
        if wait is not None:
            acquired = _RENDER_SLOTS.acquire(timeout=wait)
            if not acquired:
                raise HTTPException(
                    status_code=503,
                    detail="We're finishing another render right now. Try again in a moment.",
                    headers={"Retry-After": "30"},
                )
        else:
            if job_id:
                update_job(job_id, status="queued", percent=0,
                           progress_message="Waiting for a free render slot...")
            _RENDER_SLOTS.acquire()
    try:
        yield
    finally:
        _RENDER_SLOTS.release()


# "auto" is resolved per job by content.guess_genre; the rest name a rubric in
# analyzer.GENRES directly.
GENRE_CHOICES = ("auto", "general", "podcast", "gaming")
OUTPUT_CHOICES = ("clips", "highlight")
HIGHLIGHT_TOO_SHORT = ("This video is too short for a highlight video -- it needs "
                       "at least 8 minutes to condense. Make clips from it instead; "
                       "you were not charged.")

# Below this share of the runtime being speech, a gameplay video's moments are
# in the game rather than in what is said over it, so they are found by sound
# and motion and the commentary just rides along as captions.
ACTION_FIRST_COVERAGE = 0.25


# Templates provide framing defaults. Caption styling is chosen separately in
# Preferences and stored on the job, while these legacy defaults keep old jobs
# rendering exactly as they did before the choices were separated.
TEMPLATES = {
    "classic":  {"frame": "blur",     "caption_style": "classic",
                 "label": "Classic"},
    "fitvideo": {"frame": "fit",      "caption_style": "fitbox",
                 "label": "Fit video"},
    "tech":     {"frame": "fill",     "caption_style": "tech",
                 "label": "Tech"},
    "business": {"frame": "fill",     "caption_style": "business",
                 "label": "Business"},
    # NOT the gaming template. This is the retention-bait split: a talking head
    # over a stock gameplay loop. Real gameplay footage is handled by Auto (or
    # by the layout it picks); the old name made the two indistinguishable.
    "gameplay": {"frame": "gameplay", "caption_style": "gameplay",
                 "label": "Split screen"},
    # Podcasts are a two-shot problem: the zoom every other template uses to
    # fill the frame is exactly what crops the second person out of it.
    "podcast":  {"frame": "podcast",  "caption_style": "podcast_clean",
                 "label": "Podcast"},
    # Chooses one of the treatments above per clip, from the footage itself.
    # The manual templates stay as an override: a gaming channel can pin its
    # layout, and everyone else stops having to know in advance what their
    # video is.
    "auto":     {"frame": "auto",     "caption_style": "pro_caps",
                 "label": "Auto",
                 "note": "Detects podcast, talking head, gameplay + facecam, "
                         "gameplay or screencast per clip"},
}


@app.get("/platform-logos/{platform}.png")
def serve_platform_logo(platform: str):
    """The same brand mark the renderer composites, so the editor preview and
    the exported frame cannot drift apart."""
    from app.pipeline.platform_logos import brand_logo
    path = brand_logo(platform)
    if not path:
        raise HTTPException(status_code=404, detail="Unknown platform")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/fonts/{font_id}.ttf")
def serve_font(font_id: str):
    """Serves a caption font so the browser previews it in the SAME file the
    renderer burns in. Loading a lookalike from a CDN instead is how a preview
    drifts from the export."""
    entry = subtitles.font_entry(font_id)
    if not entry or not entry.get("file"):
        raise HTTPException(status_code=404, detail="Unknown font")
    path = os.path.join(subtitles.FONTS_DIR, entry["file"])
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Font file missing")
    return FileResponse(path, media_type="font/ttf",
                        headers={"Cache-Control": "public, max-age=31536000"})


@app.get("/caption-options")
def caption_options():
    """Fonts, animations and speed range -- read from the renderer itself.

    The font list is whatever is installed in assets/fonts right now, so adding
    a .ttf shows up in the editor without a code change on either side.
    """
    return {
        "fonts": [
            {"id": k, "label": v["label"], "family": v["family"],
             "devanagari": v["devanagari"], "emoji": v.get("emoji", False)}
            for k, v in subtitles.FONTS.items()
        ],
        # Whether an emoji typed into a caption or title will actually burn in.
        # It reaches the subtitle file either way; libass silently draws nothing
        # unless a face on the fallback path has the glyph. The editor showed
        # emoji in the preview regardless, which is how "it was there while
        # editing and gone after export" happened.
        "emoji_supported": subtitles.emoji_font() is not None,
        "animations": [{"id": k, **v} for k, v in subtitles.ANIMATIONS.items()],
        "speed": {"min": render.MIN_SPEED, "max": render.MAX_SPEED},
        # Title looks are served rather than duplicated in the editor, for the
        # same reason as fonts: a look added here must not need a matching edit
        # on the frontend to become selectable.
        "title_styles": [
            # "Off" is a first-class choice, not an absence. Burning a title in
            # is irreversible, and plenty of people want the cut and the captions
            # while writing their own hook in the platform's composer.
            {"id": subtitles.TITLE_LOOK_OFF, "text": None, "edge": None,
             "boxed": False},
        ] + [
            {"id": k,
             # Enough for the editor to draw a faithful swatch without knowing
             # anything about ASS colour notation.
             "text": _ass_colour_to_css(v["colour"]),
             "edge": _ass_colour_to_css(v["edge"]),
             "boxed": v["border_style"] == 3}
            for k, v in subtitles.TITLE_LOOKS.items()
        ],
        "default_title_style": subtitles.DEFAULT_TITLE_LOOK,
        # The caption presets, in browser terms. Served rather than mirrored so
        # the picker's live previews are generated from the same definitions the
        # renderer burns in -- the editor used to keep its own copy, and the two
        # drifted on both colours and sizes.
        **caption_presets.web_presets(),
    }


def _ass_colour_to_css(value: str) -> str:
    """&HAABBGGRR (ASS, alpha first and BGR order) -> #RRGGBB (CSS)."""
    digits = value.lstrip("&H").lstrip("&h")
    if len(digits) < 6:
        return "#000000"
    bb, gg, rr = digits[-6:-4], digits[-4:-2], digits[-2:]
    return f"#{rr}{gg}{bb}"


@app.get("/health")
def health():
    """Liveness plus queue depth.

    Depth is the number that decides when to add a worker: `waiting` climbing
    while `running` stays flat means the workers are saturated, and no amount
    of API capacity will help. Unauthenticated because a platform health check
    cannot log in, and because none of it is sensitive -- counts only, no job
    ids, no users.
    """
    # `storage` is here because the split only works when it is shared. A
    # deployment with workers but a local driver looks completely healthy from
    # every other angle -- the queue moves, jobs get claimed -- and fails every
    # upload with a message that points at the file rather than the config.
    split_without_shared_storage = (not INLINE_WORKER
                                    and storage.driver.name == "local")
    return {
        "ok": not split_without_shared_storage,
        "queue": queue.stats(),
        "inline_worker": INLINE_WORKER,
        "storage": storage.driver.name,
        **({"error": "Workers render on other containers but STORAGE_BACKEND is "
                     "not set on this one, so uploads cannot reach them. Set "
                     "STORAGE_BACKEND=r2 and the R2_* variables here."}
           if split_without_shared_storage else {}),
    }


@app.get("/turnstile-site-key")
def turnstile_site_key():
    """The frontend needs the site key to render the widget.
    Returns null when Turnstile is not configured (local dev)."""
    key = os.environ.get("TURNSTILE_SITE_KEY", "")
    return {"site_key": key or None, "enabled": turnstile.is_enabled()}


@app.get("/templates")
def list_templates():
    """Lets the UI render the picker from one source of truth."""
    return [{"id": k, **v} for k, v in TEMPLATES.items()]


@app.get("/gameplay-list")
def list_gameplay():
    """Available gameplay files for the editor picker."""
    try:
        files = sorted(f for f in os.listdir(GAMEPLAY_DIR)
                       if f.lower().endswith((".mp4", ".mov", ".webm")))
    except FileNotFoundError:
        files = []
    return [{"file": f, "name": os.path.splitext(f)[0]} for f in files]


def _gameplay_loops() -> list:
    """All installed gameplay assets, shuffled. Raises with setup instructions.

    Returns the whole list rather than one file so a multi-clip job can give each
    clip different footage -- shipping five clips over identical gameplay reads
    as a template, not an edit.
    """
    try:
        files = [f for f in os.listdir(GAMEPLAY_DIR)
                 if f.lower().endswith((".mp4", ".mov", ".webm"))]
    except FileNotFoundError:
        files = []
    if not files:
        raise HTTPException(
            status_code=400,
            detail="No gameplay footage installed. Put an .mp4 loop (e.g. parkour or "
                   f"driving gameplay you have rights to) in {os.path.abspath(GAMEPLAY_DIR)} "
                   "and try again.",
        )
    random.shuffle(files)
    return [os.path.join(GAMEPLAY_DIR, f) for f in files]


class JobRequest(BaseModel):
    url: str
    n_clips: int = 3
    mode: str = "original"          # "original" keeps real audio, "voiceover" replaces it
    voice: str = "onyx"
    language: str = "English"
    burn_subtitles: bool = True
    # With captions off, still pop up the clip's keyword phrases and emoji.
    # Full captions on every line can be too much; nothing at all loses the
    # point of the clip. False is a truly clean picture.
    keyword_captions: bool = True
    burn_title: bool = True
    template: str = "auto"          # key into TEMPLATES
    caption_style: str = "karaoke_fill"  # independent of the frame layout
    ratio: str = "9:16"             # key into render.RATIOS
    length_pref: str = "any"        # key into analyzer.LENGTH_PREFS
    intent: str = ""                # free-text steer, e.g. "when he talks about pricing"
    # What kind of video this is, which decides what counts as a good moment:
    # "auto" works it out from the title and how much of the runtime is speech.
    genre: str = "auto"             # auto | general | podcast | gaming
    # Filled in by the server from the link's own metadata; never trusted for
    # anything but the genre guess.
    source_title: Optional[str] = None
    tighten_pauses: bool = True     # cut dead air so the clip does not feel merely trimmed
    auto_censor: bool = True        # mute profanity and star it in captions
    # Mark the cues that earn an emoji. On by default now that a keyword
    # fallback exists: it costs one model call per clip when a key is
    # configured, and nothing at all when it is not. Clients that specifically
    # want plain captions can still send false.
    auto_emoji: bool = True
    # 1/2/3 words on screen at a time. None keeps the caption preset's cadence,
    # which is what the wizard uses; the editor sets it per clip.
    # Must be Optional: _start_job freezes req.dict() onto the job, which
    # stores None for "use the preset". A plain `int = None` accepted None at
    # construction from the request but REJECTED it when the worker rebuilt
    # JobRequest from the stored options -- so every job crashed at 0% before
    # the pipeline began, while the API happily returned 200.
    words_per_cue: Optional[int] = None
    # Punch-in on the clip's key words and a dip at real layout cuts. On by
    # default: a batch of otherwise-identical static crops was the single
    # loudest "these look boring" complaint, and both are editorial flourishes
    # the editor exposes as one-click toggles. SFX stays opt-in -- a whoosh on
    # every cut is a taste, not a default.
    auto_zoom: bool = True
    auto_transitions: bool = True
    auto_sfx: bool = False
    # Opt-in: a bed changes the mood of every clip, so it should be a choice.
    music_bed: bool = False
    auto_broll: bool = True
    # What the job makes. "clips": n_clips short standalone posts. "highlight":
    # ONE longer video of the best parts of the whole source, in story order --
    # a different product with its own length, so n_clips, length_pref and the
    # clip-only extras (title card, music bed, keyword zoom) do not apply.
    output: str = "clips"
    # Target length of a highlight video in minutes; None = auto (~15% of the
    # source, 3 to 15 minutes).
    highlight_minutes: Optional[float] = None
    highlight_memes: bool = True     # reaction beats after the biggest moments
    highlight_tags: bool = True      # "12 MIN LATER" where the video skips ahead
    highlight_music: bool = True     # a royalty-free bed, ducked under speech
    # Transcribe speech that switches language mid-sentence with the model that
    # can follow it. Off by default and gated on the plan -- see billing.allows.
    multilingual: bool = False
    # Paid-for extras, priced in credits rather than gated on a plan, so a Free
    # account can spend its signup grant finding out whether either is worth it.
    # Both default off: the cheaper path is what everyone already gets, and an
    # opt-out surcharge would be a charge nobody chose.
    high_quality: bool = False       # slower preset, lower CRF -- per clip
    advanced_model: bool = False     # the better analysis model -- per job
    turnstile_token: Optional[str] = None


class SourcePreviewRequest(BaseModel):
    url: str


class Credentials(BaseModel):
    email: str
    password: str


# States that only exist while a worker thread is alive. If the process died
# (a crash, a Ctrl-C, or uvicorn --reload restarting on a file change) the
# thread went with it, and nothing will ever move these jobs again -- they sit
# at "0%" forever, which is exactly what a stuck spinner looks like.
RUNNING_STATES = ("queued", "downloading", "transcribing", "analyzing", "rendering")


def _reload_active() -> bool:
    """True when uvicorn was started with --reload (best effort)."""
    try:
        import psutil  # optional
        return "--reload" in " ".join(psutil.Process(os.getppid()).cmdline())
    except Exception:
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(os.getppid())],
                                 capture_output=True, text=True, timeout=5)
            return "--reload" in out.stdout
        except Exception:
            return False


def _recover_orphaned_jobs():
    """Fails jobs left mid-flight by a previous process. Returns how many."""
    from app.models import Job
    with SessionLocal() as session:
        stale = session.query(Job).filter(Job.status.in_(RUNNING_STATES)).all()
        for job in stale:
            job.status = "failed"
            job.error = ("This run was interrupted when the server restarted, so it "
                         "could not finish. Nothing was charged -- run the video again.")
        if stale:
            session.commit()
        return len(stale)


@app.on_event("startup")
def on_startup():
    init_db()

    orphaned = _recover_orphaned_jobs()
    if orphaned:
        print(f"[clipper] recovered {orphaned} job(s) interrupted by a restart")

    # Clipping runs in a background thread, and --reload kills the process on
    # every file save. Editing any backend file mid-job therefore destroys that
    # job silently -- it just stops at whatever percentage it had reached.
    if os.environ.get("CLIPPER_RELOAD_WARNED") != "1" and _reload_active():
        os.environ["CLIPPER_RELOAD_WARNED"] = "1"
        print("[clipper] NOTE: running with --reload. Saving any backend file "
              "restarts the server and kills jobs that are still running. "
              "Use plain `uvicorn app.main:app` while processing videos.")
    for key, what in (("DEEPGRAM_API_KEY", "transcription"),
                      ("OPENAI_API_KEY", "clip selection")):
        if not os.environ.get(key):
            print(f"[clipper] WARNING: {key} is not set -- {what} will fail. "
                  f"Add it to backend/.env")

    from app.pipeline import downloader as _dl
    # Read from the analyzer rather than the environment: this used to repeat
    # both the variable name and its default, so it would have kept printing
    # "gpt-4o" while jobs actually ran on something else.
    _tiers = analyzer.tiers()
    print(f"[clipper] models: standard={_tiers['standard']} "
          f"advanced={_tiers['advanced']}"
          f"{'' if _tiers['advanced_configured'] else ' (same -- set CLIPPER_MODEL_ADVANCED)'}"
          f" | youtube session: {_dl.cookie_source()}")
    removed = expire_due_jobs()
    if removed:
        print(f"[clipper] expired {removed} finished project(s)")

    _start_inline_worker()


# Whether this API process should also render.
#
# In production it should not: that is the entire point of the split, and a
# worker sharing the web server's CPU and memory limit is what the queue was
# introduced to stop. Set CLIPPER_INLINE_WORKER=0 on the API service and run
# worker.py as its own service.
#
# Locally it should, because otherwise `make dev` starts a server that accepts
# jobs and never runs them -- a broken-looking app and a second terminal to
# remember. Defaulting ON keeps development a single command, and the
# production deploy turns it off explicitly.
INLINE_WORKER = os.environ.get("CLIPPER_INLINE_WORKER", "1").lower() not in (
    "0", "false", "no", "off")

# Hinglish repair needs one LLM call per job. On by default only for jobs that
# asked for multilingual transcription (or whose transcript actually contains
# Devanagari); pure English jobs never pay for it.
# Off by default: every Devanagari word is romanised offline, for free, with
# its ASR timing intact (hinglish.romanize). The LLM rewrite on top only adds
# context fixes for misheard phrases, and Devanagari is the most token-hungry
# script there is. Set CLIPPER_HINGLISH_CLEANUP=1 to pay for it anyway.
HINGLISH_CLEANUP = os.environ.get("CLIPPER_HINGLISH_CLEANUP", "0").lower() not in (
    "0", "false", "no", "off")


# Set when this process is told to shut down. Uvicorn first waits for open
# connections to close -- and the editor holds video streams open -- so a
# "stopped" server could linger for hours with its worker thread still taking
# new jobs. Three such processes were once found running side by side, the
# oldest processing jobs with code from a day earlier.
_WORKER_STOP = threading.Event()


def _stop_worker_on_signals() -> None:
    """Chain onto the server's own SIGTERM/SIGINT handlers: stop claiming, then
    let the server shut down exactly as it would have. A job already running
    finishes; only NEW work is refused."""
    import signal
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue

        def handler(signum, frame, previous=previous):
            _WORKER_STOP.set()
            previous(signum, frame)

        signal.signal(sig, handler)


def _start_inline_worker() -> None:
    if not INLINE_WORKER:
        print("[clipper] inline worker off -- run worker.py to process jobs")
        # Named at boot rather than discovered through a failed upload two
        # minutes into a job on another container.
        if storage.driver.name == "local":
            print("[clipper] *** MISCONFIGURED: jobs render on separate workers "
                  "but STORAGE_BACKEND is not set here, so nothing this service "
                  "stores is reachable by them. Every upload will fail with "
                  "'The uploaded source video is missing'. Set STORAGE_BACKEND=r2 "
                  "and the R2_* variables on this service. ***")
        return

    from app import runner

    def loop():
        name = f"inline:{queue.worker_name()}"
        last_reap = 0.0
        while not _WORKER_STOP.is_set():
            try:
                now = time.monotonic()
                if now - last_reap > 60:
                    queue.release_stale()
                    last_reap = now
                if not runner.work_once(run_queued_job, name):
                    time.sleep(2)
            except Exception:
                # A daemon thread that raises dies silently, taking job
                # processing with it and leaving a server that looks healthy
                # and quietly does nothing.
                print(f"[clipper] inline worker error\n{traceback.format_exc()}")
                time.sleep(5)

        print("[clipper] inline worker stopped: shutting down, no new jobs taken")

    _stop_worker_on_signals()
    threading.Thread(target=loop, name="clipper-inline-worker",
                     daemon=True).start()
    print("[clipper] inline worker started (CLIPPER_INLINE_WORKER=0 to disable)")


@app.post("/auth/register")
def register(body: Credentials, session: Session = Depends(get_session)):
    email = body.email.strip().lower()
    if "@" not in email or len(body.password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Enter a valid email and a password of at least "
                   f"{auth.MIN_PASSWORD_LENGTH} characters.",
        )
    if session.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="That email is already registered.")

    user = User(
        email=email,
        password_hash=auth.hash_password(body.password),
        credits=billing.FREE_SIGNUP_CREDITS,
    )
    session.add(user)
    session.commit()
    return {"token": auth.create_token(user.id),
            "user": {"id": user.id, "email": user.email, "credits": user.credits}}


@app.post("/auth/login")
def login(body: Credentials, session: Session = Depends(get_session)):
    user = session.query(User).filter(User.email == body.email.strip().lower()).first()
    # Same message either way -- distinguishing them tells an attacker which
    # emails are registered.
    if not user or not auth.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    return {"token": auth.create_token(user.id),
            "user": {"id": user.id, "email": user.email, "credits": user.credits}}


@app.get("/billing/plans")
def plans():
    """Public pricing, so the page renders from one source of truth."""
    return {
        "plans": billing.PLANS,
        "topups": billing.TOPUPS,
        "credits_per_clip": billing.CREDITS_PER_CLIP,
        "max_source_minutes": billing.MAX_SOURCE_MINUTES,
        # Signed-out visitors get the free ceilings, which is what the studio
        # needs before anyone has an account.
        "limits": billing.LIMITS,
        "entitlements": {k: sorted(v) for k, v in billing.ENTITLEMENTS.items()},
        # Surcharge rates, so the studio can price a selection as it changes
        # without a round trip per keystroke -- and without hardcoding numbers
        # that would drift from billing.py the first time either one moves.
        "extras": {
            "high_quality_per_clip": billing.HIGH_QUALITY_PER_CLIP,
            "advanced_model_per_job": billing.ADVANCED_MODEL_PER_JOB,
            "highlight_per_minute": billing.HIGHLIGHT_CREDITS_PER_MINUTE,
            "high_quality_per_highlight_minute": billing.HIGH_QUALITY_PER_HIGHLIGHT_MINUTE,
        },
        "highlight": {
            "min_source_minutes": highlight.MIN_SOURCE_SECONDS // 60,
            "max_minutes": highlight.MAX_TARGET // 60,
            "choices": list(highlight.LENGTH_CHOICES),
            "auto_share": highlight.TARGET_SHARE,
            "min_auto_minutes": highlight.MIN_TARGET // 60,
        },
    }


@app.get("/billing/me")
def billing_me(user: User = Depends(auth.current_user_required)):
    return {
        "credits": billing.balance(user.id),
        "plan": user.plan,
        # What this plan unlocks, so the UI can show a locked control with the
        # reason rather than hiding it -- a feature nobody can see is a feature
        # nobody upgrades for. Sent as a list so adding one needs no client
        # change beyond using it.
        "entitlements": sorted(billing.entitlements_for(user.plan, user.email)),
        "limits": billing.limits_for(user.plan, user.email),
        "is_admin": billing.is_admin(user.email),
        "history": billing.history(user.id),
    }


@app.post("/billing/estimate")
def estimate(body: dict):
    """Credits a run would cost, for showing a price before committing.

    Takes a clip count now rather than a duration. `duration_seconds` is still
    accepted and ignored so an older client asking the old question gets a
    correct answer for the default clip count instead of an error.
    """
    n_clips = int(body.get("n_clips") or 1)
    high_quality = bool(body.get("high_quality"))
    advanced_model = bool(body.get("advanced_model"))
    if body.get("output") == "highlight":
        # Priced by length: the requested minutes, or auto from the source.
        minutes = body.get("highlight_minutes")
        if not minutes:
            source = float(body.get("duration_seconds") or 0)
            minutes = (highlight.target_seconds(source) / 60 if source
                       else highlight.MIN_TARGET / 60)
        breakdown = billing.highlight_breakdown(float(minutes) * 60, high_quality,
                                                advanced_model)
    else:
        breakdown = billing.cost_breakdown(n_clips, high_quality, advanced_model)
    return {
        # `credits` stays the total, so a client that only reads this field
        # still quotes the right number when extras are on.
        "credits": breakdown["total"],
        "credits_per_clip": billing.CREDITS_PER_CLIP,
        "lines": breakdown["lines"],
        "extras": {
            "high_quality_per_clip": billing.HIGH_QUALITY_PER_CLIP,
            "advanced_model_per_job": billing.ADVANCED_MODEL_PER_JOB,
        },
    }


class CheckoutRequest(BaseModel):
    plan_id: Optional[str] = None
    credits: Optional[int] = None
    interval: str = "monthly"        # "monthly" | "yearly"


@app.post("/billing/checkout")
def checkout(body: CheckoutRequest, user: User = Depends(auth.current_user_required)):
    """Return a user-bound Dodo checkout URL for a server-priced item.

    Subscriptions bill through a fixed per-tier Dodo product; credit top-ups
    reuse one Pay-What-You-Want product with the exact price passed as `amount`.
    The price is always taken from billing.py here, never from the request, so a
    client cannot name its own price.
    """
    if body.interval not in {"monthly", "yearly"}:
        raise HTTPException(status_code=400, detail="Choose monthly or yearly billing.")
    if body.plan_id:
        plan = billing.get_plan(body.plan_id)
        if not plan:
            raise HTTPException(status_code=400, detail="Unknown plan.")

        # Price comes from the tier, never from the request -- otherwise a client
        # could post any amount it liked and name its own price.
        tier = billing.get_tier(body.plan_id, body.credits)
        if not tier:
            raise HTTPException(
                status_code=400,
                detail=f"{plan['name']} is not offered at {body.credits} credits.",
            )
        product = subscription_product(plan["id"], tier["credits"], body.interval)
        price = tier["yearly_usd"] if body.interval == "yearly" else tier["monthly_usd"]
        line_item = {"kind": "plan", "plan_id": plan["id"], "name": plan["name"],
                     "interval": body.interval, "usd": price,
                     "credits": tier["credits"]}
        url = (dodo_payments.create_subscription_checkout(product, user.id, user.email)
               if product else None)
        if not url:
            return {"status": "provider_not_configured",
                    "detail": "Checkout is not available for this option yet.",
                    "config_key": product.sku if product else None,
                    "line_item": line_item}
        return {"status": "ready", "checkout_url": url, "line_item": line_item}

    topup = next((t for t in billing.TOPUPS if t["credits"] == body.credits), None)
    if not topup:
        raise HTTPException(status_code=400, detail="Unknown credit pack.")

    url = dodo_payments.create_topup_checkout(
        topup["credits"], topup["usd"], user.id, user.email)
    line_item = {"kind": "topup", "credits": topup["credits"], "usd": topup["usd"]}
    if not url:
        # NB: no `product` in this branch -- it is a local assigned only under
        # `if body.plan_id:` above, so referencing it here raised
        # UnboundLocalError and turned every unconfigured top-up into a 500.
        return {"status": "provider_not_configured",
                "detail": "Checkout is not available for this credit pack yet.",
                "config_key": "topup",
                "line_item": line_item}
    return {"status": "ready", "checkout_url": url, "line_item": line_item}


@app.post("/billing/confirm")
def confirm_checkout(body: dict, user: User = Depends(auth.current_user_required)):
    """Report whether the buyer's payment has been fulfilled yet.

    The Dodo webhook is the single source of truth: it verifies the signature
    and grants credits behind a unique per-charge row. Rather than re-fulfil
    here (which would risk double-crediting or trusting an unsigned client), this
    endpoint just tells the frontend whether that grant has landed, by looking
    for a recent DodoGrant on this user. The frontend polls it a few times after
    the redirect, so a webhook arriving a second later still resolves to success.
    """
    from app.models import DodoGrant

    before = body.get("before") if isinstance(body, dict) else None

    with SessionLocal() as session:
        db_user = session.get(User, user.id)
        if not db_user:
            raise HTTPException(status_code=404, detail="User not found.")

        # Preferred signal: did the account actually change versus the snapshot
        # the client took just before redirecting to checkout (billingBefore-
        # Checkout)? This is what tells "THIS payment landed" apart from "an
        # earlier grant or a renewal is still recent". A bare recent-grant check
        # cannot: it reports success for a failed or abandoned second payment
        # whenever any prior purchase or renewal falls inside the window.
        if isinstance(before, dict) and ("credits" in before or "plan" in before):
            before_credits = before.get("credits")
            before_plan = before.get("plan")
            changed = (
                (isinstance(before_credits, (int, float))
                 and db_user.credits > before_credits)
                or (before_plan is not None and db_user.plan != before_plan)
            )
            return {"status": "applied" if changed else "pending",
                    "credits": db_user.credits, "plan": db_user.plan}

        # Fallback when no snapshot reached us (e.g. private-mode sessionStorage
        # failed to store it): the original recent-grant heuristic. Looser, but
        # better than reporting pending forever.
        recent = (session.query(DodoGrant)
                  .filter(DodoGrant.user_id == db_user.id,
                          DodoGrant.created_at >= datetime.now(timezone.utc) - timedelta(minutes=30))
                  .order_by(DodoGrant.created_at.desc())
                  .first())
        if not recent:
            return {"status": "pending", "credits": db_user.credits,
                    "plan": db_user.plan}
        return {"status": "applied", "credits": db_user.credits,
                "plan": db_user.plan}


@app.post("/billing/dodo/webhook", status_code=202)
async def dodo_webhook(request: Request):
    """Verify Dodo's signature, then fulfill paid charges exactly once."""
    body = await request.body()
    try:
        event = dodo_payments.validate_webhook(body, request.headers)
    except dodo_payments.DodoConfigurationError as exc:
        print(f"[clipper] Dodo webhook configuration error: {exc}")
        raise HTTPException(status_code=503, detail="Payment updates are unavailable.") from exc
    except Exception as exc:
        # Signature failures stay deliberately vague at the public boundary.
        print(f"[clipper] Rejected Dodo webhook: {exc}")
        raise HTTPException(status_code=403, detail="Invalid webhook signature.") from exc

    event_id = dodo_payments.delivery_id(request.headers, body)
    try:
        result = dodo_payments.handle_event(event, event_id)
    except dodo_payments.DodoConfigurationError as exc:
        print(f"[clipper] Dodo fulfillment error: {exc}")
        raise HTTPException(status_code=503, detail="Payment update needs attention.") from exc
    except Exception as exc:
        print(f"[clipper] Dodo webhook handler crashed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Internal error processing webhook.") from exc
    return {"received": True, **result}


@app.get("/auth/me")
def me(user: User = Depends(auth.current_user_required)):
    # `entitlements` rides along here rather than only on /billing/me because
    # this is what the app loads on boot, and the clip-creation form needs to
    # know what the plan unlocks before it can render its controls correctly.
    return {"id": user.id, "email": user.email,
            "credits": billing.balance(user.id),
            "plan": user.plan,
            "entitlements": sorted(billing.entitlements_for(user.plan, user.email)),
            # Ceilings, not switches: clip count, source length and upload size.
            "limits": billing.limits_for(user.plan, user.email),
            # So the UI can say WHY a paid control is unlocked on an account
            # whose plan is free -- otherwise testing the paid experience looks
            # identical to a billing bug.
            "is_admin": billing.is_admin(user.email)}


@app.get("/jobs")
def my_jobs(user: User = Depends(auth.current_user_required)):
    return list_jobs(user.id)


# ---------------------------------------------------------------------------
# Public share pages
# ---------------------------------------------------------------------------
# The score breakdown is already computed for every clip and, until now, only
# the person who made it ever saw it. A page that shows the clip next to why it
# was picked is the one thing a creator has a reason to post -- which makes it a
# paid capability rather than a nicety: it is marketing the plan pays for.

@app.post("/jobs/{job_id}/clips/{index}/share")
def create_share(job_id: str, index: int,
                 user: User = Depends(auth.current_user_required),
                 session: Session = Depends(get_session)):
    _gate(user, "share_pages",
          "Share pages are on the Creator and Pro plans. They publish a clip "
          "with its score breakdown on a page you can post anywhere.")

    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Ownership, not just existence: a token minted for someone else's job would
    # publish their video.
    if job.user_id and job.user_id != user.id:
        raise HTTPException(status_code=403, detail="That job belongs to another account.")
    clip = next((c for c in job.clips if c.index == index), None)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    # One link per clip: re-sharing has to return the URL already in circulation,
    # or every press of the button orphans the last one.
    row = (session.query(Share)
           .filter(Share.job_id == job_id, Share.clip_index == index).first())
    if not row:
        row = Share(token=secrets.token_urlsafe(9), job_id=job_id,
                    clip_index=index, user_id=user.id)
        session.add(row)
        session.commit()
    return {"token": row.token, "path": f"/s/{row.token}", "views": row.views}


@app.get("/share/{token}")
def read_share(token: str, session: Session = Depends(get_session)):
    """Public. No auth, and deliberately no way to enumerate."""
    row = session.get(Share, token)
    if not row:
        raise HTTPException(status_code=404, detail="This share link is not valid.")
    job = session.get(Job, row.job_id)
    clip = next((c for c in (job.clips if job else []) if c.index == row.clip_index), None)
    if not clip:
        raise HTTPException(status_code=404, detail="This clip is no longer available.")

    row.views += 1
    session.commit()

    return {
        "token": token,
        "title": clip.title,
        "hook": clip.hook,
        "score": clip.score,
        "scores": clip.scores or {},
        "duration": clip.duration,
        "keywords": clip.keywords or [],
        "video": f"/api/share/{token}/video?v={clip._file_version()}",
        "views": row.views,
        "created_at": _iso_utc(row.created_at),
    }


@app.get("/share/{token}/video")
def read_share_video(token: str, session: Session = Depends(get_session)):
    row = session.get(Share, token)
    if not row:
        raise HTTPException(status_code=404, detail="This share link is not valid.")
    clip = (session.query(Clip)
            .filter(Clip.job_id == row.job_id, Clip.index == row.clip_index).first())
    if not clip:
        raise HTTPException(status_code=404, detail="This clip is no longer available.")

    if storage.driver.serves_redirects:
        key = storage.job_key(row.job_id, os.path.basename(clip.file))
        if not storage.driver.exists(key):
            raise HTTPException(status_code=404,
                                detail="This clip is no longer available.")
        return RedirectResponse(storage.driver.url(key), status_code=302)

    path = os.path.join(STORAGE_DIR, row.job_id, clip.file)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="This clip is no longer available.")
    return FileResponse(path, media_type="video/mp4")


class CaptionLine(BaseModel):
    start: float
    end: float
    text: str
    # Whether the user actually rewrote this line. Only rewritten text loses its
    # per-word timings; untouched lines keep the real ones. Defaults True so
    # recipes saved before this field existed behave exactly as they did.
    edited: bool = True


class ClipEdit(BaseModel):
    # Optional[...] rather than `= None` alone: under Pydantic v2 a plain
    # `str = None` field REJECTS an explicit JSON null, and clients send nulls.
    start: float
    end: float
    caption_style: Optional[str] = None       # override the template's caption look
    caption_lines: Optional[list[CaptionLine]] = None   # rewritten subtitle text
    caption_font: Optional[str] = None        # id from subtitles.FONTS
    translate_to: Optional[str] = None        # language code from translation.LANGUAGES
    auto_emoji: Optional[bool] = None          # None = the job's own setting
    words_per_cue: Optional[int] = None        # 1/2/3 words on screen at a time
    auto_zoom: Optional[bool] = None            # punch-in on key words
    auto_transitions: Optional[bool] = None     # dip through black at cuts
    auto_sfx: Optional[bool] = None             # whoosh on cuts
    music_bed: Optional[bool] = None            # ducked background music
    auto_broll: Optional[bool] = None           # gameplay cutaways


@app.get("/jobs/{job_id}/reframe/{index}")
def clip_reframe(job_id: str, index: int, start: float = None, end: float = None,
                 template: str = None, auto_zoom: bool = False,
                 auto_broll: bool = True):
    """Where the faces are, so the editor can PREVIEW the podcast composition.

    The editor draws its preview from the source proxy and composes the frame
    itself. It had no way to know about face-aware cropping, so it drew the old
    letterbox -- showing the user a layout their clip was not in and had never
    been in.

    Cached on disk per clip: detection takes several seconds, and reopening an
    editor should not pay that again. Trimming passes explicit bounds and gets a
    fresh plan for the new range, since that is a genuinely different question.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="No such job.")
    # The editor can switch the template after the clip exists, so the request
    # may be asking about a different framing than the one the job was born
    # with. Without this, switching a Classic job to Auto previewed the old
    # letterbox until it was exported.
    wanted = template or (job.get("options") or {}).get("template", "classic")
    tpl = TEMPLATES.get(wanted, TEMPLATES["classic"])
    if tpl["frame"] not in ("podcast", "auto"):
        return {"plan": None}          # every other frame is fixed geometry

    clips = job.get("clips") or []
    if index < 0 or index >= len(clips):
        raise HTTPException(status_code=404, detail="No such clip.")
    clip = clips[index]
    s = clip["start"] if start is None else start
    e = clip["end"] if end is None else end

    job_dir = os.path.join(STORAGE_DIR, job_id)
    # Include the planner version. Reusing a v1 whole-clip stacked plan after the
    # adaptive planner shipped would preserve duplicated speakers in old projects
    # even though every new render was fixed.
    # The zoom variant is a different plan for the same range; sharing a cache
    # file with the un-zoomed preview made the Auto Zoom toggle appear to do
    # nothing on the second click.
    cache = os.path.join(
        job_dir,
        (f"reframe_v{reframe.PLAN_VERSION}_{index}_{s:.1f}_{e:.1f}"
         f"{'_zoom' if auto_zoom else ''}.json"),
    )
    if os.path.exists(cache):
        with open(cache) as f:
            return {"plan": json.load(f)}

    source = os.path.join(job_dir, "source.mp4")
    if not os.path.exists(source):
        return {"plan": None}
    ratio = (job.get("options") or {}).get("ratio", "9:16")
    out_w, out_h = render.RATIOS.get(ratio, render.RATIOS["9:16"])
    try:
        transcript = get_job_transcript(job_id)
        speaker_words = (editing.words_in_range(transcript, s, e)
                         if transcript else None)
        zoom_windows = (reframe._keyword_windows(
                            speaker_words or [], clip.get("keywords") or [],
                            clip.get("title") or "")
                        if auto_zoom else None)
        if tpl["frame"] == "auto":
            plan = content.plan(source, s, e, out_w, out_h,
                                speaker_words=speaker_words,
                                zoom_windows=zoom_windows, variant=index,
                                title=clip.get("title", ""), broll=auto_broll)
        else:
            plan = reframe.plan(source, s, e, out_w, out_h,
                                speaker_words=speaker_words,
                                zoom_windows=zoom_windows)
    except Exception as exc:
        print(f"[clipper] editor reframe preview failed: {exc}")
        return {"plan": None}
    if plan:
        with open(cache, "w") as f:
            json.dump(plan, f)
    return {"plan": plan}


@app.get("/jobs/{job_id}/transcript")
def job_transcript(job_id: str):
    """Utterances with word timings, shaped for the editor timeline."""
    transcript = get_job_transcript(job_id)
    if transcript is None:
        raise HTTPException(
            status_code=404,
            detail="No transcript stored for this job. Jobs created before editing "
                   "existed cannot be edited -- re-run the video to enable it.",
        )
    utterances = transcriber.get_utterances(transcript)
    # The source length, not the last word: silent gameplay has no words, and a
    # duration of 0 collapsed the editor's trim bar to nothing (both ends read
    # 0:00.0 and neither handle could move).
    source_seconds = (get_job(job_id) or {}).get("source_duration") or 0
    return {
        "duration": max(utterances[-1]["end"] if utterances else 0, source_seconds),
        # Word timings travel too: the editor reproduces the renderer's cue
        # chunking from them, so the preview breaks captions in exactly the same
        # places the exported video will. Showing whole sentences there was a
        # preview that lied.
        "cue_rules": {
            "max_words": subtitles.MAX_WORDS_PER_CUE,
            "max_seconds": subtitles.MAX_CUE_SECONDS,
        },
        "utterances": [
            {
                "start": u["start"], "end": u["end"], "text": u["transcript"],
                "words": [
                    {"t": w["start"], "e": w["end"],
                     "w": w.get("punctuated_word") or w.get("word", "")}
                    for w in u.get("words", [])
                ],
            }
            for u in utterances
        ],
    }


def _serve_gameplay_file(filename: str | None, job_id: str | None, request: Request):
    """Shared range-serving logic for gameplay endpoints."""
    if filename:
        safe = os.path.basename(filename)
        path = os.path.join(GAMEPLAY_DIR, safe)
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="Gameplay file not found")
    else:
        loops = sorted(
            [os.path.join(GAMEPLAY_DIR, f) for f in os.listdir(GAMEPLAY_DIR)
             if f.lower().endswith((".mp4", ".mov", ".webm"))],
        )
        if not loops:
            raise HTTPException(status_code=404, detail="No gameplay assets available")
        path = loops[hash(job_id) % len(loops)]

    ext = os.path.splitext(path)[1].lower()
    media_type = {".webm": "video/webm", ".mp4": "video/mp4"}.get(ext, "video/mp4")
    size = os.path.getsize(path)

    if request.method == "HEAD":
        return Response(status_code=200, media_type=media_type,
                        headers={"Accept-Ranges": "bytes",
                                 "Content-Length": str(size)})

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type,
                            headers={"Accept-Ranges": "bytes"})

    try:
        _, _, span = range_header.partition("=")
        start_s, _, end_s = span.partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        raise HTTPException(status_code=416, detail="Malformed Range header.")

    if start >= size:
        return Response(status_code=416,
                        headers={"Content-Range": f"bytes */{size}"})
    start = max(0, min(start, size - 1))
    end = max(start, min(end, size - 1))

    def chunks():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = f.read(min(65536, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        chunks(), status_code=206, media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
    )


@app.api_route("/gameplay-file/{filename}", methods=["GET", "HEAD"])
def serve_gameplay_file(filename: str, request: Request):
    """Serve a specific gameplay file by name (for the editor picker preview)."""
    return _serve_gameplay_file(filename, None, request)


@app.api_route("/jobs/{job_id}/gameplay", methods=["GET", "HEAD"])
def job_gameplay(job_id: str, request: Request, file: str = None):
    """The gameplay loop this job renders with, so the editor can show the
    split-screen composition instead of the bare source."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    tpl = TEMPLATES.get((job.get("options") or {}).get("template", "classic"))
    if not tpl or tpl["frame"] != "gameplay":
        raise HTTPException(status_code=404, detail="This job is not a split-screen job")

    return _serve_gameplay_file(file, job_id, request)


@app.api_route("/jobs/{job_id}/preview", methods=["GET", "HEAD"])
def job_preview(job_id: str, request: Request, clip: int = None):
    """Streams the editor proxy with HTTP range support.

    Range support is not optional here -- without it a browser cannot seek, and
    the whole point of the proxy is scrubbing.

    `clip` selects that clip's own windowed proxy, which is what current jobs
    build. Jobs made before windowing fall back to the whole-source preview.mp4,
    so opening an old job still works; its offset is simply zero.

    HEAD is declared explicitly. FastAPI, unlike bare Starlette, does NOT add it
    alongside GET, so the editor's "is the proxy ready yet?" poll was getting 405
    forever and the loading veil never lifted -- over a video that was in fact
    playing underneath it.
    """
    # On object storage, hand the browser a signed URL and get out of the way.
    # Range requests are what makes scrubbing work, and R2 already implements
    # them correctly -- proxying the bytes here would mean re-implementing that
    # by hand AND paying egress on every scrub.
    if storage.driver.serves_redirects:
        name = f"preview_{clip}.mp4" if clip is not None else "preview.mp4"
        key = storage.job_key(job_id, name)
        if not storage.driver.exists(key):
            key = storage.job_key(job_id, "preview.mp4")   # pre-windowing jobs
        if storage.driver.exists(key):
            if request.method == "HEAD":
                # The editor polls HEAD to know when the proxy is ready; it
                # only needs the status, so do not mint a URL for it.
                return Response(status_code=200, media_type="video/mp4")
            return RedirectResponse(storage.driver.url(key), status_code=302)
        raise HTTPException(status_code=404, detail="No editor preview for this job.")

    job_dir = os.path.join(STORAGE_DIR, job_id)
    path = os.path.join(job_dir, "preview.mp4")
    if clip is not None:
        windowed = os.path.join(job_dir, f"preview_{clip}.mp4")
        if os.path.exists(windowed):
            path = windowed
    if not os.path.exists(path):
        raise HTTPException(
            status_code=404,
            detail="No editor preview for this job. Jobs made before live editing "
                   "existed do not have one -- re-run the video.",
        )

    size = os.path.getsize(path)

    if request.method == "HEAD":
        return Response(status_code=200, media_type="video/mp4",
                        headers={"Accept-Ranges": "bytes",
                                 "Content-Length": str(size)})
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type="video/mp4",
                            headers={"Accept-Ranges": "bytes"})

    try:
        units, _, span = range_header.partition("=")
        start_s, _, end_s = span.partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        raise HTTPException(status_code=416, detail="Malformed Range header.")
    start = max(0, min(start, size - 1))
    end = max(start, min(end, size - 1))

    def chunks():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = f.read(min(65536, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        chunks(), status_code=206, media_type="video/mp4",
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
    )


class Overlay(BaseModel):
    platform: Optional[str] = None          # instagram/x/tiktok/... draws the mark
    t_start: Optional[float] = None         # seconds from clip start; None = from 0
    t_end: Optional[float] = None           # None = until the clip ends
    """A logo or piece of text burned in at export.

    x/y/size are fractions of the frame (see pipeline/overlays.py) so one recipe
    survives a ratio change. `file` names an uploaded image inside the job dir --
    never a path, so a recipe cannot be used to read arbitrary files.
    """
    type: str = "text"
    text: Optional[str] = None
    file: Optional[str] = None
    x: float = 0.5
    y: float = 0.9
    size: float = 0.045
    color: Optional[str] = "#ffffff"
    font: Optional[str] = None
    plate: Optional[str] = None
    opacity: float = 1.0


EDITOR_TEMPLATES = {"classic", "fitvideo", "gameplay", "podcast", "auto"}


class ClipRecipe(BaseModel):
    """A clip described rather than rendered."""
    start: float
    end: float
    ratio: Optional[str] = None
    template: Optional[str] = None
    caption_style: Optional[str] = None
    caption_font: Optional[str] = None
    translate_to: Optional[str] = None
    lines: Optional[list[CaptionLine]] = None
    overlays: Optional[list[Overlay]] = None
    title: Optional[str] = None             # burned into the video and used as filename
    caption_size: Optional[int] = None      # px on the 1080x1920 reference canvas
    caption_pos: Optional[float] = None     # 0..1 down the frame, centre of the block
    caption_anim: Optional[str] = None      # entrance animation preset
    # Overriding the preset's colours is the change people reach for first --
    # every preset ships white text, so the style picker alone only ever changed
    # how the spoken word was marked.
    caption_color: Optional[str] = None     # CSS hex for the resting text
    caption_active_color: Optional[str] = None   # CSS hex for the spoken word
    captions_on: Optional[bool] = None      # False = export with no captions
    keyword_captions: Optional[bool] = None # with captions off: keyword pop-ups
    auto_emoji: Optional[bool] = None       # False = export without emoji
    words_per_cue: Optional[int] = None     # 1/2/3 words per caption cue
    auto_zoom: Optional[bool] = None        # punch-in on key words
    auto_transitions: Optional[bool] = None # dip through black at cuts
    auto_sfx: Optional[bool] = None         # whoosh on layout cuts
    music_bed: Optional[bool] = None        # ducked background music
    auto_broll: Optional[bool] = None       # gameplay cutaways
    speed: Optional[float] = None           # 0.5 - 3.0 playback speed
    speed_pitched: Optional[bool] = None    # True = pitch rides with speed (meme)
    tighten_pauses: Optional[bool] = None   # per-clip original/tight pacing
    background: Optional[str] = None        # bar colour for the fit/pad frame
    # The title is its own design decision, not a second caption. Look and
    # typeface move independently: the same white plate reads very differently
    # in Anton than in Merriweather.
    title_style: Optional[str] = None       # key into subtitles.TITLE_LOOKS
    title_font: Optional[str] = None        # font id, independent of captions
    title_size: Optional[int] = None        # px on the 1080x1920 reference canvas
    gameplay_file: Optional[str] = None     # filename in assets/gameplay/


CAPTION_RECIPE_DEFAULTS = {
    "caption_font": None,
    "caption_size": None,
    "caption_pos": None,
    "caption_anim": "none",
    "caption_color": None,
    "caption_active_color": None,
    "translate_to": None,
    "lines": None,
}


@app.put("/jobs/{job_id}/clips/{index}/edit")
def save_clip_edit(job_id: str, index: int, body: ClipRecipe,
                   user: User = Depends(auth.current_user_required)):
    """Stores the recipe. Costs nothing -- no render, no API calls.

    The rendered file is now stale, which `rendered: false` records so the UI
    can offer an export.
    """
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        clip = (session.query(Clip)
                .filter(Clip.job_id == job_id, Clip.index == index).first())
        if not clip:
            raise HTTPException(status_code=404, detail="Clip not found")

        if job.user_id and job.user_id != user.id:
            raise HTTPException(status_code=403, detail="That clip belongs to another account.")

        incoming = body.dict(exclude_unset=True)
        # Caption editing is deliberately NOT gated here. This endpoint stores a
        # JSON recipe -- it renders nothing -- and the editor draws its preview
        # in the browser over the proxy, so a free account experimenting with
        # captions costs exactly one database row. Gating it meant someone on
        # Free could never find out what the editor does before being asked to
        # pay for it. The limit that matters is _reserve_editor_export, which
        # every rendering path goes through.
        if incoming.get("tighten_pauses") is True:
            _gate(user, "tighten_pauses",
                  "Automatic pause tightening is available on Creator and Pro.")

        recipe = body.dict(exclude_none=True)
        if body.lines is not None:
            recipe["lines"] = [l.dict() for l in body.lines]
        if body.overlays is not None:
            recipe["overlays"] = [o.dict() for o in body.overlays]

        clip.edit = recipe
        if body.title is not None and body.title.strip():
            clip.title = body.title.strip()[:120]
        # Do not mutate the baked clip bounds until export. The per-clip proxy
        # was encoded around those baked bounds, and changing them here makes a
        # reopened editor calculate the wrong proxy offset for the same file.
        # The recipe carries the draft trim; export commits it.
        options = (job.options or {}) if job else {}
        template = TEMPLATES.get(options.get("template", "classic"),
                                 TEMPLATES["classic"])
        clip.duration = editing.tightened_duration(
            job.transcript if job else None,
            body.start,
            body.end,
            recipe.get("lines"),
            recipe.get("tighten_pauses", options.get("tighten_pauses", True)),
            body.speed,
            pacing.max_pause_for_frame(template["frame"]),
        )
        clip.rendered = False
        session.commit()
        return clip.to_dict()


# Only formats ffmpeg can actually composite, and small enough that a logo
# cannot be used to fill the disk.
LOGO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
MAX_LOGO_BYTES = 4 * 1024 * 1024


@app.post("/jobs/{job_id}/overlays/upload")
async def upload_overlay_image(job_id: str, file: UploadFile = File(...),
                               user: User = Depends(auth.current_user_required)):
    """Stores a logo for this job and returns the name to put in a recipe."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.user_id and job.user_id != user.id:
            raise HTTPException(status_code=403, detail="That clip belongs to another account.")
    _gate(user, "branding", "Adding your own logo or handle is available on Creator and Pro.")

    if file.content_type not in LOGO_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Logos must be PNG, JPEG or WebP. PNG keeps transparency.",
        )
    data = await file.read()
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=400, detail="Logo must be under 4 MB.")
    if not data:
        raise HTTPException(status_code=400, detail="That file is empty.")

    out_dir = os.path.join(STORAGE_DIR, job_id, "overlays")
    os.makedirs(out_dir, exist_ok=True)
    name = f"logo_{uuid.uuid4().hex[:10]}{LOGO_TYPES[file.content_type]}"
    with open(os.path.join(out_dir, name), "wb") as f:
        f.write(data)
    return {"file": name, "url": f"/jobs/{job_id}/overlays/{name}"}


@app.get("/jobs/{job_id}/overlays/{name}")
def get_overlay_image(job_id: str, name: str):
    """Serves an uploaded logo so the editor can preview it."""
    path = os.path.join(STORAGE_DIR, job_id, "overlays", os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Overlay image not found")
    return FileResponse(path)


def _reserve_editor_export(job_id: str, index: int, user: User = None) -> Optional[str]:
    """Reserve the one edited export included with Free.

    The row is written before ffmpeg starts, closing the double-click/concurrent
    request hole. Callers delete it when rendering fails, so failed work never
    spends the allowance.
    """
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.user_id and (not user or user.id != job.user_id):
            raise HTTPException(status_code=403, detail="That clip belongs to another account.")

        if user and billing.allows(user.plan, "no_watermark", user.email):
            return None

        reservation = FreeEditorExport(
            user_id=user.id if user else None,
            job_id=job_id,
            clip_index=index,
        )
        session.add(reservation)
        try:
            session.commit()
            return reservation.id
        except IntegrityError:
            session.rollback()
            raise HTTPException(
                status_code=402,
                detail=("Your free edited export has already been used. "
                        "Upgrade to Creator for unlimited edited exports."),
            )


def _release_editor_export(reservation_id: Optional[str]) -> None:
    if not reservation_id:
        return
    with SessionLocal() as session:
        row = session.get(FreeEditorExport, reservation_id)
        if row:
            session.delete(row)
            session.commit()


@app.post("/jobs/{job_id}/clips/{index}/export")
def export_clip(job_id: str, index: int,
                user: User = Depends(auth.current_user_required)):
    """Renders the stored recipe to a real MP4. The only expensive operation."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    with SessionLocal() as session:
        clip = (session.query(Clip)
                .filter(Clip.job_id == job_id, Clip.index == index).first())
        if not clip:
            raise HTTPException(status_code=404, detail="Clip not found")
        recipe = dict(clip.edit or {})
        # The one export included with Free renders what the user actually
        # built, captions and all. Spending that single allowance only to
        # receive the unedited clip -- with the changes they had just made
        # silently dropped -- would be the worst possible first impression.
        # _reserve_editor_export below is what holds the line.
        if recipe.get("tighten_pauses", (job.get("options") or {}).get(
                "tighten_pauses", True)):
            _gate(user, "tighten_pauses",
                  "Automatic pause tightening is available on Creator and Pro.")
        start = recipe.get("start", clip.start)
        end = recipe.get("end", clip.end)

    # Resolve uploaded image names to paths, refusing anything that tries to
    # escape the job directory.
    job_dir = os.path.join(STORAGE_DIR, job_id)
    resolved = []
    for ov in (recipe.get("overlays") or []):
        ov = dict(ov)
        if ov.get("type") == "image" and ov.get("file"):
            name = os.path.basename(ov["file"])
            path = os.path.join(job_dir, "overlays", name)
            if not os.path.exists(path):
                continue
            ov["path"] = path
        resolved.append(ov)

    tpl_key = (job.get("options") or {}).get("template", "classic")
    recipe_tpl = recipe.get("template")
    if recipe_tpl and recipe_tpl in EDITOR_TEMPLATES:
        tpl_key = recipe_tpl
    tpl = TEMPLATES.get(tpl_key)
    chosen_gameplay = recipe.get("gameplay_file")
    if tpl and tpl["frame"] == "gameplay" and chosen_gameplay:
        safe = os.path.basename(chosen_gameplay)
        gp = os.path.join(GAMEPLAY_DIR, safe)
        loops = [gp] if os.path.exists(gp) else _gameplay_loops()
    else:
        loops = _gameplay_loops() if tpl and tpl["frame"] == "gameplay" else []
    reservation_id = _reserve_editor_export(job_id, index, user)
    try:
        # Renders on the request thread, so it competes for memory with any
        # pipeline job already running. Bounded wait rather than an open-ended
        # one: the caller is a browser holding a connection open.
        with _render_slot(wait=EXPORT_SLOT_WAIT_SECONDS):
            result = editing.rerender_clip(
                job_id, index, start, end, STORAGE_DIR, TEMPLATES, loops,
                caption_style=recipe.get("caption_style"),
                caption_lines=recipe.get("lines"),
                caption_font=recipe.get("caption_font"),
                translate_to=recipe.get("translate_to"),
                ratio=recipe.get("ratio"),
                overlay_list=resolved,
                caption_size=recipe.get("caption_size"),
                caption_pos=recipe.get("caption_pos"),
                caption_anim=recipe.get("caption_anim"),
                caption_color=recipe.get("caption_color"),
                caption_active_color=recipe.get("caption_active_color"),
                captions_on=recipe.get("captions_on", True),
                keyword_captions=recipe.get("keyword_captions"),
                auto_emoji=recipe.get("auto_emoji"),
                words_per_cue=recipe.get("words_per_cue"),
                auto_zoom=recipe.get("auto_zoom"),
                auto_transitions=recipe.get("auto_transitions"),
                auto_sfx=recipe.get("auto_sfx"),
                music_bed=recipe.get("music_bed"),
                auto_broll=recipe.get("auto_broll"),
                speed=recipe.get("speed"),
                speed_pitched=bool(recipe.get("speed_pitched")),
                background=recipe.get("background"),
                title_style=recipe.get("title_style"),
                title_font=recipe.get("title_font"),
                title_size=recipe.get("title_size"),
                tighten_pauses=recipe.get("tighten_pauses"),
                template_override=tpl_key,
            )
        with SessionLocal() as session:
            clip = (session.query(Clip)
                    .filter(Clip.job_id == job_id, Clip.index == index).first())
            clip.rendered = True
            session.commit()
            # Re-read inside this session: `result` was serialised before the
            # flag was flipped and would tell the UI the export is still pending.
            return clip.to_dict()
    except ValueError as e:
        _release_editor_export(reservation_id)
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        _release_editor_export(reservation_id)
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")
    except HTTPException:
        # A deliberate status (the busy-slot 503) must survive the catch-all
        # below, which would otherwise flatten it to a generic 500.
        _release_editor_export(reservation_id)
        raise
    except Exception:
        _release_editor_export(reservation_id)
        print(f"[clipper] unexpected editor export failure\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="We couldn't finish this export right now.")


@app.post("/jobs/{job_id}/clips/{index}/rerender")
def rerender_clip(job_id: str, index: int, body: ClipEdit,
                  user: User = Depends(auth.current_user_required)):
    """Re-cuts one clip to new in/out points, reusing the stored transcript."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Same reasoning as the export path: this re-cut is metered by
    # _reserve_editor_export a few lines down, so the caption settings riding
    # along with it need no separate gate of their own.
    tpl = TEMPLATES.get((job.get("options") or {}).get("template", "classic"))
    loops = _gameplay_loops() if tpl and tpl["frame"] == "gameplay" else []
    reservation_id = _reserve_editor_export(job_id, index, user)
    try:
        with _render_slot(wait=EXPORT_SLOT_WAIT_SECONDS):
            return editing.rerender_clip(
                job_id, index, body.start, body.end, STORAGE_DIR, TEMPLATES, loops,
                caption_style=body.caption_style,
                caption_lines=[l.dict() for l in body.caption_lines] if body.caption_lines else None,
                caption_font=body.caption_font,
                translate_to=body.translate_to,
                auto_emoji=body.auto_emoji,
                words_per_cue=body.words_per_cue,
                auto_zoom=body.auto_zoom,
                auto_transitions=body.auto_transitions,
                auto_sfx=body.auto_sfx,
                music_bed=body.music_bed,
                auto_broll=body.auto_broll,
            )
    except ValueError as e:
        _release_editor_export(reservation_id)
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        _release_editor_export(reservation_id)
        raise HTTPException(status_code=500, detail=f"Re-render failed: {e}")
    except HTTPException:
        # Already a considered answer -- the 503 from a busy render slot, say.
        # Without this the catch-all below would rewrite it into a generic 500
        # and lose both the real status and the Retry-After.
        _release_editor_export(reservation_id)
        raise
    except Exception:
        _release_editor_export(reservation_id)
        print(f"[clipper] unexpected re-render failure\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="We couldn't finish this export right now.")


def _gate(user: User, feature: str, message: str) -> None:
    """Rejects a paid capability. One helper so every gate reads the same.

    Enforced here rather than trusted from the client: the UI locks these
    controls, but locking a control is presentation, not a gate -- the request
    can still arrive with the flag set.
    """
    if not billing.allows(user.plan if user else None, feature,
                          user.email if user else None):
        raise HTTPException(status_code=402, detail=message)


def _prepare_job(req: JobRequest, user: User = None, uploaded: bool = False) -> list:
    """Validate shared job options and return any pre-resolved gameplay loops."""
    if not uploaded:
        parsed = urlparse(req.url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(status_code=400, detail="Paste a public video link starting with http:// or https://.")
    tpl = TEMPLATES.get(req.template)
    if not tpl:
        raise HTTPException(status_code=400,
                            detail=f"Unknown template. Choose from: {', '.join(TEMPLATES)}")
    if req.genre not in GENRE_CHOICES:
        raise HTTPException(status_code=400,
                            detail=f"Unknown genre. Choose from: {', '.join(GENRE_CHOICES)}")

    if req.output not in OUTPUT_CHOICES:
        raise HTTPException(status_code=400,
                            detail=f"Unknown output. Choose from: {', '.join(OUTPUT_CHOICES)}")
    highlight_video = req.output == "highlight"
    if highlight_video:
        if req.mode != "original":
            raise HTTPException(status_code=400,
                                detail="A highlight video keeps the original audio.")
        if req.highlight_minutes is not None and not (
                1 <= req.highlight_minutes <= highlight.MAX_TARGET / 60):
            raise HTTPException(
                status_code=400,
                detail=f"Highlight length must be 1 to {highlight.MAX_TARGET // 60} minutes.")

    limits = billing.limits_for(user.plan if user else None,
                               user.email if user else None)
    if not highlight_video and req.n_clips > limits["max_clips"]:
        raise HTTPException(
            status_code=402,
            detail=(f"Your plan covers up to {limits['max_clips']} clips per video. "
                    f"Upgrade to ask for {req.n_clips} in one run."),
        )

    if tpl["frame"] == "gameplay":
        _gate(user, "gameplay",
              "Gameplay split-screen is on the Creator and Pro plans. It pairs your "
              "clip with looping footage to hold attention through the whole video.")
    if req.tighten_pauses:
        _gate(user, "tighten_pauses",
              "Pause tightening is on the Creator and Pro plans. It cuts the dead air "
              "so a clip feels edited rather than merely trimmed.")

    # Resolve the gameplay asset NOW so a missing file fails the request with a
    # clear message instead of dying mid-pipeline after download+transcription.
    gameplay_loops = _gameplay_loops() if tpl["frame"] == "gameplay" else []

    # Per-clip pricing means the price of what was ASKED for is known up front,
    # so the gate can be exact instead of "has any credit at all". Charged for
    # real once the analysis settles the actual clip count, which may be lower.
    if user and highlight_video:
        # The real length is only known once the moments are chosen, so the
        # gate asks for what was requested, or the shortest auto length.
        seconds = (req.highlight_minutes or highlight.MIN_TARGET / 60) * 60
        want = billing.credits_for_highlight(seconds, req.high_quality,
                                             req.advanced_model)
        have = billing.balance(user.id)
        if have < want:
            raise HTTPException(
                status_code=402,
                detail=(f"A highlight video of about {seconds / 60:.0f} minutes costs "
                        f"at least {want} credits and you have {have}. Top up, "
                        f"pick a shorter length, or make clips instead."))
    elif user:
        want = billing.credits_for_job(req.n_clips, req.high_quality,
                                       req.advanced_model)
        have = billing.balance(user.id)
        if have < want:
            # Name the extras when they are what pushed it over, so the fix on
            # offer is "turn one off", not only "buy more".
            extras = [n for n, on in (("High quality", req.high_quality),
                                      ("Advanced model", req.advanced_model)) if on]
            with_extras = f" with {' and '.join(extras)}" if extras else ""
            raise HTTPException(
                status_code=402,
                detail=(f"{req.n_clips} clips{with_extras} cost {want} credits and "
                        f"you have {have}. Ask for fewer clips, turn off an extra, "
                        f"top up, or upgrade."),
            )

    # Paid capability, enforced here rather than trusted from the client.
    if req.multilingual:
        _gate(user, "multilingual",
              "Multilingual captions are on the Creator and Pro plans. "
              "They transcribe speech that switches between languages "
              "mid-sentence, which the standard model transcribes as noise.")

    # The template's frame mode is resolved once, here, and stored with the job.
    # The editor has to preview the same composition the renderer builds, and it
    # was inferring that from the template name -- so every template added since
    # was silently previewed as `blur`, whatever it actually renders as.
    return gameplay_loops


def _gate_source_duration(duration_seconds: Optional[float], user: User = None) -> None:
    """Block expensive work before transcription or analysis starts."""
    limit = billing.limits_for(user.plan if user else None,
                               user.email if user else None)["max_source_minutes"]
    if duration_seconds is None and limit <= billing.LIMITS["free"]["max_source_minutes"]:
        raise HTTPException(
            status_code=402,
            detail="We couldn't confirm this video's length on the Free plan. Upload a file instead, or choose a plan with longer sources.",
        )
    if duration_seconds and duration_seconds > limit * 60:
        raise HTTPException(
            status_code=402,
            detail=f"This video is longer than the {limit}-minute limit on your plan.",
        )


def _start_job(req: JobRequest, background_tasks: BackgroundTasks,
               user: User = None, uploaded: bool = False, gameplay_loops: list = None,
               enqueue: bool = True) -> str:
    options = req.dict()
    options["frame"] = TEMPLATES.get(req.template, TEMPLATES["classic"])["frame"]
    options["input_type"] = "upload" if uploaded else "link"
    # Plan decisions are frozen onto the job at creation, not looked up while it
    # renders. A subscription that starts or lapses mid-render must not change
    # what the clip looks like, and a re-export months later has to match the
    # file it replaces.
    plan_id = user.plan if user else None
    email = user.email if user else None
    options["watermark"] = ("" if billing.allows(plan_id, "no_watermark", email)
                            else billing.WATERMARK_TEXT)
    plan_limits = billing.limits_for(plan_id, email)
    options["max_source_minutes"] = plan_limits["max_source_minutes"]
    options["max_video_height"] = plan_limits.get("max_video_height", 1080)
    options["retention_until"] = (
        datetime.now(timezone.utc)
        + timedelta(hours=billing.retention_hours_for(plan_id, email))
    ).isoformat()
    job_id = create_job(req.url, options, user_id=user.id if user else None)
    # Onto the queue rather than into this process. BackgroundTasks ran the
    # render inside the web server, so a redeploy or an OOM kill lost every job
    # in flight with no record they had ever started -- and the user had already
    # been charged. A claimed row can be released and picked up again.
    #
    # `background_tasks` is still accepted so the signature is unchanged for
    # callers; nothing is scheduled on it any more.
    #
    # Uploads defer this: their source is not reachable by the worker until it
    # has been published, and a queued job whose input does not exist yet is a
    # job a worker can claim and fail. See submit_uploaded_job.
    if enqueue:
        queue.enqueue(job_id, priority=_job_priority(user))
    return job_id


def _job_priority(user: User = None) -> int:
    """Where this job enters the queue.

    The first thing that makes billing's "priority" entitlement real: until
    now it was declared on the Pro plan, advertised as "Priority rendering",
    and used precisely nowhere.
    """
    if user and billing.allows(user.plan, "priority", user.email):
        return queue.PRIORITY_PRO
    return 0


def _await_uploaded_source(job_id: str, job_dir: str, timeout: float = 120.0):
    """The uploaded source, waiting for it to appear in storage if necessary.

    The upload is published AFTER the job is enqueued, so a worker that is idle
    when the job lands can claim it before the last bytes have been stored.
    That window is short but entirely real, and losing it means telling someone
    their file is missing seconds after they watched it upload.

    Polls rather than coordinates: a flag saying "source ready" would be one
    more thing to get wrong, and the file's own existence already answers the
    question.
    """
    deadline = time.monotonic() + timeout
    delay = 0.5
    while True:
        path = storage.ensure_local(job_id, job_dir, "source.mp4")
        if path:
            return path
        if time.monotonic() >= deadline:
            return None
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)


def run_queued_job(job_id: str) -> None:
    """Run one claimed job. The worker's only entry point into the pipeline.

    Everything needed is rebuilt from what was frozen onto the job at
    submission, never from live state: the worker may be a different process on
    a different machine running days later, and the job must render as it was
    agreed to, not as the account looks now.
    """
    with SessionLocal() as session:
        row = session.get(Job, job_id)
        if not row:
            print(f"[worker] job {job_id} vanished before it ran")
            return
        options = dict(row.options or {})
        job_user_id = row.user_id
        url = row.url

    # Only the fields JobRequest actually declares: options also carries the
    # frozen plan decisions (watermark, retention, ceilings), which are read
    # directly by the pipeline and would be rejected as unknown fields here.
    #
    # options already contains `url` -- it is req.dict() -- so the row's value
    # is assigned over the top rather than passed as a second keyword, which is
    # a duplicate argument and a TypeError on every single job.
    fields = set(JobRequest.model_fields)
    data = {k: v for k, v in options.items() if k in fields}
    # The Job row is authoritative. Keep unknown keys out, and fail the job
    # cleanly rather than raising here: this runs before run_pipeline's handler,
    # so an exception would be retried three times and leave the user staring at
    # "Starting now" forever.
    data["url"] = url
    try:
        req = JobRequest(**data)
    except Exception as exc:
        message = (f"This job's saved settings could not be read ({exc.__class__.__name__}). "
                   "Run the video again with the current version.")
        print(f"[worker] job {job_id} has unusable options: {exc}")
        update_job(job_id, status="failed", error=message)
        return

    uploaded = options.get("input_type") == "upload"
    # Re-resolved rather than carried: the loops are files on the worker's own
    # disk, and a path from whichever machine accepted the request means
    # nothing here.
    tpl = TEMPLATES.get(req.template, TEMPLATES["classic"])
    gameplay_loops = _gameplay_loops() if tpl["frame"] == "gameplay" else []

    run_pipeline(job_id, req, gameplay_loops, job_user_id, uploaded)


@app.post("/sources/preview")
def source_preview(body: SourcePreviewRequest):
    """Confirm a public URL's identity before a user starts a job."""
    parsed = urlparse(body.url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Paste a public video link starting with http:// or https://.")
    try:
        return downloader.inspect_url(body.url.strip())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/jobs")
def submit_job(req: JobRequest, request: Request, background_tasks: BackgroundTasks,
               user: User = Depends(auth.current_user_required)):
    if not turnstile.verify(req.turnstile_token,
                            request.client.host if request.client else None):
        raise HTTPException(status_code=403,
                            detail="Verification failed. Please refresh the page and try again.")
    gameplay_loops = _prepare_job(req, user)
    # Metadata-only inspection happens before a job is created, so an over-limit
    # link never downloads media or reaches a paid transcription/analysis call.
    try:
        metadata = downloader.inspect_url(req.url.strip())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _gate_source_duration(metadata.get("duration"), user)
    req.source_title = (metadata.get("title") or "")[:200]
    job_id = _start_job(req, background_tasks, user, gameplay_loops=gameplay_loops)
    return {"job_id": job_id}


def _remove_failed_upload(job_id: str) -> None:
    """Remove a just-created job if its source file fails validation."""
    shutil.rmtree(os.path.join(STORAGE_DIR, job_id), ignore_errors=True)
    with SessionLocal() as session:
        row = session.get(Job, job_id)
        if row:
            session.delete(row)
            session.commit()


def _assert_video_file(path: str) -> float:
    """Reject a renamed text/archive file before it reaches ffmpeg workers."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "format=duration:stream=codec_type", "-of", "json", path],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(probe.stdout)
        has_video = any(s.get("codec_type") == "video" for s in data.get("streams", []))
        duration = float(data.get("format", {}).get("duration") or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
        has_video, duration = False, 0
    if probe.returncode != 0 or not has_video:
        raise HTTPException(status_code=400, detail="That file is not a readable video.")
    return duration


@app.post("/jobs/upload")
async def submit_uploaded_job(request: Request, background_tasks: BackgroundTasks,
                              file: UploadFile = File(...), options: str = Form("{}"),
                              turnstile_token: str = Form(""),
                              user: User = Depends(auth.current_user_required)):
    """Create a job from a local video without involving a platform downloader."""
    if not turnstile.verify(turnstile_token,
                            request.client.host if request.client else None):
        raise HTTPException(status_code=403,
                            detail="Verification failed. Please refresh the page and try again.")
    try:
        req = JobRequest(**json.loads(options))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Upload settings were invalid.") from exc

    name = os.path.basename(file.filename or "video.mp4")
    ext = os.path.splitext(name)[1].lower()
    if ext not in VIDEO_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Upload an MP4, MOV, M4V, WebM or MKV video.")
    if (file.content_type and file.content_type != "application/octet-stream"
            and not file.content_type.startswith("video/")):
        raise HTTPException(status_code=400, detail="Choose a video file to upload.")

    gameplay_loops = _prepare_job(req, user, uploaded=True)
    # Per-plan ceiling, checked while streaming rather than from a header: a
    # Content-Length is whatever the client claims it is.
    max_bytes = min(
        MAX_UPLOAD_BYTES,
        billing.limits_for(user.plan if user else None,
                           user.email if user else None)["max_upload_mb"] * 1024 * 1024,
    )
    req.url = f"upload://{name}"
    upload_id = f"upload-{uuid.uuid4().hex}"
    job_dir = os.path.join(STORAGE_DIR, upload_id)
    os.makedirs(job_dir, exist_ok=True)
    part_path = os.path.join(job_dir, "source.mp4.part")
    source_path = os.path.join(job_dir, "source.mp4")
    written = 0

    try:
        with open(part_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(f"Your plan allows uploads up to "
                                f"{max_bytes // (1024 * 1024)} MB. Upgrade for larger "
                                f"files, or paste a link instead."),
                    )
                out.write(chunk)
        if not written:
            raise HTTPException(status_code=400, detail="That upload was empty.")
        os.replace(part_path, source_path)
        _gate_source_duration(_assert_video_file(source_path), user)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    # Created but NOT queued yet: the job id is the storage key, so the row has
    # to exist before the file can be published, and the file has to exist
    # before any worker may see the job.
    job_id = _start_job(req, background_tasks, user, uploaded=True,
                        gameplay_loops=gameplay_loops, enqueue=False)
    final_dir = os.path.join(STORAGE_DIR, job_id)
    os.replace(job_dir, final_dir)

    # The API and the worker are different containers with different disks, so
    # a file sitting here is invisible to whichever worker picks the job up.
    # Publishing makes the source fetchable by key; the worker pulls it back
    # with storage.ensure_local.
    #
    # This MUST finish before the job is queued. Publishing afterwards left a
    # window where a worker claimed the job, looked for a source that was still
    # uploading, and failed it -- and on a container with an idle worker that
    # window is hit every time, not occasionally.
    try:
        update_job(job_id, progress_message="Storing your video...")
        storage.publish(job_id, final_dir, ["source.mp4"])
        # The invariant is that the source is in SHARED storage, not merely
        # that it exists. Those differ in the one case that matters: on the
        # local driver publish() is a no-op and exists() then checks this
        # container's own disk, finds the file it just wrote, and reports
        # success -- while the worker, elsewhere, has no way to reach it.
        # Checking the file was therefore self-confirming and caught nothing.
        if not INLINE_WORKER and storage.driver.name == "local":
            raise RuntimeError(
                "This API has no shared storage configured (STORAGE_BACKEND is "
                "not set) but renders on a separate worker, so an upload saved "
                "here is unreachable from there. Set STORAGE_BACKEND=r2 and the "
                "R2_* variables on THIS service, matching the worker.")
        if not storage.driver.exists(storage.job_key(job_id, "source.mp4")):
            raise RuntimeError(
                f"source.mp4 is not readable at its key after publishing "
                f"(storage backend: {storage.driver.name}).")
    except Exception as exc:
        traceback.print_exc()
        update_job(job_id, status="failed",
                   error="We couldn't store your upload. Please try again.")
        raise HTTPException(
            status_code=502,
            detail="We couldn't store your upload. Please try again.") from exc

    queue.enqueue(job_id, priority=_job_priority(user))
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # How many jobs are ahead, while this one is still waiting. A wait you can
    # watch shrink is a queue; the same wait with no number is indistinguishable
    # from the app being broken, which is what people close the tab over.
    if job.get("status") == "queued":
        ahead = queue.position(job_id)
        if ahead is not None:
            job["queue_position"] = ahead
            job["progress_message"] = (
                "Starting now..." if ahead == 0
                else f"Waiting — {ahead} job{'' if ahead == 1 else 's'} ahead of you"
            )
    return job


@app.get("/clips/{job_id}/{filename}")
def get_clip_file(job_id: str, filename: str, download: str = None):
    # A filename from the URL must never be able to walk out of the job's own
    # namespace -- on object storage that would sign a URL for someone else's
    # key, which is a far worse outcome than the local `..` traversal it also
    # prevents.
    filename = os.path.basename(filename)

    # Expiry has to be enforced here, not only by the sweep that deletes files.
    # On local disk the two were the same thing: the bytes were gone, so the
    # request 404'd on its own. With a bucket, the delete and this request are
    # independent -- a sweep that has not run yet, or that failed on one key,
    # would otherwise keep serving a clip the user was told had expired.
    job = get_job(job_id)
    if job and job.get("status") == "expired":
        raise HTTPException(
            status_code=410,
            detail="These clips have expired. Re-run the video to make them again.",
        )

    if storage.driver.serves_redirects:
        key = storage.job_key(job_id, filename)
        if not storage.driver.exists(key):
            raise HTTPException(status_code=404, detail="Clip not found")
        # 302 rather than streaming: the bytes go from R2 straight to the
        # browser, so this process pays no egress and stays free for real work.
        #
        # `download` carries the name the user should get. It has to be signed
        # into the URL because the browser drops the <a download> filename the
        # moment a request redirects cross-origin.
        return RedirectResponse(
            storage.driver.url(key, filename=os.path.basename(download or "")),
            status_code=302)

    path = os.path.join(STORAGE_DIR, job_id, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(path, media_type="video/mp4")


def _build_proxies_quietly(job_id: str, video_path: str, job_dir: str,
                           clips: list, duration: float = None) -> None:
    """Builds one scrubbing proxy per clip, on a background thread.

    One proxy per clip, not one for the job. The editor only ever exposes a
    ~105s window around a clip (its trim handles are clamped to it), so a
    whole-source proxy spent minutes encoding footage nobody could reach --
    which is why the editor sat on "Preparing the editor preview" long after
    the clips were done.

    Clips are done in order, so clip 1 -- the one people open first -- is
    playable within seconds of its own render finishing. A failed proxy costs
    live editing for that clip and nothing else, so failure is swallowed per
    clip: this runs unsupervised while the real render is in flight and must
    never take the deliverable down with it.
    """
    for i, clip in enumerate(clips, start=1):
        lo, hi = render.proxy_window(clip["start"], clip["end"], duration)
        out_path = os.path.join(job_dir, f"preview_{i}.mp4")
        try:
            render.make_proxy(video_path, out_path, start=lo, end=hi)
            # Published here, one at a time, rather than in a batch when the job
            # finishes: this thread outlives the pipeline, so a batch upload at
            # job-done would race whichever encode was still running and silently
            # ship a proxy that is not there. Publishing on completion means each
            # one is available exactly when it is genuinely ready.
            storage.publish(job_id, job_dir, [f"preview_{i}.mp4"])
            print(f"[clipper] editor preview ready for {job_id} clip {i} "
                  f"({lo:.0f}s-{hi:.0f}s)")
        except Exception as e:
            # Deliberately broad. boto3 raises S3UploadFailedError and
            # ClientError, which descend straight from Exception and so slipped
            # past the (RuntimeError, OSError) this used to catch -- an R2
            # permission problem then killed this whole thread mid-loop, taking
            # the remaining previews with it and printing a traceback that
            # looked like a crash in the job itself. A proxy is a convenience;
            # nothing that happens to one may escape this loop.
            print(f"[clipper] proxy for {job_id} clip {i} failed (live editing "
                  f"unavailable for it): {e}")


def run_pipeline(job_id: str, req: JobRequest, gameplay_loops: list = None,
                 job_user_id: str = None, uploaded_source: bool = False):
    """Runs one job end to end. Holds a render slot for its whole duration.

    The slot covers transcription as well as rendering, which costs some
    throughput -- transcription waits on Deepgram rather than the CPU, so a
    finer-grained lock could overlap it with someone else's render. That is not
    worth the complexity here: the source file, the proxy and the working set
    all stay resident for the whole job, so it is memory, not CPU, that decides
    how many jobs fit -- and by that measure the answer on this container is
    one. Overlapping the stages would only make the peak arrive sooner.
    """
    with _render_slot(job_id=job_id):
        _run_pipeline_locked(job_id, req, gameplay_loops, job_user_id, uploaded_source)


def _clip_timeline_words(item: dict) -> list:
    """A prepared clip's words on the rendered clip's own timeline.

    Pause tightening already re-times words onto the clip; otherwise they are
    in source time and start at the clip's start.
    """
    shift = 0.0 if item["render_kwargs"]["segments"] else item["clip"]["start"]
    return [{**w, "start": w["start"] - shift, "end": w["end"] - shift}
            for w in (item["clip_words"] or [])]


def _run_pipeline_locked(job_id: str, req: JobRequest, gameplay_loops: list = None,
                         job_user_id: str = None, uploaded_source: bool = False):
    gameplay_loops = gameplay_loops or []
    tpl = TEMPLATES.get(req.template, TEMPLATES["classic"])
    job_dir = os.path.join(STORAGE_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    # Nothing has been billed yet. Set before the try so the failure handler can
    # always read it, including for a failure that happens before the charge.
    charged_credits = 0
    # Frozen at creation by _start_job -- see the note there.
    job_options = (get_job(job_id) or {}).get("options") or {}
    options_watermark = job_options.get("watermark") or None
    max_source_minutes = job_options.get("max_source_minutes") or billing.MAX_SOURCE_MINUTES
    max_video_height = job_options.get("max_video_height", 1080)

    try:
        # Link jobs fetch an audio-only stream first. Uploads already live in
        # this job directory, so extracting audio locally avoids yt-dlp entirely.
        audio_path = os.path.join(job_dir, "audio.mp3")
        source_has_audio = True
        # Only an upload is on disk this early. A link job analyses the audio
        # track alone and fetches the picture after the moments are chosen, so
        # everything before that point has to cope with there being no file.
        video_path = None
        if uploaded_source:
            # Not necessarily on this disk: the container that accepted the
            # upload is not the one rendering it. ensure_local returns the file
            # if it is here and pulls it from storage if it is not.
            update_job(job_id, status="downloading",
                       progress_message="Getting your video ready...", percent=0)
            video_path = _await_uploaded_source(job_id, job_dir)
            if not video_path:
                # Name what was actually looked for. "Upload it again" sent
                # people to re-upload a file that had uploaded perfectly well,
                # over and over, when the real cause was this worker reading a
                # different storage backend than the API wrote to.
                key = storage.job_key(job_id, "source.mp4")
                print(f"[worker] {key!r} not found via the {storage.driver.name} "
                      f"backend after waiting. If the API stored it elsewhere, "
                      f"the two services disagree about STORAGE_BACKEND / "
                      f"R2_BUCKET -- compare them.")
                raise RuntimeError("The uploaded source video is missing. Upload it again.")
            update_job(job_id, status="downloading", progress_message="Getting your video ready...", percent=0)
            workspace.ensure_space(job_dir, os.path.getsize(video_path))
            source_has_audio = render.has_audio(video_path)
            if source_has_audio:
                downloader.extract_audio(video_path, audio_path)
            else:
                print("[clipper] source has no audio; skipping speech analysis")
        else:
            update_job(job_id, status="downloading", progress_message="Getting your video ready...", percent=0)
            audio_path = downloader.download_audio(
                req.url,
                audio_path,
                on_progress=lambda p: update_job(
                    job_id, percent=round(p), progress_message=f"Getting your video ready... {p:.0f}%"
                ),
            )

        # Transcription is one request with no progress of its own: a 3-hour
        # source spends several minutes here with the bar standing still, which
        # reads as a hang. Say how long it should take instead.
        audio_minutes = (content.probe_duration(audio_path) / 60
                         if source_has_audio and os.path.exists(audio_path) else 0)
        listening = "Listening to the conversation..."
        if audio_minutes >= 45:
            listening = (f"Listening to all {audio_minutes / 60:.1f} hours -- long "
                         f"videos take a few minutes here..."
                         if audio_minutes >= 90 else
                         f"Listening to all {audio_minutes:.0f} minutes -- this "
                         f"takes a minute or two...")
        update_job(job_id, status="transcribing",
                   progress_message=listening, percent=0)
        # The pricier model is asked for, not inferred. Tying it to the podcast
        # template was the wrong proxy: code-switching is a property of how
        # people SPEAK, not of the layout picked for them -- a bilingual
        # commentary video needs it and is not a podcast.
        # Diarization stays tied to the frame, because only the podcast
        # reframing reads speaker labels.
        if source_has_audio:
            transcript_json = transcriber.transcribe(
                audio_path,
                multilingual=bool(req.multilingual),
                # The auto planner may land on a podcast composition, and by then
                # the transcript is already paid for -- so ask for speaker labels
                # whenever the result might use them.
                diarize=tpl["frame"] in ("podcast", "auto"),
                keyterms=(hinglish.keyterms(req.source_title, req.intent)
                          if req.multilingual else None),
            )
        else:
            # No audio means no Deepgram call at all: nothing to transcribe and
            # nothing to bill for. The empty transcript is a real result here,
            # not a failure, and the highlight picker below reads the footage.
            transcript_json = {"results": {"utterances": []}}
        utterances = transcriber.get_utterances(transcript_json)
        if HINGLISH_CLEANUP and (req.multilingual or hinglish.has_devanagari(utterances)):
            # Opt-in: one LLM pass that also fixes misheard phrases from
            # context and re-times the corrected words; see pipeline/hinglish.py.
            before = [u.get("transcript") for u in utterances]
            utterances = hinglish.clean(utterances)
            changed = sum(1 for a, b in zip(before, utterances)
                          if (b.get("transcript") or "") != (a or ""))
            print(f"[clipper] Hinglish cleanup: {changed} of {len(utterances)} "
                  f"line(s) corrected")
        if hinglish.has_devanagari(utterances):
            # Nova-3 keeps English words English but still writes Hindi words in
            # Devanagari. Romanise them here, once, before anything reads the
            # transcript: the analyzer then reads Roman Hinglish (Devanagari
            # costs several tokens a word), titles come back Roman, and the
            # stored transcript the editor and re-exports use is already right.
            utterances = hinglish.romanize(utterances)
            print("[clipper] Hinglish: romanised Devanagari words offline")
        silent_footage = False
        if not utterances:
            # Silence is not always a mistake. A gameplay VOD with no commentary,
            # a music montage and a screen recording are all clippable, and
            # failing them with "Clipper needs talking to work with" excluded
            # exactly the kind of footage this product now supports. Fall back to
            # an audio-visual highlight picker for the templates where that is
            # the right answer; keep the error where speech is the content.
            if tpl["frame"] not in ("auto", "gameplay"):
                raise RuntimeError(
                    "No speech was found in this video, and this template needs "
                    "talking to work with. Try the Auto template for silent "
                    "gameplay or screen footage."
                )
            silent_footage = True
            transcript_json.setdefault("results", {}).setdefault("utterances", [])
            source_seconds = content.probe_duration(
                video_path or (audio_path if source_has_audio else ""))
            if source_seconds <= 0:
                raise RuntimeError("This video has no readable duration.")
        else:
            source_seconds = utterances[-1]["end"]
        numbered = transcriber.build_numbered_transcript(utterances)
        # Stored now so a later edit can reopen this job without paying Deepgram again.
        update_job(job_id, transcript=transcript_json, source_duration=source_seconds)

        # Source length no longer sets the price, but it still sets OUR cost, so
        # it is the one thing per-clip pricing has to bound. Rejected here,
        # after transcription has revealed the true duration.
        if source_seconds > max_source_minutes * 60:
            pretty = (f"{max_source_minutes // 60} hours" if max_source_minutes >= 120
                      else f"{max_source_minutes} minutes")
            update_job(
                job_id, status="failed",
                error=(f"This video is {source_seconds/60:.0f} minutes long, over "
                       f"your {pretty} limit. Clip a shorter section, or upgrade "
                       f"for longer sources -- you were not charged."),
            )
            workspace.clear_intermediates(job_dir)
            return

        update_job(job_id, status="analyzing",
                   progress_message="Finding the strongest moments...", percent=0)
        # What kind of video this is decides what a good moment looks like. Worked
        # out here because this is the first point both inputs exist -- the
        # title from the link and the transcript -- and written back onto the
        # job so the editor and any re-export agree with this run.
        genre = req.genre if req.genre != "auto" else content.guess_genre(
            utterances, source_seconds, req.source_title or "")
        coverage = content.speech_coverage(utterances, source_seconds)
        all_words = [w for u in utterances for w in (u.get("words") or [])]
        print(f"[clipper] genre: {genre} (speech covers {coverage:.0%} of the source)")
        if genre == "gaming":
            # The action in a gameplay clip happens BETWEEN the lines. Pause
            # tightening would cut exactly that, here and on every re-export.
            job_options = {**job_options, "tighten_pauses": False}
        job_options = {**job_options, "genre": genre}
        update_job(job_id, options=job_options)

        # Sound-and-motion picking reads the picture when it is on disk (an
        # upload) and the audio track alone when it is not (a link, whose video
        # is only fetched once there is something worth rendering).
        highlight_audio = audio_path if source_has_audio else None
        action_first = (genre == "gaming" and not silent_footage
                        and coverage < ACTION_FIRST_COVERAGE)

        highlight_video = req.output == "highlight"
        if highlight_video:
            # One video of the best parts, not N clips: candidates from across
            # the whole source, then a time budget decides what survives.
            if source_seconds < highlight.MIN_SOURCE_SECONDS:
                raise RuntimeError(HIGHLIGHT_TOO_SHORT)
            target = highlight.target_seconds(source_seconds, req.highlight_minutes)
            update_job(job_id, progress_message="Finding the best parts of the whole video...",
                       percent=0)
            want_moments = max(12, int(target / 20))
            if silent_footage or action_first:
                pool = content.highlight_moments(
                    video_path, source_seconds, want_moments,
                    audio_path=highlight_audio, length_pref="medium", words=all_words)
            else:
                utterances = energy.analyze(audio_path, utterances)
                pool = analyzer.highlight_candidates(
                    utterances, intent=req.intent, genre=genre,
                    # This pass IS the whole selection for a highlight video, so
                    # the paid advanced model runs it; otherwise the cheap one.
                    model=(analyzer.model_for(True)
                           if job_options.get("advanced_model") else None),
                    on_progress=lambda n, total: update_job(
                        job_id, percent=round(n / total * 100),
                        progress_message=f"Reading part {n} of {total} of the video..."))
                if genre == "gaming":
                    # The best plays are often met with silence; the game's own
                    # sound and motion find those.
                    pool += content.highlight_moments(
                        video_path, source_seconds, want_moments // 2,
                        audio_path=highlight_audio, length_pref="medium",
                        words=all_words, avoid=[(c["start"], c["end"]) for c in pool])
            resolved_clips = highlight.select_segments(pool, target, source_seconds)
            if not resolved_clips:
                raise RuntimeError(
                    "Nothing in this video scored well enough for a highlight video.")
            print(f"[clipper] highlight: {len(resolved_clips)} part(s), "
                  f"{sum(c['end'] - c['start'] for c in resolved_clips) / 60:.1f} of "
                  f"{target / 60:.1f} target minutes")
        elif silent_footage or action_first:
            # Nothing to read, or too little of it to matter, so the moments
            # come from the footage: motion peaks, sudden game audio, music
            # hits. Same idea as the spoken path, just a different sensor. Any
            # commentary inside a picked window still becomes its captions.
            resolved_clips = content.highlight_moments(
                video_path, source_seconds, req.n_clips,
                audio_path=highlight_audio, length_pref=req.length_pref,
                words=all_words)
            if not resolved_clips:
                raise RuntimeError(
                    "This video has no speech and no strong action peaks, so "
                    "there was nothing to clip."
                )
            print(f"[clipper] {'action-first' if action_first else 'silent footage'}: "
                  f"picked {len(resolved_clips)} audio-visual highlight(s)")
        else:
            # Prosody: how lines were delivered, which the transcript cannot show.
            utterances = energy.analyze(audio_path, utterances)

            update_job(job_id,
                       progress_message="Comparing the moments worth posting...",
                       percent=0)
            raw_clips = analyzer.pick_clips(
                utterances,
                req.n_clips,
                intent=req.intent,
                length_pref=req.length_pref,
                genre=genre,
                # From the frozen options, so the tier billed for is the tier run.
                model=analyzer.model_for(bool(job_options.get("advanced_model"))),
                on_progress=lambda n, total: update_job(
                    job_id,
                    percent=100 if n < 0 else round(n / total * 100),
                    progress_message=(
                        f"Comparing the {total} strongest moments..."
                        if n < 0 else f"Reviewing moment {n} of {total}..."
                    ),
                ),
            )
            resolved_clips = [analyzer.resolve_clip_times(c, utterances)
                              for c in raw_clips]
            if genre == "gaming" and len(resolved_clips) < req.n_clips:
                # A streamer's commentary rarely covers every good moment: the
                # best play of the session is often met with silence or a single
                # word. Fill what the transcript could not find from the game
                # itself, keeping clear of what has already been picked.
                extra = content.highlight_moments(
                    video_path, source_seconds,
                    req.n_clips - len(resolved_clips),
                    audio_path=highlight_audio, length_pref=req.length_pref,
                    words=all_words,
                    avoid=[(c["start"], c["end"]) for c in resolved_clips])
                if extra:
                    print(f"[clipper] gaming: added {len(extra)} action "
                          f"highlight(s) the commentary did not cover")
                resolved_clips += extra
            if not resolved_clips:
                raise RuntimeError(
                    "Nothing in this video scored well enough to clip. Videos with a "
                    "clear spoken point -- podcasts, interviews, commentary -- work best."
                )

        # Charged here, not earlier: the price is per clip, and only now is the
        # real number known. Asking for 5 and being handed 3 costs 3 -- billing
        # for clips the analysis could not find would make the per-clip promise
        # a lie. Still ahead of the render, which is where the compute goes.
        # Anonymous jobs are free while metering is unenforced.
        if job_user_id:
            # Extras come from the OPTIONS frozen onto the job at creation, not
            # from `req`, for the same reason the watermark and plan limits do:
            # what someone agreed to at the review screen is what they get
            # billed for, whatever has changed since.
            hq = bool(job_options.get("high_quality"))
            adv = bool(job_options.get("advanced_model"))
            if highlight_video:
                # One video, priced by its finished length.
                seconds = sum(c["end"] - c["start"] for c in resolved_clips)
                cost = billing.credits_for_highlight(seconds, hq, adv)
                what = f"This {seconds / 60:.0f}-minute highlight video needs"
                note = f"highlight video ({seconds / 60:.1f} min)"
            else:
                cost = billing.credits_for_job(len(resolved_clips), hq, adv)
                what = f"These {len(resolved_clips)} clips need"
                note = f"{len(resolved_clips)} clip(s)"
            # Remembered so the failure handler can give it back. Everything
            # after this point can still fail -- the source download is the
            # likeliest, since YouTube can refuse it long after the transcript
            # was paid for -- and a user billed for clips they never received
            # is the worst bug this file can have.
            charged_credits = cost
            if not billing.charge(job_user_id, cost, job_id=job_id, note=note):
                have = billing.balance(job_user_id)
                update_job(
                    job_id, status="failed",
                    error=(f"{what} {cost} credits and you have {have}. Top up "
                           f"and run it again -- you were not charged."),
                )
                workspace.clear_intermediates(job_dir)
                return

        noun = "highlight video" if highlight_video else "clips"
        update_job(job_id, status="rendering",
                   progress_message=f"Preparing your {noun}...", percent=0)
        if uploaded_source:
            workspace.ensure_space(job_dir, os.path.getsize(video_path))
            update_job(job_id, progress_message="Getting the full video ready...", percent=0)
        else:
            workspace.ensure_space(job_dir, downloader.estimate_video_bytes(req.url, max_video_height))
            update_job(job_id, progress_message="Getting the full video ready...", percent=0)
            video_path = downloader.download_video(
                req.url,
                os.path.join(job_dir, "source.mp4"),
                on_progress=lambda p: update_job(
                    job_id, percent=round(p * 0.5), progress_message=f"Getting the full video ready... {p:.0f}%"
                ),
                max_height=max_video_height,
            )

        # Start the editor proxies NOW, alongside clip rendering, instead of
        # after it. The trim/caption editor is useless without them, and building
        # them last meant the editor was dead at exactly the moment people open
        # it -- when their clips have just appeared. These are 640px veryfast
        # encodes of ~105s each, so they cost a background core, not the clips'
        # latency. Nothing waits on this thread: if it loses the race the editor
        # shows its "preparing" state for that clip, which is the truth.
        if not highlight_video:
            # Editor proxies are per clip; a highlight video is not edited
            # clip by clip, so it has nothing to scrub.
            proxy_thread = threading.Thread(
                target=_build_proxies_quietly,
                args=(job_id, video_path, job_dir,
                      [{"start": c["start"], "end": c["end"]} for c in resolved_clips],
                      source_seconds),
                daemon=True,
            )
            proxy_thread.start()

        out_w, out_h = render.RATIOS.get(req.ratio, render.RATIOS["9:16"])
        # A wide highlight video keeps the whole original picture (a source
        # that is not 16:9 sits on a blurred fill of itself, not black bars);
        # anything else is framed exactly as clips are.
        frame = "blur" if highlight_video and req.ratio == "16:9" else tpl["frame"]
        if frame != "gameplay":
            gameplay_loops = []
        # Clip-only touches: a title card per clip, a music bed under every
        # clip, a whoosh on every layout cut and keyword punch-ins all belong
        # to a short post, not to minutes of one continuous video.
        burn_title = req.burn_title and not highlight_video
        use_music = req.music_bed and not highlight_video
        use_sfx = req.auto_sfx and not highlight_video
        use_zoom = req.auto_zoom and not highlight_video

        if highlight_video and frame == "blur":
            # Watch-along and stream parts: find the streamer's webcam overlay
            # so the edit can enlarge it next to what they are reacting to.
            update_job(job_id, progress_message="Finding the streamer's camera...")
            for clip in resolved_clips:
                try:
                    info = content.analyze(video_path, clip["start"], clip["end"])
                    cam = (info["facecam"] if info["kind"] == "gameplay_facecam"
                           else content.corner_facecam(info["samples"], *info["src"]))
                    if cam:
                        clip["pip"] = highlight.pip_for(cam, *info["src"], out_w, out_h)
                except Exception as exc:
                    print(f"[clipper] highlight: facecam check skipped ({exc})")

        def part_name(i: int) -> str:
            return (f"{highlight.WORK_PREFIX}seg_{i}.mp4" if highlight_video
                    else f"clip_{i}.mp4")

        if gameplay_loops:
            margin_v = render.split_caption_margin_v(out_h)
        else:
            src_w, src_h = render.probe_dimensions(video_path)
            # The podcast and fit frames sit their video high, so the generic
            # margin -- which assumes equal bands -- would strand captions in
            # the corner of the taller band underneath.
            place = {"podcast": render.podcast_caption_margin_v,
                     "auto": render.fit_caption_margin_v,
                     "fit": render.fit_caption_margin_v}.get(
                         frame, render.caption_margin_v)
            margin_v = place(src_w, src_h, out_w, out_h)

        # -- Prepare each clip (subtitles, reframing, voiceover) sequentially,
        # because some steps make network calls or write shared state.
        # Then render the actual ffmpeg encodes in parallel.
        total = len(resolved_clips)
        prepared = []  # list of dicts with everything render_clip needs

        for i, clip in enumerate(resolved_clips, start=1):
            subtitle_path = None
            audio_override = None
            clip_words = clip["words"]

            clip_plan = None
            clip_margin_v = margin_v
            lock_margin = bool(gameplay_loops)
            if frame in ("podcast", "auto"):
                try:
                    zoom_windows = (reframe._keyword_windows(
                                        clip_words, clip.get("keywords"),
                                        clip.get("title"))
                                    if use_zoom else None)
                    if frame == "auto":
                        clip_plan = content.plan(
                            video_path, clip["start"], clip["end"], out_w, out_h,
                            speaker_words=clip_words, zoom_windows=zoom_windows,
                            variant=i, title=clip.get("title", ""),
                            genre=genre, broll=req.auto_broll,
                            source_title=req.source_title)
                    else:
                        clip_plan = reframe.plan(
                            video_path, clip["start"], clip["end"], out_w, out_h,
                            speaker_words=clip_words, zoom_windows=zoom_windows)
                except Exception as exc:
                    print(f"[clipper] clip {i}: framing failed ({exc}); "
                          "falling back to the fixed frame")
                if clip_plan:
                    print(f"[clipper] clip {i}: framing as "
                          f"{content.describe(clip_plan)}")
                    clip_margin_v = render.smart_caption_margin_v(clip_plan, out_h)
                    lock_margin = True
                    cache_path = os.path.join(
                        job_dir,
                        (f"reframe_v{reframe.PLAN_VERSION}_{i - 1}_"
                         f"{clip['start']:.1f}_{clip['end']:.1f}.json"),
                    )
                    with open(cache_path, "w") as cache_file:
                        json.dump(clip_plan, cache_file)

            segments = None
            render_duration = clip["end"] - clip["start"]
            # Never on gameplay or on a moment found by sound and motion: there
            # the gaps between words ARE the clip.
            if (req.tighten_pauses and clip_words and genre != "gaming"
                    and not clip.get("highlight")):
                segments, clip_words, removed = pacing.tighten(
                    clip_words, clip["start"], clip["end"],
                    max_pause=pacing.max_pause_for_frame(frame))
                if removed < 0.4:
                    segments, clip_words = None, clip["words"]
                else:
                    render_duration = sum(e - s for s, e in segments)
                    print(f"[clipper] clip {i}: removed {removed:.1f}s of dead air")

            caption_positions = (reframe.caption_positions(
                                    clip_plan, clip["start"], segments)
                                 if clip_plan else None)
            sfx_events = []
            whoosh = render.whoosh_path()
            if use_sfx and clip_plan and whoosh:
                sfx_events = [
                    {"path": whoosh, "time": t, "gain": 0.26}
                    for t in reframe.cut_times(clip_plan, clip["start"], segments)
                ]
            music_path = (music.path_for(genre, clip.get("title", ""), variant=i)
                          if use_music else None)

            mute_spans = None
            caption_words = clip_words
            if req.auto_censor and clip_words:
                offset = 0.0 if segments else clip["start"]
                caption_words, mute_spans = censor.apply(clip_words, offset)
                if mute_spans:
                    print(f"[clipper] clip {i}: muted {len(mute_spans)} word(s)")

            emoji_overlays = []
            if req.mode == "original":
                clip_title = clip.get("title", "") if burn_title else ""
                popups = (not req.burn_subtitles and req.keyword_captions
                          and bool(clip_words))
                # A pop-up is a whole keyword phrase (up to 3 words), not the
                # look's 2-word beat, unless the person set words per cue.
                cue_words = req.words_per_cue or (
                    subtitles.POPUP_MAX_WORDS if popups else None)
                if popups:
                    caption_words = subtitles.keyword_popups(
                        caption_words, clip.get("keywords"))
                if (req.burn_subtitles or popups) and caption_words:
                    subtitle_path = os.path.join(job_dir, f"clip_{i}.ass")
                    caption_preset_id = caption_presets.get(req.caption_style)["id"]
                    cue_emoji = (
                        caption_emoji.plan(caption_words, caption_preset_id,
                                           max_words=cue_words,
                                           popups=popups)
                        if req.auto_emoji else {}
                    )
                    # Color emoji are composited as PNGs; anything without a
                    # bundled asset stays a monochrome glyph in the ASS file.
                    inline_emoji, emoji_overlays = emoji_overlay.plan_overlays(
                        cue_emoji, caption_words, caption_preset_id,
                        (out_w, out_h), clip_margin_v,
                        0.0 if segments else clip["start"],
                        max_words=cue_words,
                        positions=caption_positions)
                    subtitles.build_ass(caption_words,
                                        0.0 if segments else clip["start"], subtitle_path,
                                        clip_margin_v,
                                        style=caption_preset_id,
                                        play_res=(out_w, out_h),
                                        title=clip_title,
                                        keywords=clip.get("keywords"),
                                        cue_emoji=inline_emoji,
                                        max_words=cue_words,
                                        positions=caption_positions,
                                        lock_margin=lock_margin)
                elif clip_title.strip():
                    subtitle_path = os.path.join(job_dir, f"clip_{i}.ass")
                    caption_preset_id = caption_presets.get(req.caption_style)["id"]
                    subtitles.build_ass([], 0.0, subtitle_path,
                                        clip_margin_v,
                                        style=caption_preset_id,
                                        play_res=(out_w, out_h),
                                        title=clip_title,
                                        lock_margin=lock_margin)
                end_time = clip["end"]
            else:
                script_text = voiceover.write_narration_script(
                    numbered, clip["start_index"], clip["end_index"], req.language
                )
                audio_override = os.path.join(job_dir, f"voiceover_{i}.mp3")
                voiceover.generate_voiceover_audio(script_text, audio_override, req.voice)
                clip["voiceover_script"] = script_text
                end_time = clip["start"] + 9999

            out_name = part_name(i)
            prepared.append({
                "index": i,
                "clip": clip,
                "clip_words": clip_words,
                "out_name": out_name,
                "render_duration": render_duration,
                "render_kwargs": dict(
                    video_path=video_path,
                    start=clip["start"],
                    end=end_time,
                    out_path=os.path.join(job_dir, out_name),
                    subtitle_path=subtitle_path,
                    audio_override_path=audio_override,
                    gameplay_path=(gameplay_loops[(i - 1) % len(gameplay_loops)]
                                   if gameplay_loops else None),
                    ratio=req.ratio,
                    frame=frame,
                    segments=segments,
                    mute_spans=mute_spans,
                    reframe_plan=clip_plan,
                    emoji_overlays=emoji_overlays,
                    transitions=req.auto_transitions,
                    sfx_events=sfx_events,
                    music_path=music_path,
                    music_gain=music.gain_for(genre),
                    watermark=options_watermark,
                    high_quality=bool(job_options.get("high_quality")),
                ),
            })

        # -- Render clips in parallel. Each ffmpeg process is independent; they
        # read the same source file but write to separate outputs. On a Mac
        # with hardware encoding this is nearly free; on a container the
        # worker count is capped to avoid OOM.
        import concurrent.futures

        max_parallel = max(1, int(os.environ.get("CLIPPER_PARALLEL_RENDERS", "3")))
        done_count = 0
        done_lock = threading.Lock()

        def _render_one(item):
            nonlocal done_count
            kw = item["render_kwargs"]
            render.render_clip(**kw)
            with done_lock:
                done_count += 1
                update_job(
                    job_id,
                    progress_message=(
                        f"Rendering part {done_count} of {total}..." if highlight_video
                        else f"Finishing clips... {done_count} of {total} done"),
                    percent=round(50 + done_count / total * (40 if highlight_video else 50)),
                )

        update_job(job_id,
                   progress_message=(f"Rendering the {total} parts of your highlight video..."
                                     if highlight_video else f"Finishing {total} clips..."),
                   percent=50)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = {pool.submit(_render_one, item): item for item in prepared}
            for future in concurrent.futures.as_completed(futures):
                future.result()  # raises if a render failed

        if highlight_video:
            # The parts are only material: the deliverable is the one video
            # they are joined into. A failure here fails the job (and refunds
            # it) -- there is nothing else to hand over.
            update_job(job_id, progress_message="Editing your highlight video...",
                       percent=92)
            info = highlight.build(
                job_dir,
                [{"file": item["out_name"],
                  "title": item["clip"].get("title") or "",
                  "score": item["clip"].get("score") or 0,
                  "source_start": item["clip"]["start"],
                  "source_end": item["clip"]["end"],
                  "words": _clip_timeline_words(item),
                  "pip": item["clip"].get("pip")} for item in prepared],
                out_w, out_h, genre=genre, title=req.source_title or "",
                memes=req.highlight_memes, tags=req.highlight_tags,
                high_quality=bool(job_options.get("high_quality")),
                music_path=(music.track_for(genre, req.source_title or "", variant=1)
                            if req.highlight_music else None),
                music_gain=music.gain_for(genre))
            job_options = {**job_options, "highlight": info}
            update_job(job_id, options=job_options)
            print(f"[clipper] job {job_id}: highlight video {info['duration'] / 60:.1f} min, "
                  f"{info['segments']} part(s), {info['memes']} meme beat(s)")
            results, deliverables = [], [info["file"]]
        else:
            # -- Collect results in clip order.
            results = []
            for item in prepared:
                clip = item["clip"]
                shown_score, shown_scores = scoring.presentation_score(
                    clip.get("score") or 0, clip.get("scores") or {})
                results.append({
                    "file": item["out_name"],
                    "title": clip["title"],
                    "hook": clip["hook"],
                    "start": clip["start"],
                    "end": clip["end"],
                    "duration": round(item["render_duration"], 1),
                    "score": shown_score,
                    "scores": shown_scores,
                    "voiceover_script": clip.get("voiceover_script"),
                    # Auto Zoom and keyword colouring both read this back on a
                    # re-export. Dropping it here made the editor's "Auto Zoom"
                    # toggle do nothing, because the only copy lived in memory.
                    "keywords": clip.get("keywords") or [],
                    "words": item["clip_words"],
                })
            deliverables = [clip["file"] for clip in results]

        # Publish BEFORE marking the job done. The status is what makes clips
        # downloadable in the UI, so flipping it first opens a window where a
        # user can click a clip that is not in storage yet -- and on object
        # storage that is a 404 on the thing they just paid for. A publish
        # failure must fail the job, for the same reason.
        #
        # The source and proxies go too: an edit re-cuts from the source, and on
        # a worker that did not render this job -- or after any redeploy -- it
        # is not on local disk. `storage.ensure_local` fetches it back.
        # Proxies are NOT in this list -- they publish themselves as each one
        # finishes, on a thread that outlives this function.
        published = storage.publish(job_id, job_dir, [*deliverables, "source.mp4"])
        if published:
            print(f"[clipper] job {job_id}: published {published} file(s) to "
                  f"{storage.driver.name}")

        # The clips are the deliverable; the source video is often several hundred
        # MB and is worthless once they exist.
        reclaimed = workspace.clear_intermediates(job_dir)

        update_job(job_id, status="done", progress_message="Done", percent=100, clips=results)
        print(f"[clipper] job {job_id} done, reclaimed {workspace.human(reclaimed)}")

    except Exception as e:
        # The user sees one sentence; the traceback goes to the server console.
        # RuntimeErrors are raised by us with messages written for humans; for
        # anything else, still show only the message, not the stack.
        traceback.print_exc()

        # Give the credits back before anything else. The charge happens once
        # the clip count is known but before rendering, so every failure past
        # that point had been leaving the user paying for clips that do not
        # exist -- and the commonest one, YouTube refusing the download, is not
        # remotely their fault.
        if charged_credits and job_user_id:
            if billing.refund(job_user_id, charged_credits, job_id=job_id):
                print(f"[clipper] refunded {charged_credits} credits for "
                      f"failed job {job_id}")

        raw = str(e) or e.__class__.__name__
        if ("No speech was found" in raw or "Nothing in this video scored" in raw
                or raw == HIGHLIGHT_TOO_SHORT):
            message = raw
        elif "longer than" in raw and "limit" in raw:
            message = "This video is longer than your plan allows. Choose a shorter video or upgrade for longer sources."
        else:
            message = "We couldn't finish this video right now. Try again shortly, or upload the video file instead."
        update_job(job_id, status="failed", error=message)
