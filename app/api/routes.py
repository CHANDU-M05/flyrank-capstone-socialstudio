"""HTTP surface for the complete workflow.

Handlers do three things only: read the request, call a service, shape the response. All
business rules (validation, transitions, idempotency, retry policy) live in ``app.services``
and ``app.scheduler`` so they are testable without a web client.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select, text

from app import __version__
from app.api.deps import AppSettings, DbSession, OpsAuth, Paging, Registry, utcnow
from app.models.entities import PublicationAttempt, ScheduledJob, Source, Variant
from app.publishers.base import PublisherStatus
from app.scheduler.worker import run_due_jobs
from app.schemas.api import (
    AttemptListOut,
    AttemptOut,
    HealthResponse,
    JobListOut,
    JobOut,
    PublishersResponse,
    PublisherStatusOut,
    ReviewRequest,
    ScheduleRequest,
    ScheduleResponse,
    SourceCreateRequest,
    SourceDetailOut,
    SourceListOut,
    SourceOut,
    UrlIngestRequest,
    ValidationOut,
    VariantGenerateRequest,
    VariantListOut,
    VariantOut,
    VariantUpdateRequest,
    WorkerRunRequest,
    WorkerRunResponse,
)
from app.services import publishing, review, scheduling, sources, variants
from app.services.variants import apply_validation

router = APIRouter()
ops_router = APIRouter()


# --------------------------------------------------------------------------- shaping
def _source_out(source: Source) -> SourceOut:
    return SourceOut(
        id=source.id,
        source_type=source.source_type,
        origin_url=source.origin_url,
        title=source.title,
        content_type=source.content_type,
        content_chars=len(source.content or ""),
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _source_detail(source: Source) -> SourceDetailOut:
    return SourceDetailOut(**_source_out(source).model_dump(), content=source.content)


def _variant_out(variant: Variant) -> VariantOut:
    return VariantOut(
        id=variant.id,
        source_id=variant.source_id,
        platform=variant.platform,
        text=variant.text,
        hashtags=list(variant.hashtags or []),
        validation=dict(variant.validation or {}),
        status=variant.status,
        created_at=variant.created_at,
        updated_at=variant.updated_at,
        status_changed_at=variant.status_changed_at,
    )


def _job_out(job: ScheduledJob) -> JobOut:
    return JobOut(
        id=job.id,
        variant_id=job.variant_id,
        platform=job.platform,
        status=job.status,
        scheduled_for=job.scheduled_for,
        next_attempt_at=job.next_attempt_at,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        last_error=job.last_error,
        idempotency_key=job.idempotency_key,
        external_id=job.external_id,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
    )


def _attempt_out(attempt: PublicationAttempt, scheduled_for=None) -> AttemptOut:
    return AttemptOut(
        id=attempt.id,
        job_id=attempt.job_id,
        variant_id=attempt.variant_id,
        attempt_number=attempt.attempt_number,
        adapter=attempt.adapter,
        platform=attempt.platform,
        scheduled_for=scheduled_for,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        success=attempt.success,
        retryable=attempt.retryable,
        dry_run=attempt.dry_run,
        external_id=attempt.external_id,
        error=attempt.error,
        retry_after_seconds=attempt.retry_after_seconds,
    )


def _status_out(publisher: PublisherStatus) -> PublisherStatusOut:
    return PublisherStatusOut(
        name=publisher.name,
        platform=publisher.platform,
        configured=publisher.configured,
        enabled=publisher.enabled,
        implemented=publisher.implemented,
        detail=publisher.detail,
        capabilities=publisher.capabilities,
    )


# --------------------------------------------------------------------------- meta
@router.get("/health", response_model=HealthResponse, tags=["meta"], summary="Health/readiness")
def health(db: DbSession) -> HealthResponse:
    """Reports whether the database is reachable *and* whether the schema exists."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health must never raise
        return HealthResponse(status="down", database="unreachable", version=__version__)
    try:
        db.execute(text("SELECT 1 FROM sources LIMIT 1"))
    except Exception:  # noqa: BLE001 - the file exists but was never migrated
        return HealthResponse(status="degraded", database="schema_missing", version=__version__)
    return HealthResponse(status="ok", database="ready", version=__version__)


@router.get("/config", tags=["meta"], summary="Non-secret configuration")
def config_summary(settings: AppSettings) -> dict:
    """The only configuration shape the API exposes: no tokens, no DSN."""
    return settings.public_summary()


