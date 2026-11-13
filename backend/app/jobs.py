"""Job persistence.

This was an in-memory dict, which meant a server restart lost every job and the
rendered clips on disk became orphans nobody could list. It is now backed by the
database while keeping the original create/get/update interface, so the pipeline
code calling it did not have to change.

Each call opens and closes its own short-lived session on purpose: the pipeline
runs on a FastAPI background thread and updates progress many times per job, and
holding one session open across a multi-minute render would pin a connection and
risk stale reads.
"""

from datetime import datetime, timedelta, timezone
import os
import shutil
from typing import Optional

from app import billing, storage
from app.db import SessionLocal
from app.models import Clip, Job, User, _iso_utc


STORAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "storage"))

# Fields that live on the Job row itself; anything else passed to update_job is
# handled specially (clips) or ignored.
JOB_FIELDS = {
    "status", "progress_message", "percent", "error",
    "transcript", "source_duration",
    # Written once more after creation, when analysis has worked out what kind
    # of video this is -- see the genre note in main._run_pipeline_locked.
    "options",
}


def create_job(url: str, options: dict, user_id: str = None) -> str:
    with SessionLocal() as session:
        options = dict(options or {})
        if "retention_until" not in options:
            user = session.get(User, user_id) if user_id else None
            hours = billing.retention_hours_for(
                user.plan if user else None, user.email if user else None)
            options["retention_until"] = (
                datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        job = Job(url=url, options=options, user_id=user_id, status="queued")
        session.add(job)
        session.commit()
        return job.id


def update_job(job_id: str, **fields):
    """Updates job columns, and replaces the clip set when `clips` is passed."""
    clips = fields.pop("clips", None)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return

        for key, value in fields.items():
            if key in JOB_FIELDS:
                setattr(job, key, value)

        # Retention starts when the clips become usable, not when a long source
        # first entered the queue.
        if fields.get("status") == "done":
            user = job.user
            hours = billing.retention_hours_for(
                user.plan if user else None, user.email if user else None)
            options = dict(job.options or {})
            options["retention_until"] = (
                datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
            job.options = options

        if clips is not None:
            # Rendering reports the full list each time; replace wholesale so a
            # re-run cannot leave rows from a previous attempt behind.
            session.query(Clip).filter(Clip.job_id == job_id).delete()
            for n, c in enumerate(clips):
                session.add(Clip(
                    job_id=job_id,
                    index=n,
                    file=c.get("file", ""),
                    title=c.get("title", ""),
                    hook=c.get("hook", ""),
                    start=c.get("start", 0.0),
                    end=c.get("end", 0.0),
                    duration=c.get("duration", 0.0),
                    score=c.get("score"),
                    scores=c.get("scores"),
                    keywords=c.get("keywords"),
                    words=c.get("words"),
                ))

        session.commit()


def get_job(job_id: str) -> Optional[dict]:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        return job.to_dict() if job else None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def retention_deadline(job: Job) -> datetime:
    """The persisted deadline, with a plan-based fallback for older rows."""
    raw = (job.options or {}).get("retention_until")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return _aware(parsed)
        except (TypeError, ValueError):
            pass
    user = job.user
    hours = billing.retention_hours_for(
        user.plan if user else None, user.email if user else None)
    return _aware(job.created_at) + timedelta(hours=hours)


def expire_due_jobs(user_id: str = None, now: datetime = None) -> int:
    """Delete expired media while retaining its library row as Expired."""
    now = _aware(now or datetime.now(timezone.utc))
    expired = []
    with SessionLocal() as session:
        query = session.query(Job).filter(Job.status == "done")
        if user_id:
            query = query.filter(Job.user_id == user_id)
        for job in query.all():
            if retention_deadline(job) > now:
                continue
            shutil.rmtree(os.path.join(STORAGE_DIR, job.id), ignore_errors=True)

            # Object storage as well, and for two separate reasons. Cost is the
            # obvious one -- nothing else ever revisits these keys, so a bucket
            # would grow forever. The sharper one is that expiry is a PROMISE:
            # the local delete is what used to make an expired clip actually
            # gone, and once media lives in a bucket, deleting an empty local
            # directory leaves the clip fully downloadable by anyone who can
            # reach it. Retention would have become a label rather than a fact.
            try:
                if storage.driver.name != "local":
                    storage.driver.delete_prefix(job.id)
            except Exception as exc:
                # Do not abandon the sweep: one unreachable key must not stop
                # every later job from expiring.
                print(f"[clipper] could not purge storage for {job.id}: {exc}")

            job.status = "expired"
            job.progress_message = "Expired"
            expired.append(job.id)
        if expired:
            session.commit()
    return len(expired)


def extend_user_retention(session, user_id: str, hours: int = 72) -> int:
    """Give still-available projects a fresh paid editing window."""
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    changed = 0
    rows = (session.query(Job)
            .filter(Job.user_id == user_id, Job.status == "done")
            .all())
    for job in rows:
        options = dict(job.options or {})
        options["retention_until"] = until.isoformat()
        job.options = options
        changed += 1
    return changed


def list_jobs(user_id: str, limit: int = 50) -> list:
    """A signed-in user's history -- impossible before persistence existed."""
    expire_due_jobs(user_id)
    with SessionLocal() as session:
        rows = (session.query(Job)
                .filter(Job.user_id == user_id)
                .order_by(Job.created_at.desc())
                .limit(limit)
                .all())
        result = []
        for job in rows:
            item = job.to_dict()
            item["expires_at"] = (_iso_utc(retention_deadline(job))
                                  if job.status != "expired" else None)
            result.append(item)
        return result


def get_job_transcript(job_id: str) -> Optional[dict]:
    """Stored word-level transcript, for the clip editor."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        return job.transcript if job else None
