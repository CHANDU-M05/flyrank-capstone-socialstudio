"""Shared FastAPI dependencies.

Settings and the publisher registry are attached to ``app.state`` by the factory, so tests
can build an application with a temporary database and a fake registry without monkeypatching
module-level globals.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.session import get_db
from app.publishers.registry import PublisherRegistry
from app.services.errors import OpsUnauthorized

DbSession = Annotated[Session, Depends(get_db)]


def utcnow() -> datetime:
    """Request-time clock. Tests patch ``app.api.deps.utcnow`` for deterministic slots."""
    return datetime.now(timezone.utc)


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_registry(request: Request) -> PublisherRegistry:
    return request.app.state.registry


AppSettings = Annotated[Settings, Depends(get_app_settings)]
Registry = Annotated[PublisherRegistry, Depends(get_registry)]


class Pagination:
    """Simple limit/offset pagination, bounded so a client cannot ask for everything."""

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=100, description="Page size")] = 20,
        offset: Annotated[int, Query(ge=0, description="Rows to skip")] = 0,
    ):
        self.limit = limit
        self.offset = offset


Paging = Annotated[Pagination, Depends()]


def require_ops_token(
    request: Request,
    x_ops_token: Annotated[str | None, Header(alias="X-Ops-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Guard the operational endpoints.

    Disabled entirely unless ``OPS_TOKEN`` is configured — an unauthenticated scheduler
    endpoint is never exposed "by default".
    """
    settings: Settings = request.app.state.settings
    expected = settings.ops_token
    if not expected:
        raise OpsUnauthorized(
            "Operational endpoints are disabled because OPS_TOKEN is not configured.",
            code="ops_disabled",
        )

    supplied = x_ops_token
    if not supplied and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied = value.strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise OpsUnauthorized("A valid operational token is required.", code="ops_forbidden")


OpsAuth = Annotated[None, Depends(require_ops_token)]
