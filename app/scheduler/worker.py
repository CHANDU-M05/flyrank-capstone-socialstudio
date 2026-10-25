"""Durable scheduler: claim, publish, retry, recover.

The database is the scheduler. There is no in-process timer and no APScheduler: a job is a
row, "due" is a SQL predicate, and a crashed process leaves recoverable state behind.

Claiming is optimistic. ``UPDATE ... WHERE id = :id AND status = 'pending'`` returns
``rowcount == 1`` for exactly one winner, which is what stops two workers from publishing the
same job. SQLite serialises writers, so the update is atomic with respect to other writers.

Crash recovery is a lease: any job stuck in ``processing`` whose ``claimed_at`` is older than
``stale_claim_seconds`` is returned to ``pending`` and any dangling attempt is closed with an
``interrupted`` error. Whether the remote platform actually received that message is
unknowable — this is the documented exactly-once gap, not a bug we can paper over.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.entities import PublicationAttempt, ScheduledJob, utcnow
from app.publishers.registry import PublisherRegistry
from app.services import publishing

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR = "interrupted: worker claim expired before an outcome was recorded"


@dataclass
class WorkerReport:
    """Counters for one worker tick. Mirrors ``WorkerRunResponse``."""

    recovered: int = 0
    claimed: int = 0
    completed: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0
    job_ids: list[str] = field(default_factory=list)


def recover_stale_claims(db: Session, *, now: datetime, settings: Settings) -> int:
    """Return abandoned ``processing`` jobs to the queue. Safe to call on every tick."""
    cutoff = now - timedelta(seconds=settings.stale_claim_seconds)
    stale_ids = list(
        db.execute(
            select(ScheduledJob.id).where(
                ScheduledJob.status == "processing",
                ScheduledJob.claimed_at.is_not(None),
                ScheduledJob.claimed_at < cutoff,
            )
        ).scalars()
    )
    if not stale_ids:
        return 0

    db.execute(
        update(PublicationAttempt)
        .where(
            PublicationAttempt.job_id.in_(stale_ids),
            PublicationAttempt.finished_at.is_(None),
        )
        .values(
            finished_at=now,
            success=False,
            retryable=True,
            error=INTERRUPTED_ERROR,
        )
        .execution_options(synchronize_session=False)
    )
    db.execute(
        update(ScheduledJob)
        .where(ScheduledJob.id.in_(stale_ids))
        .values(
            status="pending",
            claimed_at=None,
            claim_token=None,
            updated_at=now,
            last_error="recovered after a stale claim; the previous worker did not finish",
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.expire_all()
    logger.warning("recovered %s stale job(s) whose claim had expired", len(stale_ids))
    return len(stale_ids)


def claim_due_jobs(
    db: Session, *, now: datetime, limit: int
) -> list[ScheduledJob]:
    """Claim up to ``limit`` due jobs. Jobs in the future are never touched."""
    candidate_ids = list(
        db.execute(
            select(ScheduledJob.id)
            .where(ScheduledJob.status == "pending", ScheduledJob.next_attempt_at <= now)
            .order_by(ScheduledJob.next_attempt_at, ScheduledJob.id)
            .limit(limit)
        ).scalars()
    )
    claimed: list[str] = []
    for job_id in candidate_ids:
        result = db.execute(
            update(ScheduledJob)
            .where(ScheduledJob.id == job_id, ScheduledJob.status == "pending")
            .values(
                status="processing",
                claim_token=uuid.uuid4().hex,
                claimed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if result.rowcount == 1:
            claimed.append(job_id)

    if not claimed:  # pragma: no cover - another worker won every race
        return []
    db.expire_all()
    jobs = list(
        db.execute(
            select(ScheduledJob)
            .where(ScheduledJob.id.in_(claimed))
            .order_by(ScheduledJob.next_attempt_at, ScheduledJob.id)
        ).scalars()
    )
    return jobs


def run_due_jobs(
    db: Session,
    registry: PublisherRegistry,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int = 10,
) -> WorkerReport:
    """One worker tick: recover, claim, publish. Returns counters, never raises for a job."""
    moment = now or utcnow()
    report = WorkerReport()
    report.recovered = recover_stale_claims(db, now=moment, settings=settings)

    jobs = claim_due_jobs(db, now=moment, limit=limit)
    report.claimed = len(jobs)

    for job in jobs:
        try:
            outcome = publishing.publish_job(db, job, registry, settings, now=moment)
        except Exception:  # noqa: BLE001 - a crash here must not strand the claim
            db.rollback()
            logger.exception("job %s failed unexpectedly; releasing the claim", job.id)
            db.execute(
                update(ScheduledJob)
                .where(ScheduledJob.id == job.id, ScheduledJob.status == "processing")
                .values(status="pending", claimed_at=None, claim_token=None, updated_at=moment)
                .execution_options(synchronize_session=False)
            )
            db.commit()
            report.failed += 1
            report.job_ids.append(job.id)
            continue

        report.job_ids.append(job.id)
        if outcome.outcome == publishing.OUTCOME_COMPLETED:
            report.completed += 1
        elif outcome.outcome == publishing.OUTCOME_RETRY:
            report.retried += 1
        elif outcome.outcome == publishing.OUTCOME_DRY_RUN:
            report.skipped += 1
        else:
            report.failed += 1

    if report.claimed:
        logger.info(
            "worker tick: claimed=%s completed=%s retried=%s failed=%s skipped=%s recovered=%s",
            report.claimed,
            report.completed,
            report.retried,
            report.failed,
            report.skipped,
            report.recovered,
        )
    return report


def run_forever(
    session_factory,
    registry: PublisherRegistry,
    settings: Settings,
    *,
    stop_after_ticks: int | None = None,
) -> int:
    """Poll loop for ``scripts/run_worker.py``. Returns the number of ticks executed."""
    import time

    ticks = 0
    while stop_after_ticks is None or ticks < stop_after_ticks:
        db = session_factory()
        try:
            run_due_jobs(db, registry, settings, now=datetime.now(timezone.utc))
        finally:
            db.close()
        ticks += 1
        if stop_after_ticks is not None and ticks >= stop_after_ticks:
            break
        time.sleep(settings.scheduler_poll_seconds)
    return ticks
