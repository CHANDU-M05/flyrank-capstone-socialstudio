"""URL ingestion: SSRF defences, redirects, timeouts, size and content-type limits.

The transport is injected, so every test is deterministic and none of them touches the
network.
"""
from __future__ import annotations

import httpx
import pytest

from app.services import ingest
from app.services.errors import IngestError
from tests.conftest import make_settings

PUBLIC_IP = "93.184.216.34"
PUBLIC_HOST = "https://blog.example.com/post"


def public_resolver(_host: str) -> list[str]:
    return [PUBLIC_IP]


def transport_returning(
    body: bytes = b"<html><body><p>hello</p></body></html>",
    *,
    status_code: int = 200,
    content_type: str = "text/html; charset=utf-8",
    headers: dict | None = None,
) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        merged = {"content-type": content_type}
        merged.update(headers or {})
        return httpx.Response(status_code, headers=merged, content=body)

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- URL validation
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com", "javascript:alert(1)"])
def test_non_http_schemes_are_rejected(url):
    with pytest.raises(IngestError) as excinfo:
        ingest.validate_url(url)
    assert excinfo.value.code == "url_scheme_not_allowed"


def test_url_credentials_are_rejected():
    with pytest.raises(IngestError) as excinfo:
        ingest.validate_url("https://user:secret@example.com/post", resolver=public_resolver)
    assert excinfo.value.code == "url_credentials_not_allowed"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
    ],
)
def test_private_and_internal_literal_addresses_are_blocked(url):
    with pytest.raises(IngestError) as excinfo:
        ingest.validate_url(url)
    assert excinfo.value.code == "url_target_not_public"


def test_hostname_resolving_to_a_private_address_is_blocked():
    with pytest.raises(IngestError) as excinfo:
        ingest.validate_url("http://internal.example.com/", resolver=lambda host: ["10.1.2.3"])
    assert excinfo.value.code == "url_target_not_public"


def test_hostname_resolving_to_a_public_address_is_allowed():
    assert ingest.validate_url(PUBLIC_HOST, resolver=public_resolver) == "https://blog.example.com/post"


def test_unresolvable_host_is_reported():
    def failing(_host: str):
        raise OSError("no answer")

    with pytest.raises(IngestError) as excinfo:
        ingest.validate_url("https://nope.invalid/", resolver=failing)
    assert excinfo.value.code == "url_dns_failed"


def test_missing_url_and_overlong_url_are_rejected():
    with pytest.raises(IngestError) as excinfo:
        ingest.validate_url("   ")
    assert excinfo.value.code == "url_missing"

    with pytest.raises(IngestError) as excinfo:
        ingest.validate_url("https://example.com/" + "a" * 3000)
    assert excinfo.value.code == "url_too_long"


# --------------------------------------------------------------------------- fetching
def test_successful_fetch_extracts_text_and_title(tmp_path):
    settings = make_settings(tmp_path)
    body = (
        b"<html><head><title>Durable queues &amp; you</title>"
        b"<style>.a{color:red}</style></head>"
        b"<body><script>window.x=1</script><h1>Heading</h1><p>First para.</p>"
        b"<p>Second para.</p></body></html>"
    )
    document = ingest.fetch(
        PUBLIC_HOST, settings, resolver=public_resolver, transport=transport_returning(body)
    )
    assert document.title == "Durable queues & you"
    assert "Heading" in document.content
    assert "First para." in document.content
    assert "window.x" not in document.content
    assert "color:red" not in document.content
    assert document.content_type == "text/markdown"
    assert document.redirects == 0


def test_plain_text_is_preserved(tmp_path):
    settings = make_settings(tmp_path)
    document = ingest.fetch(
        PUBLIC_HOST,
        settings,
        resolver=public_resolver,
        transport=transport_returning(b"line one\nline two", content_type="text/plain"),
    )
    assert document.content == "line one\nline two"
    assert document.content_type == "text/plain"


