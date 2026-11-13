"""Fetches source media with yt-dlp.

Two important performance decisions live here:

1. Audio is downloaded on its own, first. Transcription only needs audio, and
   the audio stream is a few MB against a video's hundreds. If the transcript
   yields nothing usable we never pay for the video download at all.

2. The video is capped at 1080p. render.py composes a 1080x1920 frame, so
   anything above that is downscaled and thrown away -- and YouTube's top
   formats are now 4K AV1, which is both enormous (>1GB) and slow to decode.
   Preferring avc1 keeps ffmpeg on its fast, hardware-friendly path.
"""

import json
import os
import re
import subprocess
import threading
import urllib.parse
import urllib.request

# "[download]  46.3% of ..." -- yt-dlp emits one of these per line with --newline
_PERCENT_RE = re.compile(r"\[download\]\s+([\d.]+)%")

# Height cap applied via -S res:N instead of -f filters so yt-dlp's JS
# challenge solver runs unimpeded. This constant is kept for reference only.
VIDEO_FORMAT = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"

AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"

# YouTube increasingly refuses anonymous downloads ("Sign in to confirm you're
# not a bot"), so a signed-in session is needed for reliability.
#
# TWO ways to supply one, and the difference matters for deployment:
#
#   CLIPPER_COOKIES_FILE    a Netscape cookies.txt. Portable, no OS keychain,
#                           works on a headless server. THIS is the deployable
#                           option and is preferred when both are set.
#   CLIPPER_COOKIES_BROWSER read the local browser profile directly. Convenient
#                           on a dev laptop, but it prompts for the macOS
#                           keychain password and cannot work on a server.
#
# Default to a cookies.txt sitting next to the app if present, so the common
# case needs no configuration at all.
_DEFAULT_COOKIE_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "cookies.txt"))

_configured = os.environ.get("CLIPPER_COOKIES_FILE", "").strip()
if _configured:
    # Resolve relative to the backend directory, not the process working
    # directory -- uvicorn may be started from anywhere.
    COOKIES_FILE = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", _configured))
elif os.path.exists(_DEFAULT_COOKIE_FILE):
    COOKIES_FILE = _DEFAULT_COOKIE_FILE
else:
    COOKIES_FILE = ""

if COOKIES_FILE and not os.path.exists(COOKIES_FILE):
    print(f"[clipper] WARNING: cookies file not found at {COOKIES_FILE} -- "
          f"YouTube will block many downloads. Regenerate it with: make cookies")
    COOKIES_FILE = ""
COOKIES_BROWSER = os.environ.get("CLIPPER_COOKIES_BROWSER", "").strip()


# A residential proxy, if you have one. This is the only thing that reliably
# fixes "Sign in to confirm you're not a bot" at scale: the block is on the IP,
# not the request. Datacenter ranges -- Railway, Render, AWS, GCP -- are
# pre-flagged, and no combination of headers, clients or cookies changes which
# network the packets came from. Cookies help, but YouTube invalidates them
# faster when it sees them used from a datacenter, so they decay in exactly the
# environment that needs them most.
PROXY = os.environ.get("CLIPPER_PROXY", "").strip()

# YouTube applies the bot challenge per client. When the default web client is
# challenged, another often is not -- so a failure is worth retrying as a
# different client before giving up. Ordered by how much detail they return:
# web carries the richest metadata, the mobile clients are the ones that tend to
# still work from a blocked IP.
#
# This is mitigation, not a fix. Which clients work changes every few weeks, so
# treat it as buying time rather than solving the problem.
PLAYER_CLIENTS = ("default", "android", "ios", "tv")


