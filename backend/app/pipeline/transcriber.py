"""Transcribes audio with Deepgram, preserving word-level timestamps.

Word-level data is what lets us later snap clip boundaries to real speech
edges and build accurate burned-in captions, instead of trusting an LLM's
guessed second values.
"""

from app import env  # noqa: F401  -- ensures .env is loaded

import os

import time

import requests

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")


def _require_key():
    if not DEEPGRAM_API_KEY:
        raise RuntimeError(
            "No Deepgram API key is configured, so speech can't be transcribed. "
            "Add DEEPGRAM_API_KEY=... to backend/.env and restart the backend."
        )


_COMMON = {
    "smart_format": "true",
    "utterances": "true",
    "punctuate": "true",
}

# The default, unchanged, and what every job still uses unless it asks for
# something else. Cheap, and correct for single-language footage -- which most
# footage is.
_DEFAULT = {"model": "nova-2", "detect_language": "true"}

# Opt-in. `detect_language` picks ONE language for the whole file, which is
# wrong for the way some footage is actually spoken: an Indian podcast switches
# between Hindi and English inside a single sentence, and whichever language
# won the detection transcribed the other one as noise. nova-3's language=multi
# is built for that.
#
# It is NOT the default, because it is a more expensive tier and transcription
# runs once per job regardless of template -- making it global would raise the
# price of every clip to fix a problem only some of them have.
#
# The nova-2 entry after it is a fallback, not a second opinion: nova-3 is not
# on every Deepgram plan, and without it a plan lacking nova-3 would fail the
# whole job -- strictly worse than the model it replaced.
_MULTILINGUAL = ({"model": "nova-3", "language": "multi"}, _DEFAULT)


# Network attempts per Deepgram request before the job is failed.
UPLOAD_ATTEMPTS = 3


def transcribe(audio_path: str, multilingual: bool = False,
               diarize: bool = False, keyterms: list = None) -> dict:
    """Transcribe with word-level timings.

    `multilingual` opts into the pricier model that handles code-switching.
    `diarize` asks who is speaking, which only the podcast reframing consumes.
    Both default off so the common path costs exactly what it did before.
    """
    _require_key()
    with open(audio_path, "rb") as f:
        audio = f.read()

    params = dict(_COMMON)
    if diarize:
        params["diarize"] = "true"
    attempts = _MULTILINGUAL if multilingual else (_DEFAULT,)

    last = None
    for i, choice in enumerate(attempts):
        request_params = {**params, **choice}
        # Keyterm boosting is nova-3 only. The 100 most useful Hinglish words
        # plus the title are enough to make nova-3 spell "bahut" as "bahut"
        # rather than inventing a Devanagari word that romanises into nonsense.
        if keyterms and choice.get("model") == "nova-3":
            request_params["keyterm"] = list(keyterms)[:100]

        def _post(payload):
            # A long source is tens of MB in one request, and one stalled
            # upload used to fail the whole job ("The write operation timed
            # out"). A network failure means Deepgram never received the
            # audio, so retrying cannot bill twice; HTTP errors are not retried
            # here -- they are answers, handled below.
            for attempt in range(UPLOAD_ATTEMPTS):
                try:
                    return requests.post(
                        "https://api.deepgram.com/v1/listen",
                        params=payload,
                        headers={
                            "Authorization": f"Token {DEEPGRAM_API_KEY}",
                            "Content-Type": "audio/mp3",
                        },
                        data=audio,
                        timeout=(30, 600),
                    )
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout) as exc:
                    if attempt == UPLOAD_ATTEMPTS - 1:
                        raise
                    wait = 5 * (attempt + 1) ** 2
                    print(f"[clipper] Deepgram upload failed "
                          f"({exc.__class__.__name__}); retrying in {wait}s")
                    time.sleep(wait)

        resp = _post(request_params)
        if resp.status_code == 400 and "keyterm" in request_params:
            # Older nova-3 deployments reject the parameter outright. Boosting
            # is an improvement, not a dependency.
            request_params.pop("keyterm", None)
            resp = _post(request_params)
        if resp.status_code in (401, 403):
            # A key problem is not a model problem -- retrying the fallback
            # would just spend another request to be told the same thing.
            raise RuntimeError(
                "Deepgram rejected the API key. Check DEEPGRAM_API_KEY in "
                "backend/.env (was the key rotated or the project deleted?)."
            )
        if resp.ok:
            if i:
                print(f"[clipper] Deepgram: fell back to {choice['model']}; "
                      "mixed-language speech will transcribe less accurately.")
            return resp.json()
        last = resp
        # Only a model/plan rejection is worth retrying. A 5xx or a timeout is
        # about Deepgram, not about which model was asked for.
        if resp.status_code not in (400, 402, 404):
            break
        print(f"[clipper] Deepgram refused {choice['model']} "
              f"({resp.status_code}), trying the next model...")

    last.raise_for_status()
    return last.json()


def get_utterances(transcript_json: dict) -> list:
    """Each utterance: {start, end, transcript, words: [{word, start, end}, ...]}"""
    return transcript_json["results"]["utterances"]


def build_numbered_transcript(utterances: list) -> str:
    """Numbered so the LLM can reference utterances by index instead of inventing timestamps."""
    lines = [f"[{i}] ({u['start']:.1f}-{u['end']:.1f}) {u['transcript']}" for i, u in enumerate(utterances)]
    return "\n".join(lines)
