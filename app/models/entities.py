"""SQLAlchemy 2.x models: sources, variants, scheduled jobs, publication attempts."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.types import UTCDateTime

PLATFORMS: tuple[str, ...] = ("x", "linkedin", "telegram")
SOURCE_TYPES: tuple[str, ...] = ("url", "paste")

#: draft → approved → scheduled → published, plus rejected.
VARIANT_STATUSES: tuple[str, ...] = ("draft", "approved", "rejected", "scheduled", "published")
JOB_STATUSES: tuple[str, ...] = ("pending", "processing", "completed", "failed")


def utcnow() -> datetime:
    """The single source of 'now' for the application."""
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Source(Base):
    """A blog post or pasted text — the canonical source of truth for everything derived."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(48), nullable=False, default="text/markdown")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    variants: Mapped[list["Variant"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )


class Variant(Base):
    """A platform-specific draft derived from exactly one stored source."""

    __tablename__ = "variants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    hashtags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    validation: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    status_changed_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )

    source: Mapped[Source] = relationship(back_populates="variants")
    jobs: Mapped[list["ScheduledJob"]] = relationship(
        back_populates="variant", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_variants_status", "status"),
        Index("ix_variants_source", "source_id"),
        UniqueConstraint("source_id", "platform", name="uq_variants_source_platform"),
    )


class ScheduledJob(Base):
    """Durable scheduling row. The database — not a timer — is the source of truth."""

    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    variant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("variants.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    variant: Mapped[Variant] = relationship(back_populates="jobs")
    attempts_log: Mapped[list["PublicationAttempt"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        Index("ix_jobs_due", "status", "next_attempt_at"),
        Index("ix_jobs_variant", "variant_id"),
    )


class PublicationAttempt(Base):
    """Append-only history of every publish attempt. Never contains secrets."""

    __tablename__ = "publication_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scheduled_jobs.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    adapter: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    job: Mapped[ScheduledJob] = relationship(back_populates="attempts_log")

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_attempt_job_number"),
        Index("ix_attempts_variant", "variant_id", "started_at"),
    )