# ---------------------------------------------------------------------------
# Metadata through YouTube's official API
# ---------------------------------------------------------------------------
# METADATA and MEDIA BYTES are two different problems with two different
# answers, and treating them as one is why a blocked IP currently breaks the
# preview as well as the download:
#
#   bytes    -- only a better IP helps. See the note on PROXY above.
#   metadata -- YouTube publishes this through the Data API, which authenticates
#               by API KEY and never judges the caller's IP. It answers in about
#               200ms against yt-dlp's ~8s on a good address (and minutes on a
#               blocked one, since that walks every client before giving up).
#               Free to 10,000 units/day; a videos.list call costs 1.
#
# So the preview -- the only thing between pasting a link and seeing the video,
# and the whole reason links beat uploads -- becomes instant and reliable even
# where the download would still be refused. With no key set this is skipped
# and yt-dlp handles metadata exactly as before.
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()

_YT_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/|v/))"
    r"([A-Za-z0-9_-]{11})"
)


def _youtube_id(url: str) -> str:
    """The 11-character video id, or "" when this is not one YouTube video."""
    match = _YT_ID_RE.search(url)
    return match.group(1) if match else ""


def _iso8601_seconds(text: str):
    """PT1H2M3S -> 3723.0. The Data API reports length only in this form."""
    match = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
                         text or "")
    if not match:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _youtube_api_metadata(url: str):
    """Title, creator, duration and thumbnail straight from YouTube.

    Returns None whenever it cannot answer -- no key, not a YouTube link, quota
    exhausted, network trouble -- so the caller falls through to yt-dlp. This is
    an accelerator and must never become a new way for a preview to fail.
    """
    video_id = _youtube_id(url)
    if not (YOUTUBE_API_KEY and video_id):
        return None

    query = urllib.parse.urlencode({
        "id": video_id,
        "key": YOUTUBE_API_KEY,
        "part": "snippet,contentDetails",
    })
    try:
        with urllib.request.urlopen(
                f"https://www.googleapis.com/youtube/v3/videos?{query}",
                timeout=10) as response:
            payload = json.load(response)
    except Exception as exc:              # quota, network, bad key
        print(f"[clipper] YouTube Data API unavailable, falling back to "
              f"yt-dlp: {exc}")
        return None

    items = payload.get("items") or []
    if not items:
        # An authoritative "no such video" -- private, deleted, or a wrong id.
        # yt-dlp would spend seconds reaching the same conclusion.
        raise RuntimeError(
            "YouTube has no video at that link. It may be private or deleted, "
            "or the URL may be wrong."
        )

    item = items[0]
    snippet = item.get("snippet") or {}
    if (snippet.get("liveBroadcastContent") or "none") != "none":
        raise RuntimeError(
            "This stream is still live. Clip it once the stream has ended and "
            "the replay is up."
        )

    thumbs = (snippet.get("thumbnails") or {}).values()
    best = max(thumbs, key=lambda t: t.get("width", 0), default={})
    return {
        "title": snippet.get("title") or "Untitled video",
        "creator": snippet.get("channelTitle"),
        "duration": _iso8601_seconds(
            (item.get("contentDetails") or {}).get("duration")),
        "thumbnail": best.get("url"),
        "platform": "YouTube",
    }


def _cookie_args() -> list:
    if COOKIES_FILE:
        return ["--cookies", COOKIES_FILE]
    if COOKIES_BROWSER:
        return ["--cookies-from-browser", COOKIES_BROWSER]
    return []


def _network_args() -> list:
    return ["--proxy", PROXY] if PROXY else []


def _download_args() -> list:
    """Args every actual DOWNLOAD needs (not metadata/preview).

    The proxy is the important one: without it the download leaves from this
    server's own (datacenter) IP, which YouTube blocks as a bot -- the proxy is
    only useful if the bytes actually travel through it. The EJS solver
    (fetched once, then cached) lets the bundled Deno runtime solve YouTube's
    signature and "n" anti-throttle challenges so formats are not throttled or
    missing. --socket-timeout keeps a dead connection from hanging (the worker's
    stall watchdog is the outer backstop).

    Applied to downloads only, never to preview: preview must stay fast and must
    not depend on a GitHub fetch, or a slow fetch stalls the whole API.
    """
    return [*_network_args(), "--remote-components", "ejs:github",
            "--socket-timeout", "30"]


