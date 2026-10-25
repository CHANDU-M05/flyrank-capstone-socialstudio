"""Shared fixtures.

Every test gets its own SQLite file, its own session factory and its own application
instance. No test touches the network, Telegram credentials or the developer's database.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.session import get_db, make_engine
from app.main import create_app
from app.models.entities import Base
from app.publishers.registry import PublisherRegistry, build_registry

SAMPLE_MARKDOWN = """# Shipping a Durable Scheduler with SQLite

Most side projects start with a cron job and an in-memory queue. That works until the
process restarts in the middle of a publish. This post walks through replacing timers with
rows in a database, claiming them safely, and retrying failures without duplicating a post.

## Why the database is the scheduler

A timer lives inside one process. A row lives in the database, so a restart, a crash, or a
second worker all see the same truth. The scheduler becomes a query: find pending rows whose
next attempt time has passed, claim one, publish, record the outcome.

## Claiming without a lock service

An optimistic claim is a single update with a status predicate. Exactly one worker sees a
row count of one. Everyone else sees zero and moves on. No distributed lock, no coordination
service, no extra infrastructure.

## Retries that respect the platform

Transient failures get exponential backoff. Rate limits get the delay the platform asks for.
Permanent failures stop immediately, because retrying a rejected payload forever is how you
get blocked. Every attempt is appended to history so an operator can reconstruct what
happened after the fact.
"""


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": f"sqlite:///{tmp_path / 'studio.db'}",
        "ops_token": "test-ops-token",
        "max_attempts": 3,
        "retry_base_seconds": 10,
        "retry_max_seconds": 60,
        "scheduler_poll_seconds": 1,
        "stale_claim_seconds": 60,
        "telegram_enabled": False,
        "ingest_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def engine(settings):
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def db(session_factory) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def registry(settings) -> PublisherRegistry:
    return build_registry(settings)


@pytest.fixture
def app(settings, registry, session_factory):
    application = create_app(settings, registry)

    def _override() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = _override
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_markdown() -> str:
    return SAMPLE_MARKDOWN


@pytest.fixture
def now() -> datetime:
    """A fixed clock: whole seconds, so idempotency keys are stable across a test."""
    return datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def past(now) -> datetime:
    return now - timedelta(seconds=30)
