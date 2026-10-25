"""Configuration validation and the safe-to-expose summary."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from tests.conftest import make_settings


def test_defaults_run_without_telegram_credentials(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.telegram_configured() is False
    assert settings.public_summary()["telegram"] == {
        "enabled": False,
        "configured": False,
        "dry_run": False,
    }


def test_telegram_enabled_without_credentials_fails_fast(tmp_path):
    with pytest.raises(ValidationError) as excinfo:
        make_settings(tmp_path, telegram_enabled=True)
    message = str(excinfo.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "TELEGRAM_CHAT_ID" in message


def test_telegram_enabled_with_credentials_is_configured(tmp_path):
    settings = make_settings(
        tmp_path, telegram_enabled=True, telegram_bot_token="123456789:AAbbccdd", telegram_chat_id="-100"
    )
    assert settings.telegram_configured() is True


def test_retry_window_must_be_ordered(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, retry_base_seconds=100, retry_max_seconds=10)


def test_short_ops_token_is_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, ops_token="short")


def test_public_summary_never_contains_secrets(tmp_path):
    settings = make_settings(
        tmp_path,
        telegram_enabled=True,
        telegram_bot_token="987654321:AAsecretsecretsecret",
        telegram_chat_id="-1001234567890",
        ops_token="super-secret-ops-token",
    )
    summary = settings.public_summary()
    rendered = repr(summary)
    assert "AAsecretsecretsecret" not in rendered
    assert "super-secret-ops-token" not in rendered
    assert "987654321" not in rendered
    assert summary["ops_endpoint_enabled"] is True
    assert summary["telegram"]["configured"] is True


def test_invalid_combination_reports_no_secret_values(tmp_path):
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'x.db'}",
            telegram_enabled=True,
            telegram_bot_token="",
            telegram_chat_id="-100",
        )
    # The variable name is named; the value is not echoed.
    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)
