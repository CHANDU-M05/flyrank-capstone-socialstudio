"""Durable scheduling: idempotency, claiming, retries, Retry-After and crash recovery."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.entities import PublicationAttempt, ScheduledJob, Variant
from app.publishers.mock import MockXPublisher
from app.publishers.registry import build_registry
from app.scheduler.worker import claim_due_jobs, recover_stale_claims, run_due_jobs
from app.services import publishing, review, scheduling, sources, variants


def _approved_variant(db, now, markdown, platform="x") -> Variant:
    source = sources.create_pasted_source(
        db, content=markdown, title=None, content_type="text/markdown", now=now
    )
    variant = variants.generate_variants(db, source, [platform], now=now)[0]
    review.approve_variant(db, variant, now=now)
    return variant


def _scheduled(db, now, markdown, settings, *, when=None, platform="x"):
    variant = _approved_variant(db, now, markdown, platform=platform)
    job, deduplicated = scheduling.schedule_variant(
        db, variant, scheduled_for=when or now - timedelta(seconds=1), settings=settings, now=now
    )
    assert deduplicated is False
    return variant, job


# --------------------------------------------------------------------------- idempotency
def test_idempotency_key_depends_only_on_variant_and_slot(now):
    key = scheduling.compute_idempotency_key("variant-1", now)
    assert key == scheduling.compute_idempotency_key("variant-1", now)
    assert len(key) == 32
    assert key != scheduling.compute_idempotency_key("variant-2", now)
    assert key != scheduling.compute_idempotency_key("variant-1", now + timedelta(seconds=1))


def test_sub_second_differences_normalise_to_the_same_slot(now):
    nudged = now.replace(microsecond=400_000)
    assert scheduling.compute_idempotency_key("v", now) == scheduling.compute_idempotency_key("v", nudged)


def test_duplicate_schedule_request_returns_the_same_job(db, now, sample_markdown, settings):
    variant = _approved_variant(db, now, sample_markdown)
    slot = now + timedelta(hours=1)

    first, first_dup = scheduling.schedule_variant(
        db, variant, scheduled_for=slot, settings=settings, now=now
    )
    assert first_dup is False
    assert variant.status == "scheduled"

    # The same slot again — even though the variant is no longer "approved".
    second, second_dup = scheduling.schedule_variant(
        db, variant, scheduled_for=slot, settings=settings, now=now
    )
    assert second_dup is True
    assert second.id == first.id
    assert db.scalar(select(func.count()).select_from(ScheduledJob)) == 1


def test_second_slot_for_an_already_scheduled_variant_is_a_conflict(
    db, now, sample_markdown, settings
):
    """A variant publishes once. Scheduling a second slot would be a second post."""
    from app.services.errors import InvalidTransition

    variant = _approved_variant(db, now, sample_markdown)
    first, _ = scheduling.schedule_variant(
        db, variant, scheduled_for=now + timedelta(hours=1), settings=settings, now=now
    )

    with pytest.raises(InvalidTransition):
        scheduling.schedule_variant(
            db, variant, scheduled_for=now + timedelta(hours=2), settings=settings, now=now
        )
    assert db.scalar(select(func.count()).select_from(ScheduledJob)) == 1


def test_naive_schedule_time_is_rejected(db, now, sample_markdown, settings):
    from app.services.errors import ValidationFailed

    variant = _approved_variant(db, now, sample_markdown)
    with pytest.raises(ValidationFailed) as excinfo:
        scheduling.schedule_variant(
            db, variant, scheduled_for=now.replace(tzinfo=None), settings=settings, now=now
        )
    assert excinfo.value.code == "naive_datetime"


def test_schedule_horizon_is_bounded(db, now, sample_markdown, settings):
    from app.services.errors import ValidationFailed

    variant = _approved_variant(db, now, sample_markdown)
    with pytest.raises(ValidationFailed) as excinfo:
        scheduling.schedule_variant(
            db, variant, scheduled_for=now + timedelta(days=400), settings=settings, now=now
        )
    assert excinfo.value.code == "schedule_too_far"


def test_database_rejects_a_duplicate_idempotency_key(db, now, sample_markdown, settings):
    _, job = _scheduled(db, now, sample_markdown, settings)

    clone = ScheduledJob(
        variant_id=job.variant_id,
        platform=job.platform,
        scheduled_for=job.scheduled_for,
        next_attempt_at=job.next_attempt_at,
        status="pending",
        attempts=0,
        max_attempts=3,
        idempotency_key=job.idempotency_key,
        created_at=now,
        updated_at=now,
    )
    db.add(clone)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# --------------------------------------------------------------------------- claiming
def test_future_jobs_are_not_claimed_early(db, now, sample_markdown, settings):
    _, job = _scheduled(db, now, sample_markdown, settings, when=now + timedelta(hours=2))
    assert claim_due_jobs(db, now=now, limit=10) == []
    assert db.get(ScheduledJob, job.id).status == "pending"


def test_due_jobs_are_claimed_once(db, now, sample_markdown, settings):
    _, job = _scheduled(db, now, sample_markdown, settings)

    claimed = claim_due_jobs(db, now=now, limit=10)
    assert [item.id for item in claimed] == [job.id]
    assert claimed[0].status == "processing"
    assert claimed[0].claim_token is not None

    # A second worker (or a second tick) finds nothing to do.
    assert claim_due_jobs(db, now=now, limit=10) == []


def test_two_sessions_cannot_claim_the_same_job(db, session_factory, now, sample_markdown, settings):
    _, job = _scheduled(db, now, sample_markdown, settings)

    other = session_factory()
    try:
        assert [item.id for item in claim_due_jobs(db, now=now, limit=10)] == [job.id]
        assert claim_due_jobs(other, now=now, limit=10) == []
    finally:
        other.close()


# --------------------------------------------------------------------------- running
def test_due_job_publishes_and_records_history(db, now, sample_markdown, settings, registry):
    variant, job = _scheduled(db, now, sample_markdown, settings)

    report = run_due_jobs(db, registry, settings, now=now)

    assert (report.claimed, report.completed, report.retried, report.failed) == (1, 1, 0, 0)
    finished = db.get(ScheduledJob, job.id)
    assert finished.status == "completed"
    assert finished.external_id.startswith("mock_x-")
    assert finished.completed_at.tzinfo is not None
    assert db.get(Variant, variant.id).status == "published"

    attempts = db.execute(select(PublicationAttempt)).scalars().all()
    assert len(attempts) == 1
    assert attempts[0].success is True
    assert attempts[0].attempt_number == 1
    assert attempts[0].external_id == finished.external_id
    assert attempts[0].finished_at is not None

    deliveries = registry.get("x").deliveries
    assert len(deliveries) == 1


def test_running_twice_does_not_publish_twice(db, now, sample_markdown, settings, registry):
    _scheduled(db, now, sample_markdown, settings)

    run_due_jobs(db, registry, settings, now=now)
    second = run_due_jobs(db, registry, settings, now=now)

    assert second.claimed == 0
    assert len(registry.get("x").deliveries) == 1
    assert db.scalar(select(func.count()).select_from(PublicationAttempt)) == 1


def test_retryable_failure_backs_off_and_then_succeeds(db, now, sample_markdown, settings, registry):
    _, job = _scheduled(db, now, sample_markdown, settings)
    mock = registry.get("x")
    mock.fail_mode = "retryable"

    first = run_due_jobs(db, registry, settings, now=now)
    assert first.retried == 1
    retried = db.get(ScheduledJob, job.id)
    assert retried.status == "pending"
    assert retried.attempts == 1
    assert retried.next_attempt_at == now + timedelta(seconds=settings.retry_base_seconds)
    assert retried.last_error

    # Not due yet.
    assert run_due_jobs(db, registry, settings, now=now).claimed == 0

    mock.fail_mode = None
    later = now + timedelta(seconds=settings.retry_base_seconds + 1)
    second = run_due_jobs(db, registry, settings, now=later)
    assert second.completed == 1

    done = db.get(ScheduledJob, job.id)
    assert done.status == "completed"
    assert done.attempts == 2
    attempt_numbers = [
        row.attempt_number
        for row in db.execute(select(PublicationAttempt).order_by(PublicationAttempt.attempt_number)).scalars()
    ]
    assert attempt_numbers == [1, 2]


def test_retry_after_from_the_publisher_wins_over_backoff(db, now, sample_markdown, settings):
    _, job = _scheduled(db, now, sample_markdown, settings)
    configured = build_registry(settings, overrides={"x": MockXPublisher(fail_mode="retryable", retry_after_seconds=120)})

    outcome = publishing.publish_job(db, claim_due_jobs(db, now=now, limit=10)[0], configured, settings, now=now)

    assert outcome.outcome == publishing.OUTCOME_RETRY
    assert outcome.retry_after_seconds == 120
    assert db.get(ScheduledJob, job.id).next_attempt_at == now + timedelta(seconds=120)


def test_permanent_failure_stops_immediately(db, now, sample_markdown, settings, registry):
    variant, job = _scheduled(db, now, sample_markdown, settings)
    registry.get("x").fail_mode = "permanent"

    report = run_due_jobs(db, registry, settings, now=now)

    assert report.failed == 1
    failed = db.get(ScheduledJob, job.id)
    assert failed.status == "failed"
    assert failed.attempts == 1
    assert "permanent" in (failed.last_error or "")
    assert db.get(Variant, variant.id).status == "approved"


def test_retries_are_bounded(db, now, sample_markdown, settings, registry):
    _, job = _scheduled(db, now, sample_markdown, settings)
    registry.get("x").fail_mode = "retryable"

    moment = now
    for _ in range(settings.max_attempts):
        run_due_jobs(db, registry, settings, now=moment)
        moment = moment + timedelta(seconds=settings.retry_max_seconds + 1)

    exhausted = db.get(ScheduledJob, job.id)
    assert exhausted.status == "failed"
    assert exhausted.attempts == settings.max_attempts

    # No further attempts once terminal.
    assert run_due_jobs(db, registry, settings, now=moment).claimed == 0


def test_unconfigured_telegram_fails_clearly(db, now, sample_markdown, settings):
    variant, job = _scheduled(db, now, sample_markdown, settings, platform="telegram")
    registry = build_registry(settings)  # telegram disabled by default

    report = run_due_jobs(db, registry, settings, now=now)

    assert report.failed == 1
    failed = db.get(ScheduledJob, job.id)
    assert failed.status == "failed"
    assert "not configured" in (failed.last_error or "").lower()
    assert db.get(Variant, variant.id).status == "approved"
    attempt = db.execute(select(PublicationAttempt)).scalars().one()
    assert attempt.success is False
    assert attempt.adapter == "telegram"


def test_dry_run_records_an_attempt_without_consuming_it(db, now, sample_markdown, tmp_path):
    from app.publishers.telegram import TelegramPublisher

    dry = make_dry_run_settings(tmp_path)
    variant = _approved_variant(db, now, sample_markdown, platform="telegram")
    job, _ = scheduling.schedule_variant(
        db, variant, scheduled_for=now - timedelta(seconds=1), settings=dry, now=now
    )
    registry = build_registry(dry, overrides={"telegram": TelegramPublisher(dry)})

    report = run_due_jobs(db, registry, dry, now=now)

    assert report.skipped == 1
    after = db.get(ScheduledJob, job.id)
    assert after.status == "pending"
    assert after.attempts == 0
    assert "DRY_RUN" in (after.last_error or "")
    attempt = db.execute(select(PublicationAttempt)).scalars().one()
    assert attempt.dry_run is True
    assert attempt.success is False


def make_dry_run_settings(tmp_path):
    from tests.conftest import make_settings

    return make_settings(
        tmp_path,
        telegram_enabled=True,
        telegram_bot_token="123456789:AAtesttokenvalue0123456789",
        telegram_chat_id="-1001234567890",
        telegram_dry_run=True,
    )


# --------------------------------------------------------------------------- recovery
def test_stale_claims_are_returned_to_the_queue(db, now, sample_markdown, settings):
    _, job = _scheduled(db, now, sample_markdown, settings)
    claim_due_jobs(db, now=now, limit=10)

    later = now + timedelta(seconds=settings.stale_claim_seconds + 1)
    recovered = recover_stale_claims(db, now=later, settings=settings)

    assert recovered == 1
    back = db.get(ScheduledJob, job.id)
    assert back.status == "pending"
    assert back.claimed_at is None
    assert back.claim_token is None
    assert "recovered" in (back.last_error or "")


def test_fresh_claims_are_not_recovered(db, now, sample_markdown, settings):
    _scheduled(db, now, sample_markdown, settings)
    claim_due_jobs(db, now=now, limit=10)
    assert recover_stale_claims(db, now=now, settings=settings) == 0


def test_interrupted_attempt_is_closed_with_an_error(db, now, sample_markdown, settings):
    _, job = _scheduled(db, now, sample_markdown, settings)
    claim_due_jobs(db, now=now, limit=10)
    db.add(
        PublicationAttempt(
            job_id=job.id,
            variant_id=job.variant_id,
            platform=job.platform,
            adapter="mock_x",
            attempt_number=1,
            started_at=now,
            finished_at=None,
            success=False,
            retryable=False,
            dry_run=False,
        )
    )
    db.commit()

    later = now + timedelta(seconds=settings.stale_claim_seconds + 1)
    recover_stale_claims(db, now=later, settings=settings)

    attempt = db.execute(select(PublicationAttempt)).scalars().one()
    assert attempt.finished_at is not None
    assert "interrupted" in (attempt.error or "")

    # And the job really is publishable again.
    report = run_due_jobs(db, build_registry(settings), settings, now=later)
    assert report.completed == 1


def test_jobs_and_history_survive_a_new_session(db, session_factory, now, sample_markdown, settings, registry):
    _scheduled(db, now, sample_markdown, settings)
    run_due_jobs(db, registry, settings, now=now)

    fresh = session_factory()
    try:
        assert fresh.scalar(select(func.count()).select_from(ScheduledJob)) == 1
        history, total = publishing.list_attempts(
            fresh, variant_id=None, job_id=None, limit=10, offset=0
        )
        assert total == 1
        assert history[0].success is True
        assert history[0].external_id
    finally:
        fresh.close()


def test_backoff_is_capped(settings):
    assert publishing.backoff_delay(1, settings) == settings.retry_base_seconds
    assert publishing.backoff_delay(2, settings) == settings.retry_base_seconds * 2
    assert publishing.backoff_delay(10, settings) == settings.retry_max_seconds