def test_unsupported_content_type_is_rejected(tmp_path):
    settings = make_settings(tmp_path)
    with pytest.raises(IngestError) as excinfo:
        ingest.fetch(
            PUBLIC_HOST,
            settings,
            resolver=public_resolver,
            transport=transport_returning(b"%PDF-1.7", content_type="application/pdf"),
        )
    assert excinfo.value.code == "unsupported_content_type"


def test_upstream_http_error_is_reported(tmp_path):
    settings = make_settings(tmp_path)
    with pytest.raises(IngestError) as excinfo:
        ingest.fetch(
            PUBLIC_HOST, settings, resolver=public_resolver, transport=transport_returning(b"gone", status_code=404)
        )
    assert excinfo.value.code == "upstream_http_error"
    assert "404" in excinfo.value.message


def test_declared_content_length_over_the_limit_is_rejected(tmp_path):
    settings = make_settings(tmp_path, ingest_max_bytes=1024)
    with pytest.raises(IngestError) as excinfo:
        ingest.fetch(
            PUBLIC_HOST,
            settings,
            resolver=public_resolver,
            transport=transport_returning(b"<p>" + b"x" * 2000 + b"</p>"),
        )
    assert excinfo.value.code == "response_too_large"


def test_streamed_response_over_the_limit_is_rejected(tmp_path):
    """No Content-Length header: the limit must still hold while streaming."""
    settings = make_settings(tmp_path, ingest_max_bytes=2048)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=iter([b"<p>", b"x" * 1500, b"x" * 1500, b"</p>"]),
        )

    with pytest.raises(IngestError) as excinfo:
        ingest.fetch(
            PUBLIC_HOST, settings, resolver=public_resolver, transport=httpx.MockTransport(handler)
        )
    assert excinfo.value.code == "response_too_large"


def test_timeout_is_reported(tmp_path):
    settings = make_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(IngestError) as excinfo:
        ingest.fetch(PUBLIC_HOST, settings, resolver=public_resolver, transport=httpx.MockTransport(handler))
    assert excinfo.value.code == "fetch_timeout"


def test_transport_error_is_reported(tmp_path):
    settings = make_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(IngestError) as excinfo:
        ingest.fetch(PUBLIC_HOST, settings, resolver=public_resolver, transport=httpx.MockTransport(handler))
    assert excinfo.value.code == "fetch_failed"


def test_empty_document_is_not_reported_as_success(tmp_path):
    settings = make_settings(tmp_path)
    with pytest.raises(IngestError) as excinfo:
        ingest.fetch(
            PUBLIC_HOST, settings, resolver=public_resolver, transport=transport_returning(b"<html></html>")
        )
    assert excinfo.value.code == "no_readable_content"


# --------------------------------------------------------------------------- redirects
def test_redirect_to_a_public_url_is_followed(tmp_path):
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/post":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>arrived</p>")

    document = ingest.fetch(
        PUBLIC_HOST, settings, resolver=public_resolver, transport=httpx.MockTransport(handler)
    )
    assert document.redirects == 1
    assert document.url == "https://blog.example.com/final"
    assert "arrived" in document.content


def test_redirect_to_an_internal_address_is_blocked(tmp_path):
    settings = make_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    with pytest.raises(IngestError) as excinfo:
        ingest.fetch(PUBLIC_HOST, settings, resolver=public_resolver, transport=httpx.MockTransport(handler))
    assert excinfo.value.code == "url_target_not_public"


def test_too_many_redirects_is_reported(tmp_path):
    settings = make_settings(tmp_path, ingest_max_redirects=2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"/hop{int(request.url.path[-1]) + 1}"})

    with pytest.raises(IngestError) as excinfo:
        ingest.fetch(
            "https://blog.example.com/hop0",
            settings,
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.code == "too_many_redirects"


def test_redirect_loop_is_detected(tmp_path):
    settings = make_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/post"})

    with pytest.raises(IngestError) as excinfo:
        ingest.fetch(PUBLIC_HOST, settings, resolver=public_resolver, transport=httpx.MockTransport(handler))
    assert excinfo.value.code in {"redirect_loop", "too_many_redirects"}