def _client_args(client: str) -> list:
    """Extractor args for one player client, or nothing for yt-dlp's default."""
    if client == "default":
        return []
    return ["--extractor-args", f"youtube:player_client={client}"]


def _is_bot_block(log_text: str) -> bool:
    """YouTube refusing the IP rather than the video being unavailable.

    Worth separating because the two want opposite responses: a blocked IP is
    retryable with a different client and is a configuration problem to report,
    while a private or deleted video will fail identically no matter what.
    """
    markers = ("Sign in to confirm", "confirm you're not a bot",
               "confirm you’re not a bot", "not a bot")
    return any(m in log_text for m in markers)



def cookie_source() -> str:
    """Human description of where the YouTube session comes from."""
    if COOKIES_FILE:
        return f"cookies file ({os.path.basename(COOKIES_FILE)})"
    if COOKIES_BROWSER:
        return f"{COOKIES_BROWSER} browser profile (not deployable)"
    return "none -- anonymous, YouTube will block many videos"


# yt-dlp failure signatures -> what the user should actually be told. The raw
# log is for the server console; a person who pasted a link deserves a sentence.
def _cookie_hint() -> str:
    """What to tell the PERSON who pasted the link.

    Deliberately says nothing about cookies, scripts or configuration. This
    text reaches a creator in a browser, and it used to tell them to "run
    ./refresh-cookies.sh on this machine" -- a machine they have never heard of
    and cannot log into. Advice someone cannot act on reads as being blamed for
    an outage that is not theirs.

    The operator's version of this is _cookie_operator_hint, which goes to the
    server log where the person who CAN fix it will see it.
    """
    return (" Uploading the video file works and skips YouTube entirely.")


def _cookie_operator_hint() -> str:
    """The same failure, described for whoever runs this deployment."""
    if COOKIES_FILE:
        return (f"The YouTube session in {os.path.basename(COOKIES_FILE)} has gone "
                f"stale -- YouTube rotates these regularly. Regenerate it and "
                f"redeploy.")
    if COOKIES_BROWSER:
        return (f"CLIPPER_COOKIES_BROWSER={COOKIES_BROWSER} reads a local browser "
                f"profile and cannot work on a server. Use CLIPPER_COOKIES_FILE "
                f"with an exported cookies.txt instead.")
    return ("No YouTube session is configured, so YouTube is refusing most "
            "videos as bot traffic. Export cookies.txt from a signed-in browser "
            "and point CLIPPER_COOKIES_FILE at it.")

# Message templates; the session hint is appended at raise time so it always
# reflects the CURRENT configuration rather than whatever it was at import.
_FAILURES = [
    ("Sign in to confirm your age",
     "This video is age-restricted, and YouTube only serves it to signed-in viewers.@HINT"),
    ("Sign in to confirm you",   # "...you're not a bot" (curly apostrophe varies)
     "YouTube would not serve this video without a signed-in session.@HINT"),
    ("Private video",
     "This video is private, so it can't be downloaded."),
    ("members-only",
     "This video is for channel members only, so it can't be downloaded."),
    ("Video unavailable",
     "YouTube says this video is unavailable -- it may have been removed or the "
     "link may be wrong."),
    ("not available in your country",
     "This video is blocked in your region."),
    ("This live event",
     "This is a live stream that hasn't finished -- clip it after it ends."),
    ("is not a valid URL",
     "That doesn't look like a valid video link -- check the URL and try again."),
    # Kick sits behind Cloudflare's bot protection, which refuses datacenter
    # traffic outright -- every kick.com request from a hosted backend is turned
    # away before it reaches a video, whatever the User-Agent says. yt-dlp
    # recognises these links perfectly well; it simply never gets an answer. Say
    # so plainly, because "try again shortly" would be a lie here.
    ("[kick:",
     "Kick blocks downloads from servers, so this link can't be fetched here. "
     "Save the video from Kick and upload the file instead."),
]


def _friendly_error(log_text: str) -> str:
    for signature, message in _FAILURES:
        if signature in log_text:
            return message.replace("@HINT", _cookie_hint())
    return None


