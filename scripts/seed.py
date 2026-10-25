"""Seed one source, three variants and an approved Telegram variant.

Idempotent: running it twice does not duplicate sources or variants. Nothing is published.

Usage::

    python scripts/seed.py
    python scripts/seed.py --publish-now     # also schedules a due job (no delivery)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.entities import Source  # noqa: E402
from app.services import review, scheduling, sources, variants  # noqa: E402
from app.services.profiles import compose_full_text  # noqa: E402

SAMPLE_TITLE = "Seeded: how a durable scheduler works"
SAMPLE_MARKDOWN = """# How a durable scheduler works

A timer lives inside one process; a row lives in the database. When publishing has to survive a
restart, the database has to own the schedule. This seeded document exists so the API, the
reviewer flow and the worker can be exercised without inventing content.

## Same clock everywhere

Every timestamp is UTC. Slots are truncated to whole seconds, which is also the granularity of
the idempotency key, so a retried request lands on the same job instead of creating a second one.

## Failures are expected

Transient failures get exponential backoff. Rate limits get the delay the platform asks for.
Permanent failures stop immediately and are recorded in the append-only history.

## Why seeds matter

A demonstration that starts from an empty database proves very little. This source produces
three platform variants with real validation results, so the review and scheduling steps can be
walked through in under a minute.
"""


def seed(db: Session, *, publish_now: bool) -> int:
    settings = get_settings()
    now = datetime.now(timezone.utc).replace(microsecond=0)

    existing = db.scalar(select(Source).where(Source.title == SAMPLE_TITLE))
    if existing is not None:
        print(f"source already present: {existing.id}")
        source = existing
        created_source = False
    else:
        source = sources.create_pasted_source(
            db,
            content=SAMPLE_MARKDOWN,
            title=SAMPLE_TITLE,
            content_type="text/markdown",
            now=now,
        )
        created_source = True
        print(f"created source: {source.id}")

    generated = variants.generate_variants(db, source, now=now)
    for variant in generated:
        print(
            f"  variant {variant.platform:<9} {variant.status:<9} "
            f"{len(compose_full_text(variant.text, variant.hashtags)):>4} chars "
            f"valid={variant.validation.get('valid')} id={variant.id}"
        )

    approved = 0
    for variant in generated:
        if variant.status == "draft" and variant.validation.get("valid"):
            review.approve_variant(db, variant, now=now)
            approved += 1
    print(f"approved {approved} variant(s)")

    if publish_now:
        target = next(
            (variant for variant in generated if variant.platform == "telegram"),
            None,
        )
        if target is None:
            print("no telegram variant to schedule")
        else:
            if target.status != "approved":
                review.reopen_variant(db, target, now=now)
                review.approve_variant(db, target, now=now)
            job, deduplicated = scheduling.schedule_variant(
                db,
                target,
                scheduled_for=now - timedelta(seconds=1),
                settings=settings,
                now=now,
            )
            state = "already scheduled" if deduplicated else "scheduled"
            print(f"{state} job {job.id} for {job.platform} at {job.scheduled_for}")
            print("run: python scripts/run_worker.py --once")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish-now",
        action="store_true",
        help="schedule the telegram variant so the next worker tick picks it up",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        seed(db, publish_now=args.publish_now)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
