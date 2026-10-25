"""Source persistence: pasted text and SSRF-guarded URL ingestion.

A source is the canonical copy of the content. Variants reference it and never duplicate it,
so regeneration after an edit to the source stays possible.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.entities import Source
from app.services import ingest
from app.services.errors import NotFoundError, ValidationFailed


def create_pasted_source(
    db: Session,
    *,
    content: str,
    title: str | None,
    content_type: str,
    now: datetime,
) -> Source:
    """Persist pasted Markdown or plain text verbatim."""
    if not content.strip():
        raise ValidationFailed("content must not be empty", code="empty_source")
    source = Source(
        source_type="paste",
        origin_url=None,
        title=(title.strip() if title else None),
        content=content,
        content_type=content_type,
        created_at=now,
        updated_at=now,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def create_url_source(
    db: Session,
    *,
    url: str,
    title: str | None,
    settings: Settings,
    now: datetime,
    resolver: ingest.Resolver | None = None,
    transport=None,
) -> Source:
    """Fetch, extract and persist. Nothing is stored unless the fetch genuinely succeeded."""
    fetched = ingest.fetch(url, settings, resolver=resolver, transport=transport)
    source = Source(
        source_type="url",
        origin_url=fetched.url,
        title=(title.strip() if title and title.strip() else fetched.title),
        content=fetched.content,
        content_type=fetched.content_type,
        created_at=now,
        updated_at=now,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def get_source(db: Session, source_id: str) -> Source:
    source = db.get(Source, source_id)
    if source is None:
        raise NotFoundError(f"Source '{source_id}' was not found.", code="source_not_found")
    return source


def list_sources(db: Session, *, limit: int, offset: int) -> tuple[list[Source], int]:
    total = db.scalar(select(func.count()).select_from(Source)) or 0
    rows = (
        db.execute(
            select(Source)
            .order_by(Source.created_at.desc(), Source.id)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)