def _public_failure(log_text: str) -> str:
    """A useful next step for creators; detailed provider output stays in logs.

    The point of _FAILURES is that "this video is private" and "this video is
    age-restricted" need DIFFERENT next steps. This used to match one and then
    return a single generic line regardless, so all of that resolved to "try
    again shortly" -- advice that is simply wrong for a private video, and that
    sends people into retry loops over something no retry can fix. The entries
    are hand-written strings, not provider output, so passing them through
    leaks nothing; the raw log still stays server-side.
    """
    return _friendly_error(log_text) or (
        "This link is busy or unavailable right now. Try again shortly, or "
        "upload the video file instead."
    )


def _is_auth_failure(log_text: str) -> bool:
    return "Sign in to confirm" in log_text


def refresh_cookies_from_browser() -> bool:
    """Re-exports the YouTube session from the local browser.

    YouTube rotates session tokens, so an exported cookies.txt goes stale --
    sometimes within minutes. When a browser is available (a dev machine) we can
    silently re-export and retry instead of failing the user's job.
    """
    if not COOKIES_BROWSER or not COOKIES_FILE:
        return False
    try:
        out = subprocess.run(
            ["yt-dlp", "--cookies-from-browser", COOKIES_BROWSER,
             "--cookies", COOKIES_FILE, "--skip-download", "--no-playlist",
             "--quiet", "--no-warnings",
             "https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode == 0 and os.path.exists(COOKIES_FILE):
            os.chmod(COOKIES_FILE, 0o600)
            print("[clipper] refreshed the YouTube session from "
                  f"{COOKIES_BROWSER}")
            return True
    except (subprocess.SubprocessError, OSError) as e:
        print(f"[clipper] could not refresh cookies: {e}")
    return False


# Nothing here may block forever. A keychain prompt on a headless box, or a
# stalled CDN, would otherwise leave a job sitting at 0% with no explanation.
STALL_TIMEOUT = 180


def _run_with_progress(cmd: list, on_progress=None, _retrying: bool = False) -> None:
    """Runs yt-dlp, forwarding download percentage to on_progress as it streams.

    Aborts if yt-dlp produces no output at all for STALL_TIMEOUT seconds.
    """
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    tail = []
    stalled = []

    def watchdog():
        # Reading a line at a time gives no timeout hook, so a separate timer
        # kills the process if nothing has been printed in a long while.
        if proc.poll() is None:
            stalled.append(True)
            proc.kill()

    timer = threading.Timer(STALL_TIMEOUT, watchdog)
    timer.start()
    try:
        for line in proc.stdout:
            timer.cancel()
            timer = threading.Timer(STALL_TIMEOUT, watchdog)
            timer.start()
            tail.append(line)
            del tail[:-40]                  # keep only the last lines for error context
            if on_progress:
                match = _PERCENT_RE.search(line)
                if match:
                    on_progress(float(match.group(1)))
    finally:
        timer.cancel()

    if stalled:
        raise RuntimeError(
            f"The download stalled with no progress for {STALL_TIMEOUT // 60} minutes "
            f"and was stopped. If this machine was waiting on a keychain or "
            f"password prompt, switch to a cookies file "
            f"(CLIPPER_COOKIES_FILE) instead of reading the browser profile."
        )

    if proc.wait() != 0:
        log = "".join(tail)

        # A stale session is the single most common failure here, and it is
        # recoverable without bothering anyone -- refresh and try once more.
        if _is_auth_failure(log) and not _retrying and refresh_cookies_from_browser():
            return _run_with_progress(cmd, on_progress, _retrying=True)

        friendly = _friendly_error(log)
        if friendly:
            # Full log to the server console for debugging; only the sentence
            # travels to the user.
            print("[clipper] yt-dlp failure:\n" + log)
        else:
            print("[clipper] downloader failure:\n" + log)
        # The fix for an auth refusal belongs to whoever runs this deployment,
        # so it is printed here rather than shown to the person who pasted the
        # link and can do nothing about it.
        if _is_auth_failure(log):
            print(f"[clipper] ACTION NEEDED: {_cookie_operator_hint()}")
        raise RuntimeError(_public_failure(log))


def estimate_video_bytes(url: str, max_height: int = 1080) -> int:
    """Approximate download size for the video formats we actually select.

    Used to fail fast with a readable message when the disk is too full, rather
    than grinding through a multi-minute download that dies on ENOSPC.
    Returns 0 if the size cannot be determined -- absence of a number is not a
    reason to block the run.
    """
    try:
        out = subprocess.run(
            ["yt-dlp", "-S", f"res:{max_height}", "--no-playlist", "-J", "--no-warnings",
             *_cookie_args(), url],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            return 0
        info = json.loads(out.stdout)
        formats = info.get("requested_formats") or [info]
        return sum(
            (f.get("filesize") or f.get("filesize_approx") or 0) for f in formats
        )
    except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError):
        return 0


def inspect_url(url: str) -> dict:
    """Read public video metadata without downloading its media streams."""
    # Ask YouTube directly first. It is roughly 40x faster than yt-dlp here and,
    # more importantly, works from an address yt-dlp is refused from -- so the
    # preview keeps working on a server whose downloads do not.
    from_api = _youtube_api_metadata(url)
    if from_api:
        return from_api

    log = ""
    proc = None
    # Try each client in turn: a bot challenge is per-client, so the one that
    # fails is often not the one that would have worked.
    for client in PLAYER_CLIENTS:
        try:
            proc = subprocess.run(
                ["yt-dlp", "--dump-single-json", "--skip-download", "--no-playlist",
                 "--no-warnings", *_cookie_args(), *_network_args(),
                 *_client_args(client), url],
                capture_output=True, text=True, timeout=45,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError(
                "Could not check that video link. Try again or upload the file."
            ) from exc

        if proc.returncode == 0:
            if client != "default":
                print(f"[clipper] metadata needed the {client} client "
                      f"(the default was blocked)")
            break

        log = proc.stderr + proc.stdout
        if not _is_bot_block(log):
            break        # private, deleted or unsupported -- another client won't help

    if proc is None or proc.returncode != 0:
        print("[clipper] source preview failure:\n" + log)
        if _is_bot_block(log):
            # Say what is actually wrong. "Try a public video link" sent users
            # hunting for a problem with their video when the video was fine and
            # the server's IP was the thing being refused.
            print("[clipper] YouTube refused this server's IP. Set CLIPPER_PROXY "
                  "to a residential proxy, or CLIPPER_COOKIES_FILE to a fresh "
                  "cookies.txt. See DOWNLOADS.md.")
        raise RuntimeError(_public_failure(log))
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("The video site did not return usable preview data.") from exc

    # A stream that is still running reports no duration and has no end. Two
    # things go wrong if it gets through: the download never terminates, and
    # _gate_source_duration sees a null length and blames the user's PLAN --
    # "we couldn't confirm this video's length on the Free plan" -- for what is
    # really "come back when it's over". Kick channel URLs are the common way in,
    # since kick.com/<name> IS the live URL, but this is per-site behaviour that
    # YouTube and Twitch share.
    live = info.get("live_status")
    if info.get("is_live") or live == "is_live":
        raise RuntimeError(
            "This stream is still live. Clip it once the stream has ended and "
            "the replay is up."
        )
    if live == "post_live":
        raise RuntimeError(
            "This stream just ended and the replay is still being processed. "
            "Try again in a few minutes."
        )

    thumbs = info.get("thumbnails") or []
    thumb = info.get("thumbnail") or next(
        (item.get("url") for item in reversed(thumbs) if item.get("url")), None)
    return {
        "title": info.get("title") or "Untitled video",
        "creator": info.get("uploader") or info.get("channel") or info.get("extractor_key"),
        "duration": info.get("duration"),
        "thumbnail": thumb,
        "platform": info.get("extractor_key"),
    }


def download_audio(url: str, out_path: str, on_progress=None) -> str:
    """Grabs the audio stream only and transcodes it to 16kHz mono mp3 for Deepgram."""
    raw_path = out_path + ".src"
    try:
        _run_with_progress(
            [
                "yt-dlp",
                "-f", AUDIO_FORMAT,
                "--newline",
                "--no-playlist",
                "--force-overwrites",
                *_cookie_args(),
                *_download_args(),
                "-o", raw_path,
                url,
            ],
            on_progress,
        )
    except RuntimeError:
        # Default format selection gets 403'd when YouTube flags the IP.
        # The android client still serves format 18 (360p combined) reliably;
        # audio quality does not matter here since we transcode to 16kHz mono.
        print("[clipper] audio download failed, retrying with android client")
        _run_with_progress(
            [
                "yt-dlp",
                "--extractor-args", "youtube:player_client=android",
                "-x",
                "--newline",
                "--no-playlist",
                "--force-overwrites",
                *_cookie_args(),
                *_download_args(),
                "-o", raw_path,
                url,
            ],
            on_progress,
        )
  
    

    subprocess.run(
        ["ffmpeg", "-y", "-i", raw_path, "-vn", "-ac", "1", "-ar", "16000",
         "-acodec", "libmp3lame", out_path],
        check=True,
        capture_output=True,
    )
    return out_path


def _video_format(max_height: int = 1080) -> str:
    h = max_height
    return (
        f"bestvideo[height<={h}]+bestaudio/"
        f"best[height<={h}]/best"
    )


# Clients tried in order when the default set fails. android_vr and tv_simply
# serve HD without a PO token; android is the last resort because it only
# serves format 18 (360p combined) -- a working clip beats a failed job, but a
# 360p source upscaled ~3x into a split-screen tile is what made clips soft, so
# it must never be the first fallback.
VIDEO_FALLBACK_CLIENTS = ("android_vr,tv_simply", "android")


def _source_height(path: str) -> int:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return int(out.splitlines()[0])
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return 0


def download_video(url: str, out_path: str, on_progress=None,
                   max_height: int = 1080) -> str:
    # The plan's height cap was computed but never passed, so yt-dlp chose
    # freely; every attempt now asks for the best stream up to the cap.
    base = [
        "yt-dlp",
        "-f", _video_format(max_height),
        "--merge-output-format", "mp4",
        "--newline",
        "--no-playlist",
        "--force-overwrites",
        *_cookie_args(),
        *_download_args(),
        "-o", out_path,
    ]
    attempts = [[]] + [["--extractor-args", f"youtube:player_client={c}"]
                       for c in VIDEO_FALLBACK_CLIENTS]
    for i, extra in enumerate(attempts):
        try:
            _run_with_progress([*base, *extra, url], on_progress)
            break
        except RuntimeError:
            # Usually HTTP 403 from a flagged IP on the default clients.
            if i == len(attempts) - 1:
                raise
            print(f"[clipper] video download failed, retrying with "
                  f"{VIDEO_FALLBACK_CLIENTS[i]} client")
    height = _source_height(out_path)
    if height and height < min(720, max_height):
        print(f"[clipper] WARNING source downloaded at {height}p (cap "
              f"{max_height}p); clips will be soft -- upload the file instead")
    return out_path


def extract_audio(video_path: str, out_path: str) -> str:
    """Extracts a mono 16 kHz MP3 track.

    A video with no audio stream is not an error any more: gameplay captures and
    screen recordings routinely have none, and `ffmpeg -vn` on them fails with
    "Output file does not contain any stream". Write the matching duration of
    silence instead, so every downstream step still has a file to open.
    """
    try:
        has_audio = bool(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip())
    except (subprocess.SubprocessError, OSError):
        has_audio = True

    if has_audio:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
             "-acodec", "libmp3lame", out_path],
            check=True,
            capture_output=True,
        )
        return out_path

    try:
        duration = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip().splitlines()[0])
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        duration = 60.0
    print("[clipper] source has no audio stream; writing silence for analysis")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
         "-t", str(max(1.0, duration)), "-acodec", "libmp3lame", out_path],
        check=True, capture_output=True,
    )
    return out_path
