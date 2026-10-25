"""Publishing a single job, and the durable history it produces.

This module owns the only place where ``SocialPublisher.publish`` is called. It records an
``PublicationAttempt`` row for *every* call — success, retryable failure, permanent failure
and dry-run abort alike — so history reflects what actually happened even when nothing was
delivered.

Nothing secret can reach the database: adapter messages pass through ``redact()`` before
they are stored, and the adapter itself never includes the token in an error string.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.entities import PublicationAttempt, ScheduledJob, Variant
from app.publishers.base import (
    PublisherDryRunAborted,
    PublisherNotConfigured,
    PublisherPermanentError,
    PublisherRetryableError,
    PublishRequest,
)
from app.publishers.registry import PublisherRegistry
from app.services import review
from app.services.profiles import compose_full_text
from app.services.sanitize import redact
from app.services.variants import apply_validation

#: Outcome of one attempt, consumed by the worker's counters.
OUTCOME_COMPLETED = "completed"
OUTCOME_RETRY = "retry"
OUTCOME_FAILED = "failed"
OUTCOME_DRY_RUN = "dry_run"


@dataclass
class PublicationOutcome:
    job_id: str
    outcome: str
    error: str | None = None
    external_id: str | None = None
    retry_after_seconds: int | None = None
    next_attempt_at: datetime | None = None


def backoff_delay(attempts: int, settings: Settings) -> int:
    """Exponential backoff in seconds, capped. ``attempts`` is the count *after* this one."""
    exponent = max(0, attempts - 1)
    return min(settings.retry_base_seconds * (2**exponent), settings.retry_max_seconds)


def _next_attempt_number(db: Session, job: ScheduledJob) -> int:
    """Attempt numbers are derived from the history, so they stay unique even when a
    dry-run attempt is recorded without consuming a retry."""
    highest = db.scalar(
        select(func.max(PublicationAttempt.attempt_number)).where(
            PublicationAttempt.job_id == job.id
        )
    )
    return int(highest or 0) + 1


def _record(
    db: Session,
    job: ScheduledJob,
    *,
    adapter_name: str,
    attempt_number: int,
    started_at: datetime,
    finished_at: datetime,
    success: bool,
    retryable: bool,
    dry_run: bool,
    external_id: str | None,
    error: str | None,
    retry_after_seconds: int | None,
) -> PublicationAttempt:
    attempt = PublicationAttempt(
        job_id=job.id,
        variant_id=job.variant_id,
        platform=job.platform,
        adapter=adapter_name,
        attempt_number=attempt_number,
        started_at=started_at,
        finished_at=finished_at,
        success=success,
        retryable=retryable,
        dry_run=dry_run,
        external_id=external_id,
        error=error,
        retry_after_seconds=retry_after_seconds,
    )
    db.add(attempt)
    return attempt


def _variant_for(db: Session, job: ScheduledJob) -> Variant:
    variant = db.get(Variant, job.variant_id)
    if variant is None:  # pragma: no cover - FK + cascade make this unreachable
        raise LookupError(f"Variant '{job.variant_id}' disappeared from under job '{job.id}'")
    return variant


def publish_job(
    db: Session,
    job: ScheduledJob,
    registry: PublisherRegistry,
    settings: Settings,
    *,
    now: datetime,
) -> PublicationOutcome:
    """Attempt one delivery for ``job`` and persist the outcome.

    The job is expected to be claimed (status ``processing``) by the worker. A job that is
    already ``completed`` is never re-published: the caller gets ``completed`` back.
    """
    if job.status == "completed":
        return PublicationOutcome(job_id=job.id, outcome=OUTCOME_COMPLETED, external_id=job.external_id)

    variant = _variant_for(db, job)
    attempt_number = _next_attempt_number(db, job)
    started_at = now

    # A variant may have been sitting in the queue while its content changed underneath it.
    # Re-validating here is the last line of defence against publishing invalid text.
    validation = apply_validation(variant)
    if not validation.valid:
        message = redact(
            "variant failed validation at publish time: " + "; ".join(validation.errors)
        )
        _record(
            db,
            job,
            adapter_name="validation",
            attempt_number=attempt_number,
            started_at=started_at,
            finished_at=now,
            success=False,
            retryable=False,
            dry_run=False,
            external_id=None,
            error=message,
            retry_after_seconds=None,
        )
        return _fail_terminally(db, job, variant, message, now)

    try:
        adapter = registry.get(job.platform)
    except LookupError as exc:
        message = redact(f"no adapter registered: {exc}")
        _record(
            db,
            job,
            adapter_name="none",
            attempt_number=attempt_number,
            started_at=started_at,
            finished_at=now,
            success=False,
            retryable=False,
            dry_run=False,
            external_id=None,
            error=message,
            retry_after_seconds=None,
        )
        return _fail_terminally(db, job, variant, message, now)

    request = PublishRequest(
        text=compose_full_text(variant.text, list(variant.hashtags or [])),
        hashtags=list(variant.hashtags or []),
        idempotency_key=job.idempotency_key,
        variant_id=variant.id,
        scheduled_for=job.scheduled_for,
    )

    try:
        result = adapter.publish(request)
    except PublisherDryRunAborted as exc:
        # Deliberately *not* a success and deliberately *not* a consumed attempt: the job
        # stays pending so it will be attempted for real once dry run is switched off.
        message = redact(str(exc))
        _record(
            db,
            job,
            adapter_name=adapter.name,
            attempt_number=attempt_number,
            started_at=started_at,
            finished_at=now,
            success=False,
            retryable=True,
            dry_run=True,
            external_id=None,
            error=message,
            retry_after_seconds=None,
        )
        job.status = "pending"
        job.claimed_at = None
        job.claim_token = None
        job.last_error = message
        job.updated_at = now
        db.commit()
        return PublicationOutcome(job_id=job.id, outcome=OUTCOME_DRY_RUN, error=message)

    except PublisherRetryableError as exc:
        message = redact(str(exc))
        retry_after = exc.retry_after_seconds
        _record(
            db,
            job,
            adapter_name=adapter.name,
            attempt_number=attempt_number,
            started_at=started_at,
            finished_at=now,
            success=False,
            retryable=True,
            dry_run=False,
            external_id=None,
            error=message,
            retry_after_seconds=retry_after,
        )
        job.attempts = attempt_number
        job.last_error = message
        job.claimed_at = None
        job.claim_token = None
        job.updated_at = now

        if attempt_number >= job.max_attempts:
            job.status = "failed"
            review.mark_publish_failed(db, variant, now=now)
            db.commit()
            return PublicationOutcome(
                job_id=job.id,
                outcome=OUTCOME_FAILED,
                error=message,
                retry_after_seconds=retry_after,
            )

        delay = retry_after if retry_after is not None else backoff_delay(attempt_number, settings)
        job.status = "pending"
        job.next_attempt_at = now + timedelta(seconds=max(0, delay))
        db.commit()
        return PublicationOutcome(
            job_id=job.id,
            outcome=OUTCOME_RETRY,
            error=message,
            retry_after_seconds=retry_after,
            next_attempt_at=job.next_attempt_at,
        )

    except (PublisherPermanentError, PublisherNotConfigured) as exc:
        message = redact(str(exc))
        _record(
            db,
            job,
            adapter_name=adapter.name,
            attempt_number=attempt_number,
            started_at=started_at,
            finished_at=now,
            success=False,
            retryable=False,
            dry_run=False,
            external_id=None,
            error=message,
            retry_after_seconds=None,
        )
        return _fail_terminally(db, job, variant, message, now)

    except Exception as exc:  # noqa: BLE001 - an adapter bug must not crash the worker
        message = redact(f"unexpected adapter failure ({exc.__class__.__name__}): {exc}")
        _record(
            db,
            job,
            adapter_name=adapter.name,
            attempt_number=attempt_number,
            started_at=started_at,
            finished_at=now,
            success=False,
            retryable=False,
            dry_run=False,
            external_id=None,
            error=message,
            retry_after_seconds=None,
        )
        return _fail_terminally(db, job, variant, message, now)

    _record(
        db,
        job,
        adapter_name=adapter.name,
        attempt_number=attempt_number,
        started_at=started_at,
        finished_at=now,
        success=True,
        retryable=False,
        dry_run=False,
        external_id=result.external_id,
        error=None,
        retry_after_seconds=None,
    )
    job.attempts = attempt_number
    job.status = "completed"
    job.external_id = result.external_id
    job.last_error = None
    job.claimed_at = None
    job.claim_token = None
    job.completed_at = now
    job.updated_at = now
    review.mark_published(db, variant, now=now)
    db.commit()
    return PublicationOutcome(
        job_id=job.id, outcome=OUTCOME_COMPLETED, external_id=result.external_id
    )


def _fail_terminally(
    db: Session, job: ScheduledJob, variant: Variant, message: str | None, now: datetime
) -> PublicationOutcome:
    job.attempts = job.attempts + 1
    job.status = "failed"
    job.last_error = message
    job.claimed_at = None
    job.claim_token = None
    job.updated_at = now
    review.mark_publish_failed(db, variant, now=now)
    db.commit()
    return PublicationOutcome(job_id=job.id, outcome=OUTCOME_FAILED, error=message)


# --------------------------------------------------------------------------- history
def list_attempts(
    db: Session, *, variant_id: str | None, job_id: str | None, limit: int, offset: int
) -> tuple[list[PublicationAttempt], int]:
    """Durable publication history, newest first."""
    conditions = []
    if variant_id:
        conditions.append(PublicationAttempt.variant_id == variant_id)
    if job_id:
        conditions.append(PublicationAttempt.job_id == job_id)

    total_query = select(func.count()).select_from(PublicationAttempt)
    rows_query = select(PublicationAttempt)
    for condition in conditions:
        total_query = total_query.where(condition)
        rows_query = rows_query.where(condition)

    total = db.scalar(total_query) or 0
    rows = (
        db.execute(
            rows_query.order_by(
                PublicationAttempt.started_at.desc(), PublicationAttempt.attempt_number.desc()
            )
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


__all__ = [
    "OUTCOME_COMPLETED",
    "OUTCOME_DRY_RUN",
    "OUTCOME_FAILED",
    "OUTCOME_RETRY",
    "PublicationOutcome",
    "backoff_delay",
    "list_attempts",
    "publish_job",
]
