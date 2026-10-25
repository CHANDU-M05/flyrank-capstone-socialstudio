"""Deterministic mock publishers.

No network, no accounts, no cost. Behaviour is observable: every accepted delivery is
recorded on the instance, and a repeated idempotency key returns the *same* external id
instead of creating a second post.

The registry is in-process and therefore not durable — the durable duplicate guarantee is
the unique ``scheduled_jobs.idempotency_key`` in the database. This limitation is documented
in README/DESIGN rather than papered over.
"""
from __future__ import annotations

import hashlib

from app.publishers.base import (
    PublisherCapabilities,
    PublisherNotConfigured,
    PublisherPermanentError,
    PublisherRetryableError,
    PublisherStatus,
    PublishRequest,
    PublishResult,
)
from app.services.profiles import get_profile


class MockPublisher:
    """Shared implementation for the two mock platforms."""

    name = "mock"
    platform = "unknown"
    fail_mode: str | None = None

    def __init__(self, *, fail_mode: str | None = None, retry_after_seconds: int | None = None):
        self.fail_mode = fail_mode
        self.retry_after_seconds = retry_after_seconds
        self._delivered: dict[str, str] = {}
        self.deliveries: list[PublishResult] = []

    # -- contract ---------------------------------------------------------
    def capabilities(self) -> PublisherCapabilities:
        profile = get_profile(self.platform)
        return PublisherCapabilities(
            max_chars=profile.max_chars,
            supports_markdown=profile.allow_markdown,
            deterministic=True,
            dry_run_capable=True,
        )

    def status(self) -> PublisherStatus:
        return PublisherStatus(
            name=self.name,
            platform=self.platform,
            configured=True,
            enabled=True,
            detail="Deterministic local mock; no network calls are made.",
            capabilities=self.capabilities().as_dict(),
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        if self.fail_mode == "retryable":
            raise PublisherRetryableError(
                "simulated transient failure from mock platform",
                retry_after_seconds=self.retry_after_seconds,
            )
        if self.fail_mode == "permanent":
            raise PublisherPermanentError("simulated permanent rejection from mock platform")
        if self.fail_mode == "not_configured":
            raise PublisherNotConfigured("simulated unconfigured publisher")

        existing = self._delivered.get(request.idempotency_key)
        if existing is not None:
            return PublishResult(external_id=existing, detail="deduplicated: already delivered")

        digest = hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:12]
        external_id = f"{self.name}-{digest}"
        self._delivered[request.idempotency_key] = external_id
        result = PublishResult(external_id=external_id, detail="published to deterministic mock")
        self.deliveries.append(result)
        return result

    # -- observability ----------------------------------------------------
    @property
    def delivered_count(self) -> int:
        return len(self._delivered)


class MockXPublisher(MockPublisher):
    name = "mock_x"
    platform = "x"


class MockLinkedInPublisher(MockPublisher):
    name = "mock_linkedin"
    platform = "linkedin"
