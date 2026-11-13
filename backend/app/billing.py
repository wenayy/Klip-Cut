"""Plans, credit pricing, and metering.

Dodo Payments checkout and fulfillment live in `dodo_catalog.py` and
`dodo_payments.py`. This module remains the provider-independent source of
truth for plans, prices, entitlements, balances, and the credit ledger.

Credits are priced PER CLIP DELIVERED, not per minute of source video. This is
a deliberate product position: you pay for output you can post, not for our
processing time. Every competitor meters upload minutes, so a creator who feeds
in a three-hour stream and wants two clips is billed for the whole three hours.
Here that costs 20 credits.

The honest tradeoff: our own cost (transcription, the analysis passes) scales
with SOURCE LENGTH, while revenue now scales with clip count. A long source
that yields few clips is our worst case, and a short source that yields many is
our best. That is a margin question, not a correctness one -- but it is real,
and MAX_SOURCE_MINUTES exists so it stays bounded.

Charging happens after clip SELECTION and before rendering: by then the real
number of clips is known, so nobody is billed for clips the analysis could not
find, and nobody gets the expensive render for free.
"""

from app import env  # noqa: F401  -- ensures .env is loaded before ADMIN_EMAILS

import os
from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import CreditLedger, User, _iso_utc

# 10 credits per finished clip. The unit users are billed in is the unit they
# actually receive, which is the whole point -- see the module docstring.
CREDITS_PER_CLIP = 10

# What an admin's balance reads as. Admin accounts are for testing the product
# without the product's economics in the way -- the owner needs to render a
# hundred clips from their own account and still see the Pro output (no
# watermark, pause tightening, every template) while doing it. Real customers
# are metered exactly as before; nothing about this touches them.
UNLIMITED_CREDITS = 1_000_000_000

# The floor for having "any credit at all", used to reject a job before it
# starts. One clip is the smallest thing anyone can ask for.
MIN_CHARGE = CREDITS_PER_CLIP

FREE_SIGNUP_CREDITS = 20           # 2 finished clips to try it with

# Per-clip pricing decouples revenue from source length, so this is what stops
# a 6-hour upload costing more to transcribe than the clips are worth. Not a
# quality limit -- purely the point where the economics invert.
MAX_SOURCE_MINUTES = 120

PLANS = [
    {
        "id": "free",
        "name": "Free",
        "blurb": "Try it on a few videos",
        "monthly_usd": 0,
        "yearly_usd": 0,
        "credits": FREE_SIGNUP_CREDITS,
        "features": [
            f"{FREE_SIGNUP_CREDITS // CREDITS_PER_CLIP} free clips on signup",
            "480p output quality",
            "Projects saved for 3 hours",
            "Full caption editing in the editor",
            "One edited export",
            "All templates and ratios",
            "Word-accurate captions",
            "Clip trimming",
        ],
    },
    {
        "id": "creator",
        "name": "Creator",
        "blurb": "For posting consistently",
        "credits": 1000,           # default tier -- the headline price
        "monthly_usd": 19,
        "yearly_usd": 15,          # per month, billed yearly
        "popular": True,
        # Same plan, more credits per month. Bigger tiers cost less per credit,
        # since the fixed cost of a plan is amortised over more usage.
        "tiers": [
            {"credits": 500, "monthly_usd": 12, "yearly_usd": 10},
            {"credits": 1000, "monthly_usd": 19, "yearly_usd": 15},
            {"credits": 1500, "monthly_usd": 27, "yearly_usd": 22},
            {"credits": 2000, "monthly_usd": 34, "yearly_usd": 27},
        ],
        "features": [
            "100 ready-to-post clips each month",
            "Full HD 1080p output",
            "Projects saved for 3 days",
            "Unlimited edited exports, no watermark",
            "Multilingual captions",
            "Gameplay split-screen",
            "Pause tightening",
            "Trim and extend any clip",
            "Unlimited edited exports",
        ],
    },
    {
        "id": "pro",
        "name": "Pro",
        "blurb": "For teams and agencies",
        "credits": 1000,
        "monthly_usd": 49,
        "yearly_usd": 39,
        "features": [
            "Everything in Creator",
            "Longer source videos",
            "Larger video uploads",
            "Priority rendering",
        ],
    },
]

