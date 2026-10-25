"""Application factory.

``create_app`` takes its collaborators as arguments so tests can build an app over a
temporary database and a fake registry. The module-level ``app`` is what uvicorn imports.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import create_engine, inspect

from app import __version__
from app.api.errors import install_error_handlers
from app.api.routes import ops_router, router
from app.config import Settings, get_settings
from app.publishers.registry import PublisherRegistry, build_registry

logger = logging.getLogger(__name__)

DESCRIPTION = """
Generate platform-specific social copy from one stored source, review and approve it, then
schedule a durable publication.

* **Sources** hold the canonical content — pasted Markdown or a URL fetched through an
  SSRF-guarded client.
* **Variants** are per-platform drafts with deterministic validation against a constraint
  profile.
* **Jobs** are durable rows: the database is the scheduler, and a separate worker publishes.
* **Publication attempts** are append-only history with secrets redacted.

Operational endpoints under `/ops` require `X-Ops-Token` and are disabled unless `OPS_TOKEN`
is configured.
"""


def _ensure_logging(level: str) -> None:
    """Give application loggers a handler when nothing else has.

    uvicorn configures only its own loggers (``uvicorn.*``, ``propagate=False``), so without
    this the startup banner and the "schema is missing, run alembic upgrade head" warning are
    logged into the void. Existing configuration (pytest, a host app) is never touched.
    """
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def _schema_state(database_url: str) -> str:
    """Cheap readiness probe used only at startup, so a missing migration is obvious."""
    try:
        engine = create_engine(database_url, future=True)
        try:
            names = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001 - startup must not crash on a probe
        return "unreachable"
    if {"sources", "variants", "scheduled_jobs", "publication_attempts"} <= names:
        return "ready"
    return "schema_missing"


def create_app(
    settings: Settings | None = None, registry: PublisherRegistry | None = None
) -> FastAPI:
    resolved_settings = settings or get_settings()
    _ensure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.settings = resolved_settings
        application.state.registry = registry or build_registry(resolved_settings)
        logger.info("%s %s starting", resolved_settings.app_name, __version__)
        logger.info("configuration: %s", resolved_settings.public_summary())
        state = _schema_state(resolved_settings.database_url)
        if state == "schema_missing":
            logger.warning(
                "database schema is missing: run `alembic upgrade head` before using the API"
            )
        elif state == "unreachable":
            logger.warning("database is not reachable at startup; /health will report it")
        yield
        logger.info("%s stopping", resolved_settings.app_name)

    application = FastAPI(
        title=resolved_settings.app_name,
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "meta", "description": "Health and non-secret configuration."},
            {"name": "sources", "description": "Canonical content: pasted text or fetched URL."},
            {"name": "variants", "description": "Per-platform drafts, editing and validation."},
            {"name": "review", "description": "Approve, reject and reopen a variant."},
            {"name": "scheduling", "description": "Durable scheduled jobs."},
            {"name": "history", "description": "Append-only publication attempts."},
            {"name": "publishers", "description": "Adapter capabilities and configuration."},
            {"name": "ops", "description": "Token-protected operational controls."},
        ],
    )
    # Set both eagerly and in lifespan: middleware and dependencies may run before startup
    # completes under TestClient, and tests build the app with explicit collaborators.
    application.state.settings = resolved_settings
    application.state.registry = registry or build_registry(resolved_settings)

    install_error_handlers(application)
    application.include_router(router)
    application.include_router(ops_router)

    @application.get("/", tags=["meta"], summary="Service banner")
    def index() -> dict:
        return {
            "name": resolved_settings.app_name,
            "version": __version__,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/health",
        }

    return application


app = create_app()
