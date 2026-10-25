"""End-to-end API tests: the documented workflow, the error envelope and the ops guard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import ingest

OPS_HEADERS = {"X-Ops-Token": "test-ops-token"}


def create_source(client, markdown: str) -> dict:
    response = client.post("/sources", json={"content": markdown, "title": "A durable scheduler"})
    assert response.status_code == 201, response.text
    return response.json()


def generate(client, source_id: str, platforms=None) -> list[dict]:
    body = {} if platforms is None else {"platforms": platforms}
    response = client.post(f"/sources/{source_id}/variants", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def approve(client, variant_id: str) -> dict:
    response = client.post(f"/variants/{variant_id}/approve", json={})
    assert response.status_code == 200, response.text
    return response.json()


def schedule(client, variant_id: str, when: datetime):
    return client.post(
        f"/variants/{variant_id}/schedule", json={"scheduled_for": when.isoformat()}
    )


# --------------------------------------------------------------------------- meta
def test_health_reports_a_ready_database(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ready"
    assert body["version"]


def test_openapi_is_served_and_documented(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "Social Media Studio"
    for path in (
        "/sources",
        "/sources/ingest",
        "/sources/{source_id}/variants",
        "/variants/{variant_id}/approve",
        "/variants/{variant_id}/schedule",
        "/jobs",
        "/publications",
        "/publishers",
        "/ops/run-due",
    ):
        assert path in spec["paths"]


def test_config_endpoint_exposes_no_secrets(client):
    body = client.get("/config").json()
    assert "telegram_bot_token" not in str(body)
    assert body["telegram"]["configured"] is False


def test_publishers_endpoint_describes_every_adapter(client):
    body = client.get("/publishers").json()
    platforms = {item["platform"]: item for item in body["publishers"]}
    assert set(platforms) == {"x", "linkedin", "telegram"}
    assert platforms["telegram"]["configured"] is False
    assert platforms["telegram"]["detail"]
    assert platforms["x"]["capabilities"]["deterministic"] is True


# --------------------------------------------------------------------------- sources
def test_pasted_source_roundtrip(client, sample_markdown):
    created = create_source(client, sample_markdown)
    assert created["source_type"] == "paste"
    assert created["origin_url"] is None
    assert created["content"] == sample_markdown
    assert created["content_chars"] == len(sample_markdown)

    fetched = client.get(f"/sources/{created['id']}").json()
    assert fetched["content"] == sample_markdown

    listing = client.get("/sources").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == created["id"]


def test_blank_pasted_content_is_rejected(client):
    response = client.post("/sources", json={"content": "   \n\t "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_invalid"


def test_unknown_field_is_rejected(client):
    response = client.post("/sources", json={"content": "hello world", "unexpected": 1})
    assert response.status_code == 422


def test_unknown_source_is_a_structured_404(client):
    response = client.get("/sources/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "source_not_found"


def test_url_ingestion_rejects_an_internal_target(client):
    response = client.post("/sources/ingest", json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "url_target_not_public"
    assert "169.254.169.254" in body["message"]
    # Nothing was stored.
    assert client.get("/sources").json()["total"] == 0


def test_url_ingestion_rejects_a_non_http_scheme(client):
    response = client.post("/sources/ingest", json={"url": "file:///etc/passwd"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "url_scheme_not_allowed"


def test_url_ingestion_stores_the_fetched_text(client, sample_markdown, monkeypatch):
    fetched = ingest.FetchedDocument(
        url="https://blog.example.com/post",
        content=sample_markdown,
        title="Fetched title",
        content_type="text/markdown",
        size_bytes=len(sample_markdown),
        redirects=0,
    )
    monkeypatch.setattr(ingest, "fetch", lambda *args, **kwargs: fetched)

    response = client.post("/sources/ingest", json={"url": "https://blog.example.com/post"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_type"] == "url"
    assert body["origin_url"] == "https://blog.example.com/post"
    assert body["title"] == "Fetched title"


def test_url_ingestion_failure_is_not_reported_as_success(client, monkeypatch):
    from app.services.errors import IngestError

    def boom(*args, **kwargs):
        raise IngestError("The request timed out after 2.0s.", code="fetch_timeout")

    monkeypatch.setattr(ingest, "fetch", boom)
    response = client.post("/sources/ingest", json={"url": "https://blog.example.com/slow"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "fetch_timeout"
    assert client.get("/sources").json()["total"] == 0


# --------------------------------------------------------------------------- variants
def test_variants_are_generated_for_each_platform(client, sample_markdown):
    source = create_source(client, sample_markdown)
    created = generate(client, source["id"])

    assert {variant["platform"] for variant in created} == {"x", "linkedin", "telegram"}
    for variant in created:
        assert variant["status"] == "draft"
        assert variant["validation"]["valid"] is True
        assert variant["hashtags"]
        assert variant["source_id"] == source["id"]


def test_generating_from_a_missing_source_is_a_404(client):
    response = client.post("/sources/nope/variants", json={})
    assert response.status_code == 404


def test_editing_a_draft_revalidates_and_reports_errors(client, sample_markdown):
    source = create_source(client, sample_markdown)
    variant = generate(client, source["id"], ["x"])[0]

    response = client.patch(f"/variants/{variant['id']}", json={"text": "too short"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "draft"
    assert body["validation"]["valid"] is False
    assert body["validation"]["errors"]


def test_validation_endpoint_matches_approval_rules(client, sample_markdown):
    source = create_source(client, sample_markdown)
    variant = generate(client, source["id"], ["x"])[0]

    client.patch(f"/variants/{variant['id']}", json={"hashtags": ["#one", "#two", "#three", "#four"]})
    validated = client.post(f"/variants/{variant['id']}/validate").json()
    assert validated["valid"] is False

    rejected = client.post(f"/variants/{variant['id']}/approve", json={})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "variant_invalid"


def test_variant_listing_filters(client, sample_markdown):
    source = create_source(client, sample_markdown)
    generate(client, source["id"])

    assert client.get("/variants?platform=x").json()["total"] == 1
    assert client.get("/variants?status=draft").json()["total"] == 3
    assert client.get("/variants?status=approved").json()["total"] == 0
    assert client.get(f"/variants?source_id={source['id']}").json()["total"] == 3


# --------------------------------------------------------------------------- review
def test_review_workflow_over_http(client, sample_markdown):
    source = create_source(client, sample_markdown)
    variant = generate(client, source["id"], ["x"])[0]

    approved = approve(client, variant["id"])
    assert approved["status"] == "approved"

    second = client.post(f"/variants/{variant['id']}/approve", json={})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_transition"

    reopened = client.post(f"/variants/{variant['id']}/reopen", json={})
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "draft"

    rejected = client.post(f"/variants/{variant['id']}/reject", json={"reason": "off tone"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert any("off tone" in item for item in rejected.json()["validation"]["warnings"])

    back = client.post(f"/variants/{variant['id']}/reopen", json={})
    assert back.json()["status"] == "draft"


def test_published_variant_cannot_be_reopened(client, sample_markdown):
    source = create_source(client, sample_markdown)
    variant = generate(client, source["id"], ["x"])[0]
    approve(client, variant["id"])
    schedule(client, variant["id"], datetime.now(timezone.utc) - timedelta(seconds=5))
    assert client.post("/ops/run-due", json={"limit": 5}, headers=OPS_HEADERS).json()["completed"] == 1

    response = client.post(f"/variants/{variant['id']}/reopen", json={})
    assert response.status_code == 409
    assert response.json()["error"]["details"]["from"] == "published"


# --------------------------------------------------------------------------- scheduling
def test_unapproved_variant_cannot_be_scheduled_over_http(client, sample_markdown):
    source = create_source(client, sample_markdown)
    variant = generate(client, source["id"], ["x"])[0]

    response = schedule(client, variant["id"], datetime.now(timezone.utc) + timedelta(minutes=5))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "variant_not_approved"
    assert client.get("/jobs").json()["total"] == 0


def test_naive_schedule_time_is_rejected(client, sample_markdown):
    source = create_source(client, sample_markdown)
    variant = generate(client, source["id"], ["x"])[0]
    approve(client, variant["id"])

    response = client.post(
        f"/variants/{variant['id']}/schedule", json={"scheduled_for": "2026-11-01T09:00:00"}
    )
    assert response.status_code == 422
    assert client.get("/jobs").json()["total"] == 0


def test_duplicate_schedule_returns_200_with_the_same_job(client, sample_markdown):
    source = create_source(client, sample_markdown)
    variant = generate(client, source["id"], ["x"])[0]
    approve(client, variant["id"])
    slot = datetime.now(timezone.utc) + timedelta(hours=1)

    first = schedule(client, variant["id"], slot)
    assert first.status_code == 201
    assert first.json()["deduplicated"] is False

    second = schedule(client, variant["id"], slot)
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["job"]["id"] == first.json()["job"]["id"]
    assert client.get("/jobs").json()["total"] == 1


def test_full_publish_workflow_over_http(client, sample_markdown):
    source = create_source(client, sample_markdown)
    variant = generate(client, source["id"], ["x"])[0]
    approve(client, variant["id"])

    scheduled = schedule(client, variant["id"], datetime.now(timezone.utc) - timedelta(seconds=5))
    assert scheduled.status_code == 201
    job = scheduled.json()["job"]
    assert job["status"] == "pending"

    run = client.post("/ops/run-due", json={"limit": 10}, headers=OPS_HEADERS)
    assert run.status_code == 200
    report = run.json()
    assert report["claimed"] == 1
    assert report["completed"] == 1

    finished_job = client.get(f"/jobs/{job['id']}").json()
    assert finished_job["status"] == "completed"
    assert finished_job["external_id"]
    assert finished_job["attempts"] == 1

    published_variant = client.get(f"/variants/{variant['id']}").json()
    assert published_variant["status"] == "published"

    history = client.get(f"/publications?variant_id={variant['id']}").json()
    assert history["total"] == 1
    attempt = history["items"][0]
    assert attempt["success"] is True
    assert attempt["adapter"] == "mock_x"
    assert attempt["external_id"] == finished_job["external_id"]
    assert attempt["scheduled_for"] is not None
    assert attempt["attempt_number"] == 1

    # Running again publishes nothing.
    again = client.post("/ops/run-due", json={"limit": 10}, headers=OPS_HEADERS).json()
    assert again["claimed"] == 0
    assert client.get("/publications").json()["total"] == 1


def test_failed_publish_is_visible_in_history(client, sample_markdown, registry):
    source = create_source(client, sample_markdown)
    variant = generate(client, source["id"], ["telegram"])[0]
    approve(client, variant["id"])
    schedule(client, variant["id"], datetime.now(timezone.utc) - timedelta(seconds=5))

    report = client.post("/ops/run-due", json={"limit": 10}, headers=OPS_HEADERS).json()
    assert report["failed"] == 1

    history = client.get(f"/publications?variant_id={variant['id']}").json()
    attempt = history["items"][0]
    assert attempt["success"] is False
    assert attempt["platform"] == "telegram"
    assert "not configured" in attempt["error"].lower()
    assert client.get(f"/variants/{variant['id']}").json()["status"] == "approved"


# --------------------------------------------------------------------------- ops guard
def test_ops_endpoint_is_forbidden_without_a_token(client):
    assert client.post("/ops/run-due", json={}).status_code == 403


def test_ops_endpoint_is_forbidden_with_a_wrong_token(client):
    response = client.post("/ops/run-due", json={}, headers={"X-Ops-Token": "nope"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ops_forbidden"


def test_ops_endpoint_accepts_a_bearer_token(client):
    response = client.post(
        "/ops/run-due", json={}, headers={"Authorization": "Bearer test-ops-token"}
    )
    assert response.status_code == 200


def test_ops_endpoint_is_disabled_when_no_token_is_configured(tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.db.session import get_db, make_engine
    from app.main import create_app
    from app.models.entities import Base
    from app.publishers.registry import build_registry
    from tests.conftest import make_settings

    settings = make_settings(tmp_path, ops_token="")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    application = create_app(settings, build_registry(settings))
    application.dependency_overrides[get_db] = lambda: session_factory()
    with TestClient(application) as client:
        response = client.post("/ops/run-due", json={}, headers={"X-Ops-Token": "anything"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ops_disabled"
    engine.dispose()


# --------------------------------------------------------------------------- pagination
def test_pagination_bounds_are_enforced(client):
    assert client.get("/sources?limit=0").status_code == 422
    assert client.get("/sources?limit=500").status_code == 422
    listing = client.get("/sources?limit=5&offset=0").json()
    assert listing["limit"] == 5
    assert listing["offset"] == 0


@pytest.mark.parametrize("method,path", [("get", "/nope"), ("post", "/sources")])
def test_unknown_routes_and_methods_use_the_same_envelope(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code in {404, 405, 422}
    assert "error" in response.json()