# What a plan actually UNLOCKS, as opposed to the `features` lists above, which
# are marketing copy and are safe to reword. These strings are load-bearing:
# they gate real behaviour, so treat them as an API.
#
#   multilingual -- transcribe with Deepgram nova-3 (language=multi) instead of
#                   nova-2. It handles speech that switches language mid
#                   sentence, and it costs materially more per minute, which is
#                   the whole reason it is a paid capability rather than the
#                   default for everyone.
#   gameplay       -- the split-screen template. Ours to host and ours to keep
#                     legally clean, and it doubles render time.
#   tighten_pauses -- cutting dead air; an extra analysis pass and a re-cut.
#   branding       -- a logo or handle burned into the clip.
#   no_watermark   -- free clips carry ours. See WATERMARK_TEXT.
#   share_pages    -- the public score/hook page per clip.
#   priority       -- Pro jobs go ahead of the standard queue.
_PAID = frozenset({"multilingual", "gameplay", "tighten_pauses",
                   "branding", "no_watermark", "share_pages"})

# On every plan, Free included.
#
#   caption_editing -- changing caption text, style, colour, size or position.
#
# This one is here rather than in _PAID because editing captions RENDERS
# NOTHING: the editor previews in the browser over the proxy, and the save is a
# JSON recipe. Charging for it meant a Free account could never discover what
# the editor does before being asked to pay for it -- the strongest argument the
# product has, hidden behind the paywall it was supposed to sell.
#
# Metering lives at the export instead, where the compute actually is. A Free
# account still gets exactly one edited export, still watermarked; what changed
# is that the export now contains the work they did.
_EVERYONE = frozenset({"caption_editing"})

ENTITLEMENTS = {
    "free": _EVERYONE,
    "creator": _PAID | _EVERYONE,
    "pro": _PAID | _EVERYONE | {"priority"},
}

# Hard numbers, separate from ENTITLEMENTS because a limit is a ceiling rather
# than an on/off switch. Free is deliberately usable rather than crippled: two
# real clips off a 15-minute video is a genuine trial, and the ceiling is the
# reason to upgrade -- not a broken experience.
LIMITS = {
    # 100 MB pairs with the 15-minute ceiling and the 480p cap: a quarter-hour
    # at 480p lands well inside it, so the two limits bite at roughly the same
    # point instead of one silently overriding the other.
    "free":    {"max_clips": 2,  "max_source_minutes": 15,
                "max_upload_mb": 100, "max_video_height": 480},
    "creator": {"max_clips": 10, "max_source_minutes": MAX_SOURCE_MINUTES,
                "max_upload_mb": 2048, "max_video_height": 1080},
    "pro":     {"max_clips": 10, "max_source_minutes": 720,
                "max_upload_mb": 4096, "max_video_height": 1080},
}

# Finished projects remain editable for a short, explicit window. Keeping this
# next to plan limits makes retention a product rule rather than an accidental
# consequence of whichever disk happens to fill first.
RETENTION_HOURS = {
    "free": 3,
    "creator": 72,
    "pro": 72,
}


def retention_hours_for(plan_id: str, email: str = None) -> int:
    """How long completed media remains available for this account."""
    if is_admin(email):
        return RETENTION_HOURS["pro"]
    return RETENTION_HOURS.get(plan_id or "free", RETENTION_HOURS["free"])

# Burned into free clips. Configurable because the product name is not final,
# and a watermark is the one string that ends up on someone else's timeline.
WATERMARK_TEXT = os.environ.get("CLIPPER_WATERMARK", "Made with KlipCut")


# Accounts that get every capability regardless of plan, for testing the paid
# experience without buying it. Config rather than code so adding a tester is an
# .env edit and a restart, and so the list cannot be read off a public repo.
#
#   ADMIN_EMAILS=me@example.com,qa@example.com
#
# This grants FEATURES, not credits. An admin still spends credits at the
# normal rate, because "does the credit flow feel right" is one of the things
# worth testing, and an account that never decrements cannot answer it.
_DOTLESS_DOMAINS = ("gmail.com", "googlemail.com")


def _canonical_email(email: str) -> str:
    """Lowercased, with Gmail's dots removed from the local part.

    Gmail delivers first.last@ and firstlast@ to the same inbox, so someone
    listed one way and signing up the other way is the same person and would
    otherwise silently fail to match. Dots ARE significant on other domains, so
    this deliberately only applies to Gmail.
    """
    email = (email or "").strip().lower()
    local, _, domain = email.partition("@")
    if domain in _DOTLESS_DOMAINS:
        local = local.replace(".", "")
    return f"{local}@{domain}" if domain else local


