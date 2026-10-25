"""Domain-level errors.

Services raise these; the API layer translates them into the documented HTTP envelope.
Keeping them out of the web layer means a service can be used by the worker or a script
without dragging FastAPI along.
"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for expected, client-visible failures."""

    status_code = 400
    code = "error"

    def __init__(self, message: str, *, code: str | None = None, details: Any | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class InvalidTransition(DomainError):
    """The requested state change is not allowed from the current status."""

    status_code = 409
    code = "invalid_transition"


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_failed"


class IngestError(DomainError):
    status_code = 422
    code = "ingest_failed"


class PublisherUnavailable(DomainError):
    """No usable adapter for the requested platform.

    Deliberately *not* named ``PublisherNotConfigured``: that name belongs to the adapter
    level error in ``app.publishers.base`` so the two layers can never be confused.
    """

    status_code = 503
    code = "publisher_unavailable"


class OpsUnauthorized(DomainError):
    status_code = 403
    code = "ops_forbidden"
