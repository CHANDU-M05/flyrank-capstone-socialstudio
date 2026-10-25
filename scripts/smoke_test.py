"""Live HTTP smoke test against a running server.

Unlike the pytest suite (which uses TestClient and temporary databases), this script talks to a
real uvicorn process over TCP. It is the evidence-producing script: every line it prints is a
real response.

Usage::

    uvicorn app.main:app --port 8020 &
    python scripts/smoke_test.py --base-url http://127.0.0.1:8020 --ops-token "$OPS_TOKEN"

Exit code is 0 only when every check passed.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import httpx

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS  {label}")
    else:
        FAILED += 1
        print(f"FAIL  {label}" + (f"  -> {detail}" if detail else ""))
    return condition


def main() -> int:
    parser = argparse.ArgumentParser(description="Live smoke test for Social Media Studio")
    parser.add_argument("--base-url", default="http://127.0.0.1:8020")
    parser.add_argument("--ops-token", default="smoke-ops-token")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    ops = {"X-Ops-Token": args.ops_token}
    client = httpx.Client(base_url=base, timeout=20.0)

    print(f"target: {base}\n")

    # --- meta -------------------------------------------------------------
    health = client.get("/health")
    check(
        "GET /health is ok",
        health.status_code == 200 and health.json()["database"] == "ready",
        health.text[:200],
    )

    spec = client.get("/openapi.json")
    paths = spec.json().get("paths", {}) if spec.status_code == 200 else {}
    check("OpenAPI documents the workflow", "/variants/{variant_id}/schedule" in paths)

    publishers = client.get("/publishers").json()["publishers"]
    platforms = {item["platform"]: item for item in publishers}
    check("three adapters are registered", set(platforms) == {"x", "linkedin", "telegram"})
    check(
        "telegram reports itself unconfigured rather than pretending",
        platforms["telegram"]["configured"] is False and bool(platforms["telegram"]["detail"]),
        str(platforms["telegram"]),
    )

    config = client.get("/config").json()
    check("GET /config leaks no token", "telegram_bot_token" not in str(config))

    # --- ingestion --------------------------------------------------------
    markdown = (
        "# Smoke test post\n\n"
        "This content is created by scripts/smoke_test.py against a running server. It exists to "
        "prove that ingestion, variant generation, review, scheduling and durable publishing all "
        "work over real HTTP.\n\n"
        "## Why a real server\n\n"
        "TestClient bypasses the network stack. A live probe catches wiring mistakes that unit "
        "tests miss, such as a dependency that is never injected or a route that is not mounted."
    )
    created = client.post("/sources", json={"content": markdown, "title": "Smoke test post"})
    check("POST /sources stores pasted markdown", created.status_code == 201, created.text[:200])
    source = created.json()

    blank = client.post("/sources", json={"content": "    "})
    check("blank paste is rejected with 422", blank.status_code == 422, blank.text[:200])

    scheme = client.post("/sources/ingest", json={"url": "file:///etc/passwd"})
    check(
        "file:// URL is rejected (422 url_scheme_not_allowed)",
        scheme.status_code == 422
        and scheme.json()["error"]["code"] == "url_scheme_not_allowed",
        scheme.text[:200],
    )

    metadata = client.post("/sources/ingest", json={"url": "http://169.254.169.254/latest/meta-data/"})
    check(
        "SSRF to the cloud metadata endpoint is blocked",
        metadata.status_code == 422
        and metadata.json()["error"]["code"] == "url_target_not_public",
        metadata.text[:200],
    )
    check(
        "the blocked URL stored nothing",
        client.get("/sources").json()["total"] == 1,
        str(client.get("/sources").json()["total"]),
    )

    # --- variants ---------------------------------------------------------
    generated = client.post(f"/sources/{source['id']}/variants", json={})
    check(
        "POST /sources/{id}/variants creates three drafts",
        generated.status_code == 201 and len(generated.json()) == 3,
        generated.text[:300],
    )
    variants = {item["platform"]: item for item in generated.json()}
    check(
        "every generated variant passed its own profile",
        all(item["validation"]["valid"] for item in variants.values()),
        str({platform: item["validation"] for platform, item in variants.items()}),
    )
    check(
        "variants differ per platform",
        len({item["text"] for item in variants.values()}) == 3,
    )

    x_variant = variants["x"]
    invalid = client.patch(f"/variants/{x_variant['id']}", json={"text": "too short"})
    check(
        "editing a draft revalidates and reports the error",
        invalid.status_code == 200 and invalid.json()["validation"]["valid"] is False,
        invalid.text[:300],
    )
    refused = client.post(f"/variants/{x_variant['id']}/approve", json={})
    check(
        "invalid variant cannot be approved (422 variant_invalid)",
        refused.status_code == 422 and refused.json()["error"]["code"] == "variant_invalid",
        refused.text[:300],
    )

    # --- review -----------------------------------------------------------
    linkedin = variants["linkedin"]
    approved = client.post(f"/variants/{linkedin['id']}/approve", json={"reason": "looks good"})
    check(
        "valid draft can be approved",
        approved.status_code == 200 and approved.json()["status"] == "approved",
        approved.text[:300],
    )
    twice = client.post(f"/variants/{linkedin['id']}/approve", json={})
    check(
        "second approval is a 409 invalid_transition",
        twice.status_code == 409 and twice.json()["error"]["code"] == "invalid_transition",
        twice.text[:300],
    )
    rejected = client.post(f"/variants/{x_variant['id']}/reject", json={"reason": "smoke"})
    check(
        "draft can be rejected",
        rejected.status_code == 200 and rejected.json()["status"] == "rejected",
        rejected.text[:300],
    )
    reopened = client.post(f"/variants/{x_variant['id']}/reopen", json={})
    check(
        "rejected variant can be reopened for editing",
        reopened.status_code == 200 and reopened.json()["status"] == "draft",
        reopened.text[:300],
    )

    # --- scheduling refuses the unapproved --------------------------------
    jobs_before = client.get("/jobs").json()["total"]
    unapproved = client.post(
        f"/variants/{x_variant['id']}/schedule",
        json={"scheduled_for": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
    )
    jobs_after = client.get("/jobs").json()["total"]
    check(
        "scheduling a draft is refused and creates no job",
        unapproved.status_code == 409 and jobs_before == jobs_after == 0,
        unapproved.text[:300],
    )

    naive = client.post(
        f"/variants/{linkedin['id']}/schedule", json={"scheduled_for": "2026-12-01T10:00:00"}
    )
    check("naive datetime is rejected", naive.status_code == 422, naive.text[:200])

    # --- idempotent scheduling -------------------------------------------
    slot = datetime.now(timezone.utc) - timedelta(seconds=5)
    first = client.post(
        f"/variants/{linkedin['id']}/schedule", json={"scheduled_for": slot.isoformat()}
    )
    check("approved variant is scheduled (201)", first.status_code == 201, first.text[:300])
    first_job = first.json()["job"]

    duplicate = client.post(
        f"/variants/{linkedin['id']}/schedule", json={"scheduled_for": slot.isoformat()}
    )
    check(
        "duplicate schedule returns the same job with deduplicated=true",
        duplicate.status_code == 200
        and duplicate.json()["deduplicated"] is True
        and duplicate.json()["job"]["id"] == first_job["id"],
        duplicate.text[:300],
    )
    check("only one job exists", client.get("/jobs").json()["total"] == 1)

    # --- ops guard --------------------------------------------------------
    forbidden = client.post("/ops/run-due", json={"limit": 5})
    check("ops endpoint rejects a request without a token", forbidden.status_code == 403)

    # --- publish ----------------------------------------------------------
    run = client.post("/ops/run-due", json={"limit": 5}, headers=ops)
    report = run.json() if run.status_code == 200 else {}
    check(
        "worker tick publishes the due job",
        report.get("claimed") == 1 and report.get("completed") == 1,
        run.text[:300],
    )

    job = client.get(f"/jobs/{first_job['id']}").json()
    check(
        "job is completed with an external id",
        job["status"] == "completed" and bool(job["external_id"]),
        str(job)[:300],
    )
    published = client.get(f"/variants/{linkedin['id']}").json()
    check("variant is marked published", published["status"] == "published", published["status"])

    history = client.get(f"/publications?variant_id={linkedin['id']}").json()
    attempt = history["items"][0] if history["items"] else {}
    check(
        "publication history records the attempt",
        history["total"] == 1 and attempt.get("success") is True and attempt.get("adapter") == "mock_linkedin",
        str(history)[:300],
    )
    check(
        "history includes the scheduled slot",
        attempt.get("scheduled_for") is not None,
        str(attempt)[:200],
    )

    again = client.post("/ops/run-due", json={"limit": 5}, headers=ops).json()
    check("a second tick publishes nothing", again.get("claimed") == 0, str(again))
    check(
        "history still has exactly one attempt",
        client.get("/publications").json()["total"] == 1,
    )

    reopen_published = client.post(f"/variants/{linkedin['id']}/reopen", json={})
    check(
        "a published variant cannot be reopened",
        reopen_published.status_code == 409
        and reopen_published.json()["error"]["details"]["from"] == "published",
        reopen_published.text[:300],
    )

    # --- unconfigured telegram fails clearly ------------------------------
    telegram = variants["telegram"]
    client.post(f"/variants/{telegram['id']}/approve", json={})
    tg_slot = datetime.now(timezone.utc) - timedelta(seconds=5)
    tg_scheduled = client.post(
        f"/variants/{telegram['id']}/schedule", json={"scheduled_for": tg_slot.isoformat()}
    )
    check("telegram variant can be scheduled", tg_scheduled.status_code == 201, tg_scheduled.text[:200])

    tg_run = client.post("/ops/run-due", json={"limit": 5}, headers=ops).json()
    check("unconfigured telegram job fails instead of pretending", tg_run.get("failed") == 1, str(tg_run))

    tg_history = client.get(f"/publications?variant_id={telegram['id']}").json()
    tg_attempt = tg_history["items"][0] if tg_history["items"] else {}
    check(
        "failure is recorded with a useful, secret-free message",
        tg_attempt.get("success") is False and "not configured" in (tg_attempt.get("error") or "").lower(),
        str(tg_attempt)[:300],
    )
    check(
        "the variant is handed back to a human as approved",
        client.get(f"/variants/{telegram['id']}").json()["status"] == "approved",
    )

    # --- cleanup ----------------------------------------------------------
    client.close()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
