"""Scheduling: turning an approved variant into a durable job.

The idempotency identity is ``sha256(variant_id|slot)`` truncated to 32 hex characters and
enforced by ``scheduled_jobs.idempotency_key UNIQUE``. The check-then-insert is only an
optimisation for the friendly path; the database constraint is what makes concurrent
duplicate requests safe, so the ``IntegrityError`` branch is the real guarantee.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.entities import ScheduledJob, Variant
from app.services import review
from app.services.errors import InvalidTransition, NotFoundError, ValidationFailed
from app.services.variants import apply_validation

#: Slots further out than this are almost certainly a typo, and they would keep the queue
#: full of junk for a year.
MAX_FUTURE_DAYS = 365


def normalise_slot(scheduled_for: datetime) -> datetime:
    """UTC, whole seconds — the granularity the idempotency key is built on."""
    if scheduled_for.tzinfo is None:
        raise ValidationFailed(
            "scheduled_for must be timezone-aware (e.g. 2026-11-01T09:00:00Z).",
            code="naive_datetime",
        )
    return scheduled_for.astimezone(timezone.utc).replace(microsecond=0)


def compute_idempotency_key(variant_id: str, slot: datetime) -> str:
    stamp = normalise_slot(slot).strftime("%Y-%m-%dT%H:%M:%SZ")
    return hashlib.sha256(f"{variant_id}|{stamp}".encode()).hexdigest()[:32]


def get_job(db: Session, job_id: str) -> ScheduledJob:
    job = db.get(ScheduledJob, job_id)
    if job is None:
        raise NotFoundError(f"Job '{job_id}' was not found.", code="job_not_found")
    return job


def schedule_variant(
    db: Session,
    variant: Variant,
    *,
    scheduled_for: datetime,
    settings: Settings,
    now: datetime,
) -> tuple[ScheduledJob, bool]:
    """Create (or return the existing) job for this variant and slot.

    Returns ``(job, deduplicated)``. Deduplication is answered *before* the status check so
    a retried request gets the same job back instead of a confusing 409.
    """
    slot = normalise_slot(scheduled_for)
    key = compute_idempotency_key(variant.id, slot)

    existing = db.scalar(select(ScheduledJob).where(ScheduledJob.idempotency_key == key))
    if existing is not None:
        return existing, True

    if variant.status != "approved":
        raise InvalidTransition(
            f"Only approved variants can be scheduled; this one is '{variant.status}'. "
            "Approve it first (or reopen and edit it, then approve).",
            code="variant_not_approved",
            details={"status": variant.status},
        )

    result = apply_validation(variant)
    if not result.valid:
        raise ValidationFailed(
            "The variant is no longer valid, so it cannot be scheduled: "
            + "; ".join(result.errors),
            code="variant_invalid",
            details={"errors": list(result.errors), "warnings": list(result.warnings)},
        )

    horizon = now + timedelta(days=MAX_FUTURE_DAYS)
    if slot > horizon:
        raise ValidationFailed(
            f"scheduled_for is more than {MAX_FUTURE_DAYS} days in the future.",
            code="schedule_too_far",
        )

    job = ScheduledJob(
        variant_id=variant.id,
        platform=variant.platform,
        scheduled_for=slot,
        next_attempt_at=slot,
        status="pending",
        attempts=0,
        max_attempts=settings.max_attempts,
        idempotency_key=key,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    review.mark_scheduled(db, variant, now=now)

    try:
        db.commit()
    except IntegrityError:
        # Lost the race: another request inserted this exact slot first.
        db.rollback()
        winner = db.scalar(select(ScheduledJob).where(ScheduledJob.idempotency_key == key))
        if winner is None:  # pragma: no cover - the constraint fired for some other reason
            raise
        return winner, True

    db.refresh(job)
    return job, False


def list_jobs(
    db: Session, *, status: str | None, variant_id: str | None, limit: int, offset: int
) -> tuple[list[ScheduledJob], int]:
    conditions = []
    if status:
        conditions.append(ScheduledJob.status == status)
    if variant_id:
        conditions.append(ScheduledJob.variant_id == variant_id)

    total_query = select(func.count()).select_from(ScheduledJob)
    rows_query = select(ScheduledJob)
    for condition in conditions:
        total_query = total_query.where(condition)
        rows_query = rows_query.where(condition)

    total = db.scalar(total_query) or 0
    rows = (
        db.execute(
            rows_query.order_by(ScheduledJob.scheduled_for.desc(), ScheduledJob.id)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)
