"""The manifest must describe the API that is actually served.

A hand-written `capstone.yaml` rots the moment a route is added. This test parses it, checks
the required keys, and compares `endpoints` with the paths in the app's own OpenAPI document —
so the two can only agree while they are both true.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_KEYS = ("name", "run", "seed", "test", "base_url", "endpoints")


def _manifest() -> dict:
    return yaml.safe_load((ROOT / "capstone.yaml").read_text(encoding="utf-8"))


def test_manifest_parses_and_declares_required_keys():
    data = _manifest()
    for key in REQUIRED_KEYS:
        assert key in data, f"capstone.yaml is missing '{key}'"

    assert data["base_url"].startswith("http")
    assert isinstance(data["endpoints"], list) and data["endpoints"]
    for endpoint in data["endpoints"]:
        assert set(endpoint) == {"method", "path", "auth"}, endpoint
        assert endpoint["method"] in {"GET", "POST", "PATCH", "PUT", "DELETE"}, endpoint
        assert endpoint["path"].startswith("/"), endpoint


def test_manifest_declares_the_whole_workflow():
    declared = {(item["method"], item["path"]) for item in _manifest()["endpoints"]}
    for pair in (
        ("POST", "/sources"),
        ("POST", "/sources/ingest"),
        ("POST", "/sources/{source_id}/variants"),
        ("POST", "/variants/{variant_id}/approve"),
        ("POST", "/variants/{variant_id}/schedule"),
        ("GET", "/jobs/{job_id}"),
        ("GET", "/publications"),
        ("POST", "/ops/run-due"),
    ):
        assert pair in declared, f"capstone.yaml does not advertise {pair}"


def test_manifest_matches_the_served_openapi(client):
    """No endpoint may be advertised that is not served, and none served that is not advertised."""
    declared = {(item["method"], item["path"]) for item in _manifest()["endpoints"]}
    paths = client.get("/openapi.json").json()["paths"]
    served = {
        (method.upper(), path) for path, operations in paths.items() for method in operations
    }

    assert declared == served, (
        f"advertised but not served: {sorted(declared - served)}; "
        f"served but not advertised: {sorted(served - declared)}"
    )


def test_ops_endpoint_is_the_only_protected_route():
    protected = {item["path"] for item in _manifest()["endpoints"] if item["auth"] != "none"}
    assert protected == {"/ops/run-due"}
