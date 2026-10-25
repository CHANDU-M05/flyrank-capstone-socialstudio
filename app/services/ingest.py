"""URL ingestion with SSRF defences.

The threat model is a reviewer pasting ``http://169.254.169.254/latest/meta-data/`` or
``http://redis:6379`` into the API. Every hop is therefore re-validated: the scheme must be
http(s), the host must not carry credentials, the name must resolve, and *every* returned
address must be globally routable. Redirects are followed manually so a public URL cannot
bounce us into the private network.

Documented limitations (see README "Known limitations"):

* HTML extraction is a tag-stripping heuristic, not a readability algorithm. JavaScript
  rendering, paywalls and cookie walls produce poor text; the API returns what it actually
  fetched rather than pretending otherwise.
* DNS is resolved immediately before connecting, but a hostile resolver could still answer
  differently on the connection itself (DNS rebinding). Closing that hole needs a pinned
  connection pool that connects to the validated IP; that is noted as future work rather
  than claimed as done.
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import Settings
from app.services.errors import IngestError

#: Content types we can actually turn into readable text.
ALLOWED_CONTENT_TYPES: tuple[str, ...] = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
)
_TEXT_CONTENT_TYPES: tuple[str, ...] = ("text/plain", "text/markdown", "text/x-markdown")
_ALLOWED_SCHEMES = ("http", "https")

_SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript|template|svg)\b[^>]*>.*?</\1>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_BLOCK_TAG = re.compile(r"(?i)</?(p|div|br|li|tr|h[1-6]|section|article|blockquote)\b[^>]*>")
_TAG = re.compile(r"(?s)<[^>]+>")
_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_OG_TITLE = re.compile(r"""(?is)<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']*)["']""")
_BLANKS = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t\u00a0]+")

#: ``(host) -> addresses`` — injectable so tests never touch real DNS.
Resolver = Callable[[str], Iterable[str]]


@dataclass(frozen=True)
class FetchedDocument:
    """What a successful fetch produced, before it is persisted."""

    url: str
    content: str
    title: str | None
    content_type: str
    size_bytes: int
    redirects: int


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def _is_public_address(raw: str) -> bool:
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(address.is_global) and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_url(url: str, *, resolver: Resolver | None = None) -> str:
    """Return a normalised URL, or raise ``IngestError``. Blocks non-public targets."""
    candidate = (url or "").strip()
    if not candidate:
        raise IngestError("A URL is required.", code="url_missing")
    if len(candidate) > 2048:
        raise IngestError("URL is too long (max 2048 characters).", code="url_too_long")

    try:
        parts = urlsplit(candidate)
    except ValueError:
        raise IngestError("The URL could not be parsed.", code="url_invalid") from None

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise IngestError(
            f"Unsupported URL scheme '{parts.scheme or '(none)'}'. Only http and https are allowed.",
            code="url_scheme_not_allowed",
        )
    if parts.username or parts.password:
        raise IngestError(
            "URLs containing credentials are not accepted.", code="url_credentials_not_allowed"
        )
    if not parts.hostname:
        raise IngestError("The URL has no host.", code="url_invalid")

    host = parts.hostname
    try:
        port = parts.port
    except ValueError:
        raise IngestError("The URL port is not a number.", code="url_invalid") from None
    if port is not None and not (1 <= port <= 65535):
        raise IngestError("The URL port is out of range.", code="url_invalid")

    # A literal IP must be public on its own account; a name must resolve to public addresses.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        lookup = resolver or _default_resolver
        try:
            addresses = list(lookup(host))
        except (socket.gaierror, UnicodeError, OSError) as exc:
            raise IngestError(
                f"Could not resolve host '{host}': {exc.__class__.__name__}.",
                code="url_dns_failed",
            ) from None
        if not addresses:
            raise IngestError(f"Host '{host}' did not resolve.", code="url_dns_failed")
        blocked = [address for address in addresses if not _is_public_address(address)]
        if blocked:
            raise IngestError(
                "Refusing to fetch a private, loopback, link-local or reserved address "
                f"(host '{host}' resolved to {', '.join(sorted(set(blocked)))}).",
                code="url_target_not_public",
            )
    else:
        if not _is_public_address(host):
            raise IngestError(
                f"Refusing to fetch the non-public address '{host}'.", code="url_target_not_public"
            )

    normalised = urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))
    return normalised


def _decode(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for parameter in content_type.split(";")[1:]:
        key, _, value = parameter.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip('"')
            break
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def extract_text(document: str, content_type: str) -> str:
    """Turn an HTML or text document into readable plain text."""
    if content_type in _TEXT_CONTENT_TYPES:
        return document.replace("\r\n", "\n").replace("\r", "\n").strip()

    text = _COMMENT.sub(" ", document)
    text = _SCRIPT_STYLE.sub(" ", text)
    text = _BLOCK_TAG.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return _BLANKS.sub("\n\n", text).strip()


def extract_title(document: str, content_type: str) -> str | None:
    if content_type in _TEXT_CONTENT_TYPES:
        return None
    match = _OG_TITLE.search(document) or _TITLE.search(document)
    if not match:
        return None
    title = html.unescape(_TAG.sub("", match.group(1)))
    title = _SPACES.sub(" ", title).strip()
    return title[:300] or None


def fetch(
    url: str,
    settings: Settings,
    *,
    resolver: Resolver | None = None,
    transport: httpx.BaseTransport | None = None,
) -> FetchedDocument:
    """Fetch ``url`` safely and return extracted text. Raises ``IngestError`` on failure.

    Redirects are followed by hand so each destination is re-validated (scheme, credentials
    and DNS) before it is contacted.
    """
    current = validate_url(url, resolver=resolver)
    seen: set[str] = set()
    redirects = 0

    with httpx.Client(
        transport=transport,
        timeout=settings.ingest_timeout_seconds,
        follow_redirects=False,
        headers={"User-Agent": "flyrank-social-studio/1.0 (+ingest)"},
    ) as client:
        while True:
            if current in seen:
                raise IngestError("Redirect loop detected.", code="redirect_loop")
            seen.add(current)

            try:
                with client.stream("GET", current) as response:
                    status = response.status_code
                    location = response.headers.get("location")

                    if status in (301, 302, 303, 307, 308):
                        redirects += 1
                        if redirects > settings.ingest_max_redirects:
                            raise IngestError(
                                f"Too many redirects (max {settings.ingest_max_redirects}).",
                                code="too_many_redirects",
                            )
                        if not location:
                            raise IngestError(
                                f"HTTP {status} redirect without a Location header.",
                                code="redirect_invalid",
                            )
                        current = validate_url(
                            str(httpx.URL(current).join(location)), resolver=resolver
                        )
                        continue

                    if status >= 400:
                        raise IngestError(
                            f"The server returned HTTP {status}.", code="upstream_http_error"
                        )

                    content_type = (response.headers.get("content-type") or "").split(";")[0]
                    content_type = content_type.strip().lower()
                    if not content_type:
                        raise IngestError(
                            "The response did not declare a Content-Type.",
                            code="unsupported_content_type",
                        )
                    if content_type not in ALLOWED_CONTENT_TYPES:
                        raise IngestError(
                            f"Unsupported content type '{content_type}'. Allowed: "
                            + ", ".join(ALLOWED_CONTENT_TYPES),
                            code="unsupported_content_type",
                        )

                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > settings.ingest_max_bytes:
                        raise IngestError(
                            f"Response is {declared} bytes; the limit is "
                            f"{settings.ingest_max_bytes}.",
                            code="response_too_large",
                        )

                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > settings.ingest_max_bytes:
                            raise IngestError(
                                f"Response exceeded the {settings.ingest_max_bytes} byte limit.",
                                code="response_too_large",
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
            except httpx.TimeoutException:
                raise IngestError(
                    f"The request timed out after {settings.ingest_timeout_seconds}s.",
                    code="fetch_timeout",
                ) from None
            except httpx.HTTPError as exc:
                raise IngestError(
                    f"The request failed: {exc.__class__.__name__}.", code="fetch_failed"
                ) from None
            break

    document = _decode(body, content_type)
    text = extract_text(document, content_type)
    if not text.strip():
        raise IngestError(
            "No readable text could be extracted from the fetched document.",
            code="no_readable_content",
        )

    return FetchedDocument(
        url=current,
        content=text,
        title=extract_title(document, content_type),
        content_type="text/plain" if content_type in _TEXT_CONTENT_TYPES else "text/markdown",
        size_bytes=len(body),
        redirects=redirects,
    )
