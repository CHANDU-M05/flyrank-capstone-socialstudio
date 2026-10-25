"""Adapter registry.

The application resolves a publisher by platform name through this registry. Replacing or
adding an adapter is a registry change — the scheduler and services are untouched.
"""
from __future__ import annotations

import httpx

from app.config import Settings
from app.publishers.base import PublisherStatus, SocialPublisher
from app.publishers.mock import MockLinkedInPublisher, MockXPublisher
from app.publishers.telegram import TelegramPublisher


class PublisherRegistry:
    """Platform name → adapter."""

    def __init__(self, publishers: dict[str, SocialPublisher]):
        self._publishers = dict(publishers)

    def get(self, platform: str) -> SocialPublisher:
        try:
            return self._publishers[platform]
        except KeyError:  # pragma: no cover - guarded by profile/schema
            raise LookupError(f"No publisher registered for platform '{platform}'") from None

    def register(self, publisher: SocialPublisher) -> None:
        self._publishers[publisher.platform] = publisher

    def platforms(self) -> list[str]:
        return sorted(self._publishers)

    def statuses(self) -> list[PublisherStatus]:
        return [self._publishers[platform].status() for platform in self.platforms()]


def build_registry(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
    mock_fail_modes: dict[str, str | None] | None = None,
    overrides: dict[str, SocialPublisher] | None = None,
) -> PublisherRegistry:
    """Wire the default adapters. ``overrides`` exists so tests can swap one adapter."""
    modes = mock_fail_modes or {}
    publishers: dict[str, SocialPublisher] = {
        "x": MockXPublisher(fail_mode=modes.get("x")),
        "linkedin": MockLinkedInPublisher(fail_mode=modes.get("linkedin")),
        "telegram": TelegramPublisher(settings, transport=transport),
    }
    if overrides:
        publishers.update(overrides)
    return PublisherRegistry(publishers)
