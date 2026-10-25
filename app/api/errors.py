"""HTTP error envelope.

Every failure leaves the API in the same shape::

    {"error": {"code": "...", "message": "...", "details": ...}}

``details`` is only populated when it is safe and useful (field errors, allowed
transitions). Internal tracebacks never reach the client; they are logged instead.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.services.errors import DomainError

logger = logging.getLogger(__name__)


def error_payload(code: str, message: str, details=None) -> dict:
    body: dict = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return {"error": body}


def _json(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_payload(code, message, details))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_request: Request, exc: DomainError) -> JSONResponse:
        return _json(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "location": ".".join(str(part) for part in error.get("loc", ())),
                "message": error.get("msg", "invalid value"),
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()
        ]
        return _json(422, "request_invalid", "The request body or query is invalid.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized"}.get(
            exc.status_code, "http_error"
        )
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return _json(exc.status_code, code, detail)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        # The exception text is intentionally not echoed: it can contain a DSN or a token.
        return _json(500, "internal_error", "An unexpected internal error occurred.")
