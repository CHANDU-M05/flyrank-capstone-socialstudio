"""The ``SocialPublisher`` contract every adapter implements.

Business logic depends on this module only — never on a platform-specific detail — so a new
platform is a new adapter plus a registry entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PublisherCapabilities:
    max_chars: int
    supports_markdown: bool
    deterministic: bool
    dry_run_capable: bool

    def as_dict(self) -> dict:
        return {
            "max_chars": self.max_chars,
            "supports_markdown": self.supports_markdown,
            "deterministic": self.deterministic,
            "dry_run_capable": self.dry_run_capable,
        }


@dataclass(frozen=True)
class PublisherStatus:
    """Safe-to-expose adapter state. Contains no tokens, ever."""

    name: str
    platform: str
    configured: bool
    enabled: bool
    implemented: bool = True
    detail: str | None = None
    capabilities: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PublishRequest:
    text: str
    hashtags: list[str]
    idempotency_key: str
    variant_id: str
    scheduled_for: datetime


@dataclass(frozen=True)
class PublishResult:
    external_id: str | None
    detail: str = "published"


class PublisherError(Exception):
    """Base class for adapter failures. The scheduler maps these to retry policy."""


class PublisherRetryableError(PublisherError):
    """A transient failure: timeouts, 5xx, network errors, rate limiting."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PublisherPermanentError(PublisherError):
    """A failure that will not succeed on retry: bad payload, revoked credentials, 4xx."""


class PublisherNotConfigured(PublisherError):
    """The adapter has no usable credentials. Never silently replaced by a mock."""


class PublisherDryRunAborted(PublisherError):
    """Dry-run mode: the request was built but deliberately not sent."""


@runtime_checkable
class SocialPublisher(Protocol):
    """Everything the application needs from a publishing target."""

    name: str
    platform: str

    def capabilities(self) -> PublisherCapabilities: ...

    def status(self) -> PublisherStatus: ...

    def publish(self, request: PublishRequest) -> PublishResult: ...
