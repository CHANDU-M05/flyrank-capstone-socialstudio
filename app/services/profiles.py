"""Platform constraint profiles and the single validation implementation.

The profile is data, not code: adding a platform means adding a ``PlatformProfile`` entry
(plus an adapter), never editing the validator or the campaign logic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.errors import ValidationFailed

HASHTAG_RE = re.compile(r"^#[A-Za-z0-9_]{1,50}$")
_MARKDOWN_MARKERS = ("**", "__", "`", "](", "##")


@dataclass(frozen=True)
class PlatformProfile:
    """Everything the generator and the validator need to know about a platform."""

    platform: str
    display_name: str
    max_chars: int
    min_chars: int
    min_hashtags: int
    max_hashtags: int
    hashtag_target: int
    summary_chars: int
    separator: str
    sections: tuple[str, ...]
    hook_template: str
    cta_template: str
    cta_without_url: str
    allow_markdown: bool
    tone: str
    notes: str


PROFILES: dict[str, PlatformProfile] = {
    "x": PlatformProfile(
        platform="x",
        display_name="X",
        max_chars=280,
        min_chars=40,
        min_hashtags=1,
        max_hashtags=3,
        hashtag_target=3,
        summary_chars=140,
        separator=" ",
        sections=("hook", "summary", "cta"),
        hook_template="{title}.",
        cta_template="Link: {url}",
        cta_without_url="Read the full post.",
        allow_markdown=False,
        tone="punchy, conversational, one idea per post",
        notes="Single-line short form; no markdown emphasis; hashtags appended at the end.",
    ),
    "linkedin": PlatformProfile(
        platform="linkedin",
        display_name="LinkedIn",
        max_chars=3000,
        min_chars=120,
        min_hashtags=1,
        max_hashtags=5,
        hashtag_target=4,
        summary_chars=700,
        separator="\n\n",
        sections=("hook", "summary", "cta"),
        hook_template="{title}",
        cta_template="Full post: {url}",
        cta_without_url="The full post is on my blog.",
        allow_markdown=False,
        tone="professional, reflective, first person",
        notes="Short paragraphs separated by blank lines; no markdown emphasis.",
    ),
    "telegram": PlatformProfile(
        platform="telegram",
        display_name="Telegram",
        max_chars=4096,
        min_chars=80,
        min_hashtags=1,
        max_hashtags=10,
        hashtag_target=6,
        summary_chars=900,
        separator="\n\n",
        sections=("hook", "summary", "cta"),
        hook_template="*{title}*",
        cta_template="Read more: {url}",
        cta_without_url="More updates soon.",
        allow_markdown=True,
        tone="channel voice, direct, useful",
        notes="Markdown allowed (single-asterisk bold); longer form with more hashtags.",
    ),
}

PLATFORMS: tuple[str, ...] = tuple(PROFILES)


def get_profile(platform: str) -> PlatformProfile:
    try:
        return PROFILES[platform]
    except KeyError:  # pragma: no cover - guarded by schema Literal
        raise ValidationFailed(f"Unknown platform '{platform}'.", code="unknown_platform") from None


def compose_full_text(body: str, hashtags: list[str]) -> str:
    """The exact text that would be published: body, blank line, hashtags."""
    body = body.strip()
    if not hashtags:
        return body
    return f"{body}\n\n{' '.join(hashtags)}"


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"valid": self.valid, "errors": list(self.errors), "warnings": list(self.warnings)}


def validate(body: str, hashtags: list[str], profile: PlatformProfile) -> ValidationResult:
    """Validate a variant against its profile. Never truncates, never assumes success."""
    errors: list[str] = []
    warnings: list[str] = []
    body = (body or "").strip()
    tags = [tag.strip() for tag in (hashtags or []) if tag and tag.strip()]

    if not body:
        errors.append("text must not be empty")
    elif len(body) < profile.min_chars:
        errors.append(
            f"text is too short: {len(body)} characters; {profile.display_name} needs at least "
            f"{profile.min_chars}"
        )

    full_length = len(compose_full_text(body, tags))
    if full_length > profile.max_chars:
        errors.append(
            f"post is {full_length} characters; {profile.display_name} allows at most "
            f"{profile.max_chars}"
        )
    elif full_length > int(profile.max_chars * 0.9):
        warnings.append(
            f"post is close to the limit: {full_length}/{profile.max_chars} characters"
        )

    if len(tags) < profile.min_hashtags:
        errors.append(
            f"at least {profile.min_hashtags} hashtag(s) required; found {len(tags)}"
        )
    if len(tags) > profile.max_hashtags:
        errors.append(
            f"at most {profile.max_hashtags} hashtags allowed on {profile.display_name}; "
            f"found {len(tags)}"
        )

    seen: set[str] = set()
    for tag in tags:
        if not HASHTAG_RE.match(tag):
            errors.append(f"hashtag '{tag}' is invalid (use letters, digits and underscores only)")
        if tag.lower() in seen:
            errors.append(f"duplicate hashtag '{tag}'")
        seen.add(tag.lower())
        if tag != tag.lower():
            warnings.append(f"hashtag '{tag}' is not lowercase")

    if not profile.allow_markdown:
        found = [marker for marker in _MARKDOWN_MARKERS if marker in body]
        if found:
            errors.append(
                f"{profile.display_name} does not support markdown emphasis "
                f"(found {', '.join(found)})"
            )

    return ValidationResult(errors=tuple(errors), warnings=tuple(warnings))
