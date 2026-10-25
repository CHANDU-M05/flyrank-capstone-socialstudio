"""Central configuration.

Every value comes from the environment (or a local ``.env``). Nothing here is a secret
literal, and ``public_summary()`` is the only shape that may be logged or returned by the
API — it contains booleans and non-sensitive settings, never tokens.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, validated once at startup."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "Social Media Studio"
    environment: str = "development"
    log_level: str = "INFO"

    # SQLite by default so the project runs with no external services.
    database_url: str = "sqlite:///./studio.db"

    # Shared secret for operational endpoints. Empty => /ops is disabled (503).
    ops_token: str = ""

    # --- Telegram (the only real publishing target) -------------------------
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Dry run builds and validates the request but never performs the network call.
    telegram_dry_run: bool = False
    telegram_api_base: str = "https://api.telegram.org"
    telegram_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    telegram_test_override_url: str = ""

    # --- Scheduler / retries ------------------------------------------------
    scheduler_poll_seconds: int = Field(default=5, ge=1, le=300)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: int = Field(default=30, ge=0, le=3600)
    retry_max_seconds: int = Field(default=900, ge=0, le=86400)
    stale_claim_seconds: int = Field(default=900, ge=30, le=86400)

    # --- URL ingestion (SSRF-guarded) --------------------------------------
    ingest_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    ingest_max_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    ingest_max_redirects: int = Field(default=3, ge=0, le=5)

    @model_validator(mode="after")
    def _validate_combinations(self) -> "Settings":
        if self.telegram_enabled:
            missing = [
                name
                for name, value in (
                    ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
                    ("TELEGRAM_CHAT_ID", self.telegram_chat_id),
                )
                if not value.strip()
            ]
            if missing:
                raise ValueError(
                    "TELEGRAM_ENABLED=true requires " + " and ".join(missing) + " to be set."
                )
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("RETRY_MAX_SECONDS must be >= RETRY_BASE_SECONDS.")
        if self.ops_token and len(self.ops_token) < 8:
            raise ValueError("OPS_TOKEN must be at least 8 characters when set.")
        return self

    def telegram_configured(self) -> bool:
        """True only when the adapter could actually call Telegram."""
        return bool(
            self.telegram_enabled
            and self.telegram_bot_token.strip()
            and self.telegram_chat_id.strip()
        )

    def public_summary(self) -> dict:
        """Safe-to-expose configuration. Never includes the token or the ops secret."""
        return {
            "environment": self.environment,
            "database": self.database_url.split("://", 1)[0],
            "scheduler": {
                "max_attempts": self.max_attempts,
                "poll_seconds": self.scheduler_poll_seconds,
                "retry_base_seconds": self.retry_base_seconds,
                "retry_max_seconds": self.retry_max_seconds,
                "stale_claim_seconds": self.stale_claim_seconds,
            },
            "ingest": {
                "timeout_seconds": self.ingest_timeout_seconds,
                "max_bytes": self.ingest_max_bytes,
                "max_redirects": self.ingest_max_redirects,
            },
            "telegram": {
                "enabled": self.telegram_enabled,
                "configured": self.telegram_configured(),
                "dry_run": self.telegram_dry_run,
            },
            "ops_endpoint_enabled": bool(self.ops_token),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