@router.get(
    "/publishers",
    response_model=PublishersResponse,
    tags=["publishers"],
    summary="Adapter capabilities and configuration status",
)
def list_publishers(registry: Registry) -> PublishersResponse:
    return PublishersResponse(publishers=[_status_out(item) for item in registry.statuses()])


# --------------------------------------------------------------------------- sources
@router.post(
    "/sources",
    response_model=SourceDetailOut,
    status_code=status.HTTP_201_CREATED,
    tags=["sources"],
    summary="Create a source from pasted Markdown or plain text",
)
def create_source(payload: SourceCreateRequest, db: DbSession) -> SourceDetailOut:
    source = sources.create_pasted_source(
        db,
        content=payload.content,
        title=payload.title,
        content_type=payload.content_type,
        now=utcnow(),
    )
    return _source_detail(source)


@router.post(
    "/sources/ingest",
    response_model=SourceDetailOut,
    status_code=status.HTTP_201_CREATED,
    tags=["sources"],
    summary="Ingest a source from a URL (SSRF-guarded)",
)
def ingest_source(
    payload: UrlIngestRequest, db: DbSession, settings: AppSettings
) -> SourceDetailOut:
    source = sources.create_url_source(
        db,
        url=payload.url,
        title=payload.title,
        settings=settings,
        now=utcnow(),
    )
    return _source_detail(source)


