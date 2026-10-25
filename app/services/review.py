"""Review workflow: the single implementation of the variant state machine.

Transitions (also documented in DESIGN.md §6 and README):

    draft  → approved | rejected
    rejected → draft        (reopen: the content becomes editable again)
    approved → draft        (revise: only possible while nothing was published)
    approved → scheduled    (via the scheduler service, never directly)
    scheduled → published   (worker, on a successful delivery)
    scheduled → approved    (worker, on a terminal failure — a human must act)

Anything else is a 409 ``invalid_transition``. Approval re-runs validation and refuses
invalid content. There is deliberately no override switch: an override would be a
sanctioned way to publish text the platform rejects.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.entities import Variant
from app.services.errors import InvalidTransition, NotFoundError, ValidationFailed
from app.services.profiles import get_profile
from app.services.variants import apply_validation

#: status -> the statuses it may move to through a review action.
REVIEW_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("approved", "rejected"),
    "approved": ("draft",),
    "rejected": ("draft",),
    "scheduled": (),
    "published": (),
}

#: Human-readable reason per target status, used in error messages.
_ACTION_LABELS = {
    "approved": "approve",
    "rejected": "reject",
    "draft": "reopen",
}


def get_variant(db: Session, variant_id: str) -> Variant:
    variant = db.get(Variant, variant_id)
    if variant is None:
        raise NotFoundError(f"Variant '{variant_id}' was not found.", code="variant_not_found")
    return variant


def _ensure_transition(variant: Variant, target: str) -> None:
    allowed = REVIEW_TRANSITIONS.get(variant.status, ())
    if target not in allowed:
        action = _ACTION_LABELS.get(target, target)
        raise InvalidTransition(
            f"Cannot {action} a variant in status '{variant.status}'. "
            f"From '{variant.status}' the allowed review actions are: "
            f"{', '.join(_ACTION_LABELS[status] for status in allowed) or 'none'}.",
            code="invalid_transition",
            details={"from": variant.status, "to": target, "allowed": list(allowed)},
        )


def _apply(db: Session, variant: Variant, status: str, now: datetime) -> Variant:
    variant.status = status
    variant.status_changed_at = now
    variant.updated_at = now
    db.commit()
    db.refresh(variant)
    return variant


def approve_variant(
    db: Session, variant: Variant, *, now: datetime, reason: str | None = None
) -> Variant:
    """Approve a draft. Invalid content is refused — there is no override path."""
    _ensure_transition(variant, "approved")
    result = apply_validation(variant)
    if not result.valid:
        raise ValidationFailed(
            "The variant does not satisfy the "
            f"{get_profile(variant.platform).display_name} profile: " + "; ".join(result.errors),
            code="variant_invalid",
            details={"errors": list(result.errors), "warnings": list(result.warnings)},
        )
    if reason:
        validation = dict(variant.validation or {})
        validation["warnings"] = list(validation.get("warnings", [])) + [
            f"approved: {reason.strip()[:200]}"
        ]
        variant.validation = validation
    return _apply(db, variant, "approved", now)


def reject_variant(
    db: Session, variant: Variant, *, now: datetime, reason: str | None = None
) -> Variant:
    """Reject a draft. The reason is advisory and kept in the response only."""
    _ensure_transition(variant, "rejected")
    if reason:
        warnings = list((variant.validation or {}).get("warnings", []))
        validation = dict(variant.validation or {})
        validation["warnings"] = warnings + [f"rejected: {reason.strip()[:200]}"]
        variant.validation = validation
    return _apply(db, variant, "rejected", now)


def reopen_variant(db: Session, variant: Variant, *, now: datetime) -> Variant:
    """Return a rejected (or approved) variant to ``draft`` so it can be edited."""
    _ensure_transition(variant, "draft")
    apply_validation(variant)
    return _apply(db, variant, "draft", now)


def mark_scheduled(db: Session, variant: Variant, *, now: datetime) -> Variant:
    """Called by the scheduling service inside its own transaction.

    Only ``approved`` may move to ``scheduled``; a variant that is already scheduled,
    rejected or published cannot be queued again.
    """
    if variant.status != "approved":
        raise InvalidTransition(
            f"Cannot schedule a variant in status '{variant.status}'.",
            code="variant_not_approved",
            details={"from": variant.status, "to": "scheduled", "allowed": ["approved"]},
        )
    variant.status = "scheduled"
    variant.status_changed_at = now
    variant.updated_at = now
    return variant


def mark_published(db: Session, variant: Variant, *, now: datetime) -> Variant:
    variant.status = "published"
    variant.status_changed_at = now
    variant.updated_at = now
    return variant


def mark_publish_failed(db: Session, variant: Variant, *, now: datetime) -> Variant:
    """Terminal failure: hand the variant back to a human as ``approved``."""
    variant.status = "approved"
    variant.status_changed_at = now
    variant.updated_at = now
    return variant
