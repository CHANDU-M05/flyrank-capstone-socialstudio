"""Persistence guarantees: foreign keys, uniqueness, cascade behaviour and UTC storage."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from app.db.session import make_engine
from app.models.entities import PublicationAttempt, ScheduledJob, Source, Variant


def test_foreign_keys_are_enforced(db, now):
    db.add(
        Variant(
            source_id="missing-source",
            platform="x",
            text="orphan",
            hashtags=[],
            validation={},
            status="draft",
            created_at=now,
            updated_at=now,
            status_changed_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_deleting_a_source_cascades_to_variants(db, now, sample_markdown):
    from app.services import sources, variants

    source = sources.create_pasted_source(
        db, content=sample_markdown, title=None, content_type="text/markdown", now=now
    )
    variants.generate_variants(db, source, now=now)
    assert db.scalar(select(func.count()).select_from(Variant)) == 3

    db.delete(source)
    db.commit()
    assert db.scalar(select(func.count()).select_from(Variant)) == 0


def test_deleting_a_job_cascades_to_attempts(db, now, sample_markdown, settings, registry):
    from app.scheduler.worker import run_due_jobs
    from app.services import review, scheduling, sources, variants

    source = sources.create_pasted_source(
        db, content=sample_markdown, title=None, content_type="text/markdown", now=now
    )
    variant = variants.generate_variants(db, source, ["x"], now=now)[0]
    review.approve_variant(db, variant, now=now)
    job, _ = scheduling.schedule_variant(
        db, variant, scheduled_for=now - timedelta(seconds=1), settings=settings, now=now
    )
    run_due_jobs(db, registry, settings, now=now)
    assert db.scalar(select(func.count()).select_from(PublicationAttempt)) == 1

    db.delete(job)
    db.commit()
    assert db.scalar(select(func.count()).select_from(PublicationAttempt)) == 0


def test_timestamps_are_stored_as_utc_and_returned_aware(db, now, sample_markdown):
    from app.services import sources

    source = sources.create_pasted_source(
        db, content=sample_markdown, title=None, content_type="text/markdown", now=now
    )
    raw = db.execute(text("SELECT created_at FROM sources WHERE id = :id"), {"id": source.id}).scalar_one()
    assert isinstance(raw, str)
    assert db.get(Source, source.id).created_at.tzinfo == timezone.utc


def test_naive_datetimes_are_rejected_by_the_type(db, sample_markdown):
    from app.services import sources

    with pytest.raises(StatementError) as excinfo:
        sources.create_pasted_source(
            db,
            content=sample_markdown,
            title=None,
            content_type="text/markdown",
            now=datetime(2026, 11, 1, 9, 0, 0),  # no tzinfo
        )
    assert isinstance(excinfo.value.orig, ValueError)
    assert "Naive datetime rejected" in str(excinfo.value.orig)
    db.rollback()


def test_a_second_connection_sees_the_same_committed_state(settings, engine, db, now, sample_markdown):
    from app.services import sources

    source = sources.create_pasted_source(
        db, content=sample_markdown, title=None, content_type="text/markdown", now=now
    )

    other = create_engine(settings.database_url, future=True)
    try:
        with other.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM sources")).scalar_one()
            identifier = connection.execute(text("SELECT id FROM sources")).scalar_one()
        assert count == 1
        assert identifier == source.id
    finally:
        other.dispose()


def test_history_is_append_only(db, now, sample_markdown, settings, registry):
    """Every attempt is its own row; nothing is overwritten."""
    from app.scheduler.worker import run_due_jobs
    from app.services import review, scheduling, sources, variants

    source = sources.create_pasted_source(
        db, content=sample_markdown, title=None, content_type="text/markdown", now=now
    )
    variant = variants.generate_variants(db, source, ["x"], now=now)[0]
    review.approve_variant(db, variant, now=now)
    scheduling.schedule_variant(
        db, variant, scheduled_for=now - timedelta(seconds=1), settings=settings, now=now
    )

    registry.get("x").fail_mode = "retryable"
    run_due_jobs(db, registry, settings, now=now)
    registry.get("x").fail_mode = None
    run_due_jobs(db, registry, settings, now=now + timedelta(seconds=settings.retry_base_seconds + 1))

    attempts = db.execute(select(PublicationAttempt).order_by(PublicationAttempt.attempt_number)).scalars().all()
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert attempts[0].success is False and attempts[0].retryable is True
    assert attempts[1].success is True


def test_engine_factory_enables_foreign_keys(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'fk.db'}")
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    finally:
        engine.dispose()


def test_scheduled_jobs_are_not_held_in_memory(db, now, sample_markdown, settings):
    from app.services import review, scheduling, sources, variants

    source = sources.create_pasted_source(
        db, content=sample_markdown, title=None, content_type="text/markdown", now=now
    )
    variant = variants.generate_variants(db, source, ["x"], now=now)[0]
    review.approve_variant(db, variant, now=now)
    scheduling.schedule_variant(
        db, variant, scheduled_for=now + timedelta(hours=1), settings=settings, now=now
    )
    db.commit()
    db.expire_all()

    stored = db.execute(select(ScheduledJob)).scalars().one()
    assert stored.status == "pending"
    assert stored.scheduled_for.tzinfo == timezone.utc