@router.get("/sources", response_model=SourceListOut, tags=["sources"], summary="List sources")
def list_sources(db: DbSession, paging: Paging) -> SourceListOut:
    rows, total = sources.list_sources(db, limit=paging.limit, offset=paging.offset)
    return SourceListOut(
        items=[_source_out(row) for row in rows],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get(
    "/sources/{source_id}",
    response_model=SourceDetailOut,
    tags=["sources"],
    summary="Retrieve a source with its original content",
)
def get_source(source_id: str, db: DbSession) -> SourceDetailOut:
    return _source_detail(sources.get_source(db, source_id))


# --------------------------------------------------------------------------- variants
@router.post(
    "/sources/{source_id}/variants",
    response_model=list[VariantOut],
    status_code=status.HTTP_201_CREATED,
    tags=["variants"],
    summary="Generate platform variants from a stored source",
)
def generate_variants(
    source_id: str, payload: VariantGenerateRequest, db: DbSession
) -> list[VariantOut]:
    source = sources.get_source(db, source_id)
    created = variants.generate_variants(
        db, source, payload.platforms, now=utcnow()
    )
    return [_variant_out(variant) for variant in created]


@router.get("/variants", response_model=VariantListOut, tags=["variants"], summary="List variants")
def list_variants(
    db: DbSession,
    paging: Paging,
    variant_status: str | None = Query(default=None, alias="status"),
    platform: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
) -> VariantListOut:
    rows, total = variants.list_variants(
        db,
        status=variant_status,
        platform=platform,
        source_id=source_id,
        limit=paging.limit,
        offset=paging.offset,
    )
    return VariantListOut(
        items=[_variant_out(row) for row in rows],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get(
    "/variants/{variant_id}", response_model=VariantOut, tags=["variants"], summary="Get a variant"
)
def get_variant(variant_id: str, db: DbSession) -> VariantOut:
    return _variant_out(review.get_variant(db, variant_id))


@router.patch(
    "/variants/{variant_id}",
    response_model=VariantOut,
    tags=["variants"],
    summary="Edit a draft variant and revalidate it",
)
def edit_variant(variant_id: str, payload: VariantUpdateRequest, db: DbSession) -> VariantOut:
    variant = review.get_variant(db, variant_id)
    variants.edit_variant(
        variant, text=payload.text, hashtags=payload.hashtags, now=utcnow()
    )
    db.commit()
    db.refresh(variant)
    return _variant_out(variant)


@router.post(
    "/variants/{variant_id}/validate",
    response_model=ValidationOut,
    tags=["variants"],
    summary="Validate a variant against its platform profile",
)
def validate_variant(variant_id: str, db: DbSession) -> ValidationOut:
    variant = review.get_variant(db, variant_id)
    result = apply_validation(variant)
    db.commit()
    return ValidationOut(
        variant_id=variant.id,
        valid=result.valid,
        errors=list(result.errors),
        warnings=list(result.warnings),
    )


@router.post(
    "/variants/{variant_id}/approve",
    response_model=VariantOut,
    tags=["review"],
    summary="Approve a draft variant (refuses invalid content)",
)
def approve_variant(variant_id: str, payload: ReviewRequest, db: DbSession) -> VariantOut:
    variant = review.get_variant(db, variant_id)
    review.approve_variant(db, variant, now=utcnow(), reason=payload.reason)
    return _variant_out(variant)


@router.post(
    "/variants/{variant_id}/reject",
    response_model=VariantOut,
    tags=["review"],
    summary="Reject a draft variant",
)
def reject_variant(variant_id: str, payload: ReviewRequest, db: DbSession) -> VariantOut:
    variant = review.get_variant(db, variant_id)
    review.reject_variant(db, variant, now=utcnow(), reason=payload.reason)
    return _variant_out(variant)


@router.post(
    "/variants/{variant_id}/reopen",
    response_model=VariantOut,
    tags=["review"],
    summary="Return a rejected or approved variant to draft",
)
def reopen_variant(variant_id: str, db: DbSession) -> VariantOut:
    variant = review.get_variant(db, variant_id)
    review.reopen_variant(db, variant, now=utcnow())
    return _variant_out(variant)


# --------------------------------------------------------------------------- scheduling
@router.post(
    "/variants/{variant_id}/schedule",
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["scheduling"],
    summary="Schedule an approved variant for a UTC slot",
    responses={200: {"description": "Duplicate request: the existing job is returned"}},
)
def schedule_variant(
    variant_id: str,
    payload: ScheduleRequest,
    db: DbSession,
    settings: AppSettings,
    response: Response,
) -> ScheduleResponse:
    variant = review.get_variant(db, variant_id)
    job, deduplicated = scheduling.schedule_variant(
        db, variant, scheduled_for=payload.scheduled_for, settings=settings, now=utcnow()
    )
    if deduplicated:
        response.status_code = status.HTTP_200_OK
    return ScheduleResponse(job=_job_out(job), deduplicated=deduplicated)


@router.get("/jobs", response_model=JobListOut, tags=["scheduling"], summary="List scheduled jobs")
def list_jobs(
    db: DbSession,
    paging: Paging,
    job_status: str | None = Query(default=None, alias="status"),
    variant_id: str | None = Query(default=None),
) -> JobListOut:
    rows, total = scheduling.list_jobs(
        db, status=job_status, variant_id=variant_id, limit=paging.limit, offset=paging.offset
    )
    return JobListOut(
        items=[_job_out(row) for row in rows],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get("/jobs/{job_id}", response_model=JobOut, tags=["scheduling"], summary="Get a job")
def get_job(job_id: str, db: DbSession) -> JobOut:
    return _job_out(scheduling.get_job(db, job_id))


# --------------------------------------------------------------------------- history
@router.get(
    "/publications",
    response_model=AttemptListOut,
    tags=["history"],
    summary="Durable publication history (newest first)",
)
def list_publications(
    db: DbSession,
    paging: Paging,
    variant_id: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
) -> AttemptListOut:
    rows, total = publishing.list_attempts(
        db, variant_id=variant_id, job_id=job_id, limit=paging.limit, offset=paging.offset
    )
    job_ids = {row.job_id for row in rows}
    slots = (
        {
            job.id: job.scheduled_for
            for job in db.execute(
                select(ScheduledJob).where(ScheduledJob.id.in_(job_ids))
            ).scalars()
        }
        if job_ids
        else {}
    )
    return AttemptListOut(
        items=[_attempt_out(row, slots.get(row.job_id)) for row in rows],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


# --------------------------------------------------------------------------- ops
@ops_router.post(
    "/ops/run-due",
    response_model=WorkerRunResponse,
    tags=["ops"],
    summary="Run one scheduler tick (requires X-Ops-Token)",
)
def run_due(
    payload: WorkerRunRequest,
    db: DbSession,
    registry: Registry,
    settings: AppSettings,
    _auth: OpsAuth,
) -> WorkerRunResponse:
    """One explicit tick. The worker always re-checks ``next_attempt_at``, so calling this
    early can never publish a job that is not due yet."""
    report = run_due_jobs(db, registry, settings, now=utcnow(), limit=payload.limit)
    jobs = (
        list(
            db.execute(
                select(ScheduledJob).where(ScheduledJob.id.in_(report.job_ids))
            ).scalars()
        )
        if report.job_ids
        else []
    )
    return WorkerRunResponse(
        claimed=report.claimed,
        completed=report.completed,
        retried=report.retried,
        failed=report.failed,
        skipped=report.skipped,
        recovered=report.recovered,
        jobs=[_job_out(job) for job in jobs],
    )