ADMIN_EMAILS = frozenset(
    _canonical_email(e)
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
)


def is_admin(email: str) -> bool:
    return bool(email) and _canonical_email(email) in ADMIN_EMAILS


def entitlements_for(plan_id: str, email: str = None) -> frozenset:
    """Everything this account can reach.

    One place, so the gate in the API and the list sent to the client can never
    disagree -- a UI that offers what the server rejects is worse than one that
    offers nothing.
    """
    if is_admin(email):
        return frozenset().union(*ENTITLEMENTS.values())
    return ENTITLEMENTS.get(plan_id or "free", ENTITLEMENTS["free"])


def limits_for(plan_id: str, email: str = None) -> dict:
    """The ceilings this account works under. Signed out == free."""
    if is_admin(email):
        return dict(LIMITS["pro"])
    return dict(LIMITS.get(plan_id or "free", LIMITS["free"]))


def allows(plan_id: str, feature: str, email: str = None) -> bool:
    """Whether an account includes a feature.

    Unknown plans -- and signed-out users, who have no plan at all -- get the
    free set. Failing closed matters here: an unrecognised plan string must not
    become a way to reach paid capability.
    """
    return feature in entitlements_for(plan_id, email)


# Extra credits outside a plan.
TOPUPS = [
    {"credits": 500, "usd": 12},
    {"credits": 1500, "usd": 30},
    {"credits": 5000, "usd": 90},
]


def credits_for_clips(n_clips: int) -> int:
    """What n finished clips cost. Source length does not enter into it."""
    return max(0, int(n_clips or 0)) * CREDITS_PER_CLIP


# Optional extras, priced where the cost actually falls.
#
# Quality rides on the RENDER, which runs once per clip -- a slower preset and
# a lower CRF cost real encoder time for each one, so it is priced per clip.
#
# The advanced model rides on the ANALYSIS, which runs ONCE for the whole job
# however many clips come out of it. Pricing that per clip would bill several
# times over for a single API call, so it is a flat per-job charge.
#
# Neither is gated on a plan. They are paid for in credits, which means a Free
# account can try either on its signup grant and find out whether the better
# output is worth it -- the same reasoning that moved caption editing out of
# _PAID. What Free gets is the cheaper DEFAULT, not a locked door.
HIGH_QUALITY_PER_CLIP = 5
ADVANCED_MODEL_PER_JOB = 10


def credits_for_job(n_clips: int, high_quality: bool = False,
                    advanced_model: bool = False) -> int:
    """Total credits for a job, including any extras it opted into."""
    n = max(0, int(n_clips or 0))
    total = credits_for_clips(n)
    if high_quality:
        total += HIGH_QUALITY_PER_CLIP * n
    if advanced_model:
        total += ADVANCED_MODEL_PER_JOB
    return total


def cost_breakdown(n_clips: int, high_quality: bool = False,
                   advanced_model: bool = False) -> dict:
    """The same total, itemised, for the review step to render.

    Built here rather than in the client so the number someone agrees to and
    the number they are charged come from one place. A review screen that
    computes its own total will eventually disagree with the ledger, and the
    user will be right and the invoice wrong.
    """
    n = max(0, int(n_clips or 0))
    lines = [{
        "label": f"{n} clip{'' if n == 1 else 's'}",
        "detail": f"{CREDITS_PER_CLIP} credits each",
        "credits": credits_for_clips(n),
    }]
    if high_quality:
        lines.append({
            "label": "High quality",
            "detail": f"{HIGH_QUALITY_PER_CLIP} credits per clip",
            "credits": HIGH_QUALITY_PER_CLIP * n,
        })
    if advanced_model:
        lines.append({
            "label": "Advanced model",
            "detail": "charged once per job",
            "credits": ADVANCED_MODEL_PER_JOB,
        })
    return {"lines": lines,
            "total": credits_for_job(n, high_quality, advanced_model)}


def get_plan(plan_id: str) -> dict:
    for plan in PLANS:
        if plan["id"] == plan_id:
            return plan
    return None


