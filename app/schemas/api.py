"""Pydantic request/response models.

Everything that crosses the API boundary is validated here: unknown fields are rejected
(``extra="forbid"``), strings are stripped, and timestamps must carry an explicit offset so
an ambiguous local time can never be stored as UTC by accident.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SOURCE_CHARS = 200_000
MAX_VARIANT_CHARS = 8_000
MAX_HASHTAGS = 30


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- sources
class SourceCreateRequest(_Strict):
    """Pasted Markdown or plain text."""

    content: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)
    title: str | None = Field(default=None, max_length=300)
    content_type: Literal["text/markdown", "text/plain"] = "text/markdown"

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be empty or whitespace only")
        return value

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class UrlIngestRequest(_Strict):
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=300)


class SourceOut(BaseModel):
    id: str
    source_type: str
    origin_url: str | None
    title: str | None
    content_type: str
    content_chars: int
    created_at: datetime
    updated_at: datetime


class SourceDetailOut(SourceOut):
    content: str


class SourceListOut(BaseModel):
    items: list[SourceOut]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- variants
class VariantGenerateRequest(_Strict):
    platforms: list[Literal["x", "linkedin", "telegram"]] | None = Field(
        default=None, max_length=3
    )


class VariantOut(BaseModel):
    id: str
    source_id: str
    platform: str
    text: str
    hashtags: list[str]
    validation: dict
    status: str
    created_at: datetime
    updated_at: datetime
    status_changed_at: datetime


class VariantListOut(BaseModel):
    items: list[VariantOut]
    total: int
    limit: int
    offset: int


class VariantUpdateRequest(_Strict):
    text: str | None = Field(default=None, min_length=1, max_length=MAX_VARIANT_CHARS)
    hashtags: list[str] | None = Field(default=None, max_length=MAX_HASHTAGS)

    @field_validator("hashtags")
    @classmethod
    def _clean_hashtags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = []
        for tag in value:
            tag = tag.strip()
            if not tag:
                continue
            cleaned.append(tag if tag.startswith("#") else f"#{tag}")
        return cleaned


class ValidationOut(BaseModel):
    variant_id: str
    valid: bool
    errors: list[str]
    warnings: list[str]


class ReviewRequest(_Strict):
    """Body for approve/reject/reopen. The reason is advisory and never required."""

    reason: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- scheduling
class ScheduleRequest(_Strict):
    scheduled_for: datetime

    @field_validator("scheduled_for")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("scheduled_for must include a timezone offset, e.g. 2026-11-01T09:00:00Z")
        return value


class AttemptOut(BaseModel):
    id: str
    job_id: str
    variant_id: str
    attempt_number: int
    adapter: str
    platform: str
    scheduled_for: datetime | None = None
    started_at: datetime
    finished_at: datetime | None
    success: bool
    retryable: bool
    dry_run: bool
    external_id: str | None
    error: str | None
    retry_after_seconds: int | None


class JobOut(BaseModel):
    id: str
    variant_id: str
    platform: str
    status: str
    scheduled_for: datetime
    next_attempt_at: datetime
    attempts: int
    max_attempts: int
    last_error: str | None
    idempotency_key: str
    external_id: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class ScheduleResponse(BaseModel):
    job: JobOut
    deduplicated: bool


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int
    limit: int
    offset: int


class AttemptListOut(BaseModel):
    items: list[AttemptOut]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- misc
class PublisherStatusOut(BaseModel):
    name: str
    platform: str
    configured: bool
    enabled: bool
    implemented: bool
    detail: str | None = None
    capabilities: dict


class PublishersResponse(BaseModel):
    publishers: list[PublisherStatusOut]


class HealthResponse(BaseModel):
    status: str
    database: str
    version: str


class WorkerRunRequest(_Strict):
    limit: int = Field(default=10, ge=1, le=100)


class WorkerRunResponse(BaseModel):
    claimed: int
    completed: int
    retried: int
    failed: int
    skipped: int = 0
    recovered: int
    jobs: list[JobOut]
