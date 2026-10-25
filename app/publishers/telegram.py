"""Real Telegram adapter (official Bot API).

Rules enforced here:

* the token comes from configuration only and never appears in a message, log or stored error;
* an unconfigured adapter raises ``PublisherNotConfigured`` — it is never swapped for a mock;
* ``TELEGRAM_DRY_RUN=true`` builds and validates the request, then aborts *before* the
  network call, so a dry run can never claim a delivery that did not happen;
* 429 / 5xx / timeouts are retryable and carry ``Retry-After`` when Telegram provides it;
  4xx responses are permanent.
"""
from __future__ import annotations

import httpx

from app.config import Settings
from app.publishers.base import (
    PublisherCapabilities,
    PublisherDryRunAborted,
    PublisherNotConfigured,
    PublisherPermanentError,
    PublisherRetryableError,
    PublisherStatus,
    PublishRequest,
    PublishResult,
)
from app.services.profiles import get_profile
from app.services.sanitize import redact


def _retry_after_seconds(response: httpx.Response) -> int | None:
    """Telegram reports the delay in ``parameters.retry_after``; fall back to the header."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        parameters = payload.get("parameters")
        if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int):
            return max(0, parameters["retry_after"])
    header = response.headers.get("retry-after")
    if header and header.strip().isdigit():
        return int(header.strip())
    return None


class TelegramPublisher:
    """Sends to a chat/channel via the official Bot API."""

    name = "telegram"
    platform = "telegram"

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._transport = transport

    # -- contract ---------------------------------------------------------
    def capabilities(self) -> PublisherCapabilities:
        profile = get_profile(self.platform)
        return PublisherCapabilities(
            max_chars=profile.max_chars,
            supports_markdown=profile.allow_markdown,
            deterministic=False,
            dry_run_capable=True,
        )

    def status(self) -> PublisherStatus:
        settings = self._settings
        configured = settings.telegram_configured()
        if not settings.telegram_enabled:
            detail = "Disabled: TELEGRAM_ENABLED is false, so no message can be sent."
        elif not configured:
            detail = (
                "Enabled but not configured: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID are "
                "missing. Publishing will fail clearly instead of falling back to a mock."
            )
        elif settings.telegram_dry_run:
            detail = (
                "Configured, but TELEGRAM_DRY_RUN=true: requests are validated and aborted "
                "before the network call, so nothing is delivered."
            )
        else:
            detail = "Configured: real Telegram delivery is enabled."
        return PublisherStatus(
            name=self.name,
            platform=self.platform,
            configured=configured,
            enabled=settings.telegram_enabled,
            detail=detail,
            capabilities=self.capabilities().as_dict(),
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        settings = self._settings
        if not settings.telegram_configured():
            raise PublisherNotConfigured(
                "Telegram adapter is not configured (TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, "
                "TELEGRAM_CHAT_ID). Nothing was sent."
            )

        endpoint = (
            f"{settings.telegram_api_base.rstrip('/')}/bot"
            f"{settings.telegram_bot_token}/sendMessage"
        )
        payload = {
            "chat_id": settings.telegram_chat_id,
            "text": request.text,
            "disable_web_page_preview": False,
        }

        if settings.telegram_dry_run:
            # Request is fully built above; abort deliberately. Never report success.
            raise PublisherDryRunAborted(
                "TELEGRAM_DRY_RUN=true: request validated and aborted before sending; "
                "no message was delivered."
            )

        try:
            with httpx.Client(
                transport=self._transport, timeout=settings.telegram_timeout_seconds
            ) as client:
                response = client.post(endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise PublisherRetryableError(
                f"Telegram request timed out after {settings.telegram_timeout_seconds}s: "
                f"{redact(str(exc))}"
            ) from None
        except httpx.HTTPError as exc:
            raise PublisherRetryableError(f"Telegram transport error: {redact(str(exc))}") from None

        if response.status_code == 429:
            raise PublisherRetryableError(
                "Telegram rate limit reached (HTTP 429).",
                retry_after_seconds=_retry_after_seconds(response),
            )
        if response.status_code >= 500:
            raise PublisherRetryableError(
                f"Telegram server error (HTTP {response.status_code}).",
                retry_after_seconds=_retry_after_seconds(response),
            )

        try:
            data = response.json()
        except ValueError:
            data = {}

        if response.status_code >= 400 or not data.get("ok", False):
            description = redact(
                str(data.get("description") or f"HTTP {response.status_code}"),
                secrets=(settings.telegram_bot_token,),
            )
            raise PublisherPermanentError(f"Telegram rejected the request: {description}")

        result = data.get("result") or {}
        message_id = result.get("message_id")
        return PublishResult(
            external_id=str(message_id) if message_id is not None else None,
            detail="sent to Telegram",
        )