def get_tier(plan_id: str, credits: int) -> dict:
    """The priced tier for a plan at a given monthly credit amount.

    Falls back to the plan's headline tier when credits are not specified, and
    returns None for a combination that is not offered -- so a client cannot
    invent its own price by posting an arbitrary credit count.
    """
    plan = get_plan(plan_id)
    if not plan:
        return None

    tiers = plan.get("tiers")
    if not tiers:
        return {"credits": plan["credits"],
                "monthly_usd": plan["monthly_usd"],
                "yearly_usd": plan["yearly_usd"]}

    if credits is None:
        credits = plan["credits"]
    return next((t for t in tiers if t["credits"] == credits), None)


def balance(user_id: str) -> int:
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if not user:
            return 0
        if is_admin(user.email):
            return UNLIMITED_CREDITS
        return user.credits


def charge(user_id: str, amount: int, job_id: str = None, note: str = "") -> bool:
    """Debits credits. Returns False (and changes nothing) if the balance is short.

    Anonymous use is free while metering is unenforced, so a null user_id is a
    no-op rather than an error.
    """
    if not user_id:
        return True

    with SessionLocal() as session:
        user = session.get(User, user_id)
        if not user:
            return False
        # Admin testing is not metered. Writing a ledger row that says otherwise
        # would make the history of an owner account look like a customer's and
        # hide the exact flow the ledger exists to show.
        if is_admin(user.email):
            return True
        if user.credits < amount:
            return False
        user.credits -= amount
        session.add(CreditLedger(user_id=user_id, delta=-amount,
                                 balance_after=user.credits,
                                 job_id=job_id, note=note or "clip job"))
        session.commit()
        return True


def refund(user_id: str, amount: int, job_id: str = None) -> bool:
    """Returns credits for work that was charged and then could not be delivered.

    Charging happens once the clip count is known but before rendering, so any
    later failure -- most often the source download being refused after the
    transcript was already paid for -- used to leave the user billed for
    nothing. The ledger keeps both rows so the history still shows what
    happened rather than quietly rewriting it.
    """
    if not user_id or amount <= 0:
        return False
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if not user or is_admin(user.email):
            return False
        user.credits += amount
        session.add(CreditLedger(user_id=user_id, delta=amount,
                                 balance_after=user.credits, job_id=job_id,
                                 note="refund: job failed"))
        session.commit()
        return True


def refund_job(job_id: str) -> int:
    """Return whatever a job took and never delivered. Returns the amount.

    Reads the ledger rather than recomputing a price: a job that crashed part
    way through may have been charged for a clip count the run never reached,
    and guessing at it would refund the wrong number in one direction or the
    other. The ledger is what actually happened.

    Idempotent by construction -- existing refunds for the job are subtracted,
    so calling it twice returns 0 the second time. That matters because the
    crash paths that need it can overlap: the pipeline's own handler, the
    worker's catch-all, and the stale-claim reaper can all reasonably decide a
    job is owed money.
    """
    from app.models import Job

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job or not job.user_id:
            return 0
        user_id = job.user_id
        rows = (session.query(CreditLedger)
                .filter(CreditLedger.job_id == job_id).all())
        # Charges are negative, refunds positive; anything left is still owed.
        owed = -sum(r.delta for r in rows)
    if owed <= 0:
        return 0
    return owed if refund(user_id, owed, job_id=job_id) else 0


def grant_credits(user_id: str, amount: int, note: str = "purchase") -> int:
    """Adds credits for trusted internal/admin flows.

    Payment webhooks use their own transactional idempotency marker before
    changing a balance; calling this directly from a retried webhook would be
    unsafe.
    """
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if not user:
            return 0
        user.credits += amount
        session.add(CreditLedger(user_id=user_id, delta=amount,
                                 balance_after=user.credits, note=note))
        session.commit()
        return user.credits


def history(user_id: str, limit: int = 50) -> list:
    with SessionLocal() as session:
        rows = (session.query(CreditLedger)
                .filter(CreditLedger.user_id == user_id)
                .order_by(CreditLedger.created_at.desc())
                .limit(limit).all())
        return [{
            "delta": r.delta,
            "balance_after": r.balance_after,
            "note": r.note,
            "job_id": r.job_id,
            # Offset-stamped for the same reason Job.created_at is -- see
            # models._iso_utc.
            "at": _iso_utc(r.created_at),
        } for r in rows]
