"""Review workflow: the state machine, approval gating and rejection handling."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models.entities import ScheduledJob, Variant
from app.services import review, scheduling, sources, variants
from app.services.errors import InvalidTransition, ValidationFailed


def _draft(db, now, markdown, platform="x") -> Variant:
    source = sources.create_pasted_source(
        db, content=markdown, title=None, content_type="text/markdown", now=now
    )
    return variants.generate_variants(db, source, [platform], now=now)[0]


def test_draft_can_be_approved(db, now, sample_markdown):
    variant = _draft(db, now, sample_markdown)
    assert variant.status == "draft"
    review.approve_variant(db, variant, now=now)
    assert variant.status == "approved"


def test_draft_can_be_rejected_then_reopened(db, now, sample_markdown):
    variant = _draft(db, now, sample_markdown)
    review.reject_variant(db, variant, now=now, reason="off tone")
    assert variant.status == "rejected"
    assert any("off tone" in warning for warning in variant.validation["warnings"])

    review.reopen_variant(db, variant, now=now)
    assert variant.status == "draft"


def test_approved_variant_can_be_reopened_for_editing(db, now, sample_markdown):
    variant = _draft(db, now, sample_markdown)
    review.approve_variant(db, variant, now=now)
    review.reopen_variant(db, variant, now=now)
    assert variant.status == "draft"


def test_double_approval_is_a_conflict(db, now, sample_markdown):
    variant = _draft(db, now, sample_markdown)
    review.approve_variant(db, variant, now=now)
    with pytest.raises(InvalidTransition) as excinfo:
        review.approve_variant(db, variant, now=now)
    assert excinfo.value.status_code == 409
    assert excinfo.value.details["allowed"] == ["draft"]


def test_rejecting_an_approved_variant_is_a_conflict(db, now, sample_markdown):
    variant = _draft(db, now, sample_markdown)
    review.approve_variant(db, variant, now=now)
    with pytest.raises(InvalidTransition):
        review.reject_variant(db, variant, now=now)


def test_invalid_variant_cannot_be_approved(db, now):
    source = sources.create_pasted_source(
        db, content="x", title="x", content_type="text/plain", now=now
    )
    variant = Variant(
        source_id=source.id,
        platform="x",
        text="too short",
        hashtags=[],
        validation={},
        status="draft",
        created_at=now,
        updated_at=now,
        status_changed_at=now,
    )
    db.add(variant)
    db.commit()

    with pytest.raises(ValidationFailed) as excinfo:
        review.approve_variant(db, variant, now=now)
    assert excinfo.value.code == "variant_invalid"
    assert variant.status == "draft"


def test_reviewed_variant_cannot_be_rejected_twice(db, now, sample_markdown):
    variant = _draft(db, now, sample_markdown)
    review.reject_variant(db, variant, now=now)
    with pytest.raises(InvalidTransition):
        review.reject_variant(db, variant, now=now)


def test_unapproved_variant_cannot_be_scheduled(db, now, sample_markdown, settings):
    variant = _draft(db, now, sample_markdown)
    with pytest.raises(InvalidTransition) as excinfo:
        scheduling.schedule_variant(db, variant, scheduled_for=now, settings=settings, now=now)

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "variant_not_approved"
    assert db.scalar(select(func.count()).select_from(ScheduledJob)) == 0


def test_rejected_variant_cannot_be_scheduled(db, now, sample_markdown, settings):
    variant = _draft(db, now, sample_markdown)
    review.reject_variant(db, variant, now=now)

    with pytest.raises(InvalidTransition):
        scheduling.schedule_variant(db, variant, scheduled_for=now, settings=settings, now=now)
    assert db.scalar(select(func.count()).select_from(ScheduledJob)) == 0


def test_published_variant_cannot_be_reopened(db, now, sample_markdown):
    variant = _draft(db, now, sample_markdown)
    review.approve_variant(db, variant, now=now)
    review.mark_published(db, variant, now=now)
    db.commit()
    with pytest.raises(InvalidTransition):
        review.reopen_variant(db, variant, now=now)
    with pytest.raises(InvalidTransition):
        review.approve_variant(db, variant, now=now)
