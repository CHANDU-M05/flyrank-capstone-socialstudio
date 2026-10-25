"""Adapters: deterministic mocks, and a Telegram adapter that can never leak or lie."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.publishers.base import (
    PublisherDryRunAborted,
    PublisherNotConfigured,
    PublisherPermanentError,
    PublisherRetryableError,
    PublishRequest,
)
from app.publishers.mock import MockLinkedInPublisher, MockXPublisher
from app.publishers.registry import build_registry
from app.publishers.telegram import TelegramPublisher
from tests.conftest import make_settings

TOKEN = "123456789:AAtesttokenvalue0123456789"
REQUEST = PublishRequest(
    text="Hello from the studio",
    hashtags=["#test"],
    idempotency_key="abc123",
    variant_id="variant-1",
    scheduled_for=datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc),
)


def configured_settings(tmp_path, **overrides):
    return make_settings(
        tmp_path,
        telegram_enabled=True,
        telegram_bot_token=TOKEN,
        telegram_chat_id="-1001234567890",
        **overrides,
    )


class ExplodingTransport(httpx.BaseTransport):
    """Fails the test if anything is actually sent."""

    def __init__(self):
        self.calls = 0

    def handle_request(self, request):  # noqa: ANN001
        self.calls += 1
        raise AssertionError(f"unexpected network call to {request.url}")


# --------------------------------------------------------------------------- mocks
def test_mock_external_id_is_deterministic():
    first = MockXPublisher().publish(REQUEST)
    second = MockXPublisher().publish(REQUEST)
    assert first.external_id == second.external_id
    assert first.external_id.startswith("mock_x-")


def test_mock_rejects_a_second_delivery_with_the_same_idempotency_key():
    publisher = MockXPublisher()
    first = publisher.publish(REQUEST)
    again = publisher.publish(REQUEST)

    assert again.external_id == first.external_id
    assert again.detail == "deduplicated: already delivered"
    assert publisher.delivered_count == 1
    assert len(publisher.deliveries) == 1


def test_mock_platforms_have_distinct_ids():
    x_id = MockXPublisher().publish(REQUEST).external_id
    linkedin_id = MockLinkedInPublisher().publish(REQUEST).external_id
    assert x_id != linkedin_id
    assert linkedin_id.startswith("mock_linkedin-")


@pytest.mark.parametrize(
    "fail_mode,error",
    [
        ("retryable", PublisherRetryableError),
        ("permanent", PublisherPermanentError),
        ("not_configured", PublisherNotConfigured),
    ],
)
def test_mock_failure_modes_raise_the_documented_errors(fail_mode, error):
    publisher = MockXPublisher(fail_mode=fail_mode)
    with pytest.raises(error):
        publisher.publish(REQUEST)
    assert publisher.delivered_count == 0


def test_mock_can_advertise_a_retry_after_delay():
    publisher = MockXPublisher(fail_mode="retryable", retry_after_seconds=42)
    with pytest.raises(PublisherRetryableError) as excinfo:
        publisher.publish(REQUEST)
    assert excinfo.value.retry_after_seconds == 42


def test_mock_status_reports_capabilities():
    status = MockXPublisher().status()
    assert status.configured is True
    assert status.enabled is True
    assert status.capabilities["max_chars"] == 280
    assert status.capabilities["deterministic"] is True


# --------------------------------------------------------------------------- registry
def test_registry_exposes_the_three_adapters(tmp_path):
    registry = build_registry(make_settings(tmp_path))
    assert registry.platforms() == ["linkedin", "telegram", "x"]
    assert {status.name for status in registry.statuses()} == {
        "mock_x",
        "mock_linkedin",
        "telegram",
    }


def test_registry_accepts_an_override(tmp_path):
    custom = MockXPublisher()
    registry = build_registry(make_settings(tmp_path), overrides={"x": custom})
    assert registry.get("x") is custom


# --------------------------------------------------------------------------- telegram
def test_unconfigured_telegram_reports_and_refuses(tmp_path):
    settings = make_settings(tmp_path)
    transport = ExplodingTransport()
    publisher = TelegramPublisher(settings, transport=transport)

    status = publisher.status()
    assert status.configured is False
    assert status.enabled is False
    assert "Disabled" in status.detail

    with pytest.raises(PublisherNotConfigured):
        publisher.publish(REQUEST)
    assert transport.calls == 0


def test_telegram_platform_is_never_served_by_a_mock(tmp_path):
    registry = build_registry(make_settings(tmp_path))
    assert isinstance(registry.get("telegram"), TelegramPublisher)
    assert registry.get("telegram").platform == "telegram"
    # The mock adapters keep their own platforms and cannot be reached as "telegram".
    assert registry.get("x").name == "mock_x"
    assert registry.get("linkedin").name == "mock_linkedin"


def test_dry_run_aborts_before_the_network_call(tmp_path):
    settings = configured_settings(tmp_path, telegram_dry_run=True)
    transport = ExplodingTransport()
    publisher = TelegramPublisher(settings, transport=transport)

    assert "DRY_RUN" in publisher.status().detail
    with pytest.raises(PublisherDryRunAborted):
        publisher.publish(REQUEST)
    assert transport.calls == 0


def test_successful_send_returns_the_message_id(tmp_path):
    settings = configured_settings(tmp_path)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    publisher = TelegramPublisher(settings, transport=httpx.MockTransport(handler))
    result = publisher.publish(REQUEST)

    assert result.external_id == "42"
    assert seen["url"].endswith(f"/bot{TOKEN}/sendMessage")
    assert "-1001234567890" in seen["payload"]


def test_rate_limit_reports_telegrams_retry_after(tmp_path):
    settings = configured_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 7},
            },
        )

    publisher = TelegramPublisher(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(PublisherRetryableError) as excinfo:
        publisher.publish(REQUEST)
    assert excinfo.value.retry_after_seconds == 7


def test_rate_limit_falls_back_to_the_retry_after_header(tmp_path):
    settings = configured_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "13"}, json={"ok": False})

    publisher = TelegramPublisher(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(PublisherRetryableError) as excinfo:
        publisher.publish(REQUEST)
    assert excinfo.value.retry_after_seconds == 13


def test_server_error_is_retryable(tmp_path):
    settings = configured_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    publisher = TelegramPublisher(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(PublisherRetryableError):
        publisher.publish(REQUEST)


def test_client_error_is_permanent_and_redacted(tmp_path):
    settings = configured_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"ok": False, "description": f"Bad Request: token {TOKEN} rejected"}
        )

    publisher = TelegramPublisher(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(PublisherPermanentError) as excinfo:
        publisher.publish(REQUEST)

    message = str(excinfo.value)
    assert TOKEN not in message
    assert "<redacted" in message


def test_timeout_is_retryable(tmp_path):
    settings = configured_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    publisher = TelegramPublisher(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(PublisherRetryableError):
        publisher.publish(REQUEST)


def test_status_never_contains_the_token(tmp_path):
    settings = configured_settings(tmp_path)
    publisher = TelegramPublisher(settings)

    assert publisher.status().configured is True
    assert "delivery is enabled" in publisher.status().detail
    for attribute in ("detail", "name", "platform"):
        assert TOKEN not in str(getattr(publisher.status(), attribute))
