"""Deterministic variant generation from a *stored* source.

Composition is deliberately template-driven: a shared summary is combined with a
platform-specific hook, call to action, separator and hashtag budget taken from the
platform's profile. An LLM composer can be dropped in later by implementing
``compose_text()`` differently — nothing else in the pipeline needs to change.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import Source, Variant
from app.services.errors import InvalidTransition, ValidationFailed
from app.services.profiles import (
    PROFILES,
    PlatformProfile,
    ValidationResult,
    compose_full_text,
    get_profile,
    validate,
)

_STOPWORDS = frozenset(
    """
    about above after again against also among and another any are because been before
    being below between both but came come could did does doing down during each even
    every few find first for from further get gets getting give given go going had has
    have having here hers herself him himself his how however into its itself just know
    last least less let like made make making many may maybe might more most much must
    need never new next not now off often once one only onto other our ours ourselves
    out over own per put rather really same saw say says see seem seen several she
    should since some still such take taken than that the their theirs them themselves
    then there these they thing think this those though through thus time times to too
    toward two under until upon use used uses using very want was way we well were what
    when where whether which while who whom whose why will with within without would
    year yet you your yours yourself
    """.split()
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*")
_MD_INLINE = re.compile(r"(\*\*|__|~~|`)")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{3,}")
_TITLE_NOISE = re.compile(r"[^a-z0-9 ]+")
_TITLE_MAX = 90


def _clean_inline(text: str) -> str:
    return _MD_INLINE.sub("", (text or "").replace("\r\n", "\n").replace("\r", "\n"))


def _paragraphs(content: str, limit: int = 14) -> list[str]:
    lines: list[str] = []
    for raw in _clean_inline(content).split("\n"):
        line = _MD_HEADING.sub("", raw).strip()
        if not line or line.startswith(("![", "[![")):
            continue
        lines.append(re.sub(r"\s+", " ", line))
        if len(lines) >= limit:
            break
    return lines


def _clip(text: str, limit: int) -> str:
    """Deterministically shorten to ``limit`` characters at a word boundary."""
    text = text.strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    if not cut:
        cut = text[: limit - 1]
    return f"{cut}…"


def derive_title(source: Source) -> str:
    """Title from the stored source: explicit title, else the first meaningful line."""
    if source.title and source.title.strip():
        return _clip(source.title.strip().rstrip("."), _TITLE_MAX)
    for line in _paragraphs(source.content, limit=3):
        return _clip(line.rstrip("."), _TITLE_MAX)
    return "Untitled"


def _normalise_line(text: str) -> str:
    return _TITLE_NOISE.sub(" ", text.lower()).strip()


def _without_leading_title(content: str, title: str) -> str:
    """Drop a first line that only repeats the title.

    Pasted Markdown usually opens with an H1 that is also the explicit title. Without this,
    the composed post reads ``"<title>. <title> <body>"`` — the same sentence twice, which is
    the first thing a reviewer notices. Only an exact (normalised) match is removed; a
    deliberately different opening line is left alone.
    """
    if not title:
        return content
    wanted = _normalise_line(title)
    if not wanted:
        return content
    lines = _clean_inline(content).split("\n")
    for index, raw in enumerate(lines):
        candidate = _MD_HEADING.sub("", raw).strip()
        if not candidate:
            continue
        if _normalise_line(candidate) == wanted:
            return "\n".join(lines[index + 1 :])
        return content
    return content


def summarize(content: str, max_chars: int, max_sentences: int = 3) -> str:
    """First sentences of the document, flattened into one paragraph.

    Headings are kept by ``_paragraphs`` (without their ``#`` markers) and have no closing
    punctuation, so each paragraph is terminated before the join — otherwise the summary reads
    ``"… own the schedule. Same clock everywhere Every timestamp …"``.
    """
    parts: list[str] = []
    for paragraph in _paragraphs(content):
        piece = paragraph.strip()
        if not piece:
            continue
        if piece[-1] not in ".!?…:":
            piece = f"{piece}."
        parts.append(piece)
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if not text:
        return ""
    picked: list[str] = []
    length = 0
    for sentence in _SENTENCE_SPLIT.split(text)[:max_sentences]:
        sentence = sentence.strip()
        if not sentence:
            continue
        if picked and length + len(sentence) + 1 > max_chars:
            break
        picked.append(sentence)
        length += len(sentence) + 1
    return _clip(" ".join(picked).strip(), max_chars)


def keywords(content: str, limit: int) -> list[str]:
    """Deterministic keyword extraction: frequency, then first appearance.

    Tags are returned lowercase because that is the convention the profile enforces as a
    warning; emitting ``#Database`` would make every generated variant carry a warning that
    only exists because of the generator.
    """
    tokens = [tok for tok in _WORD.findall(_clean_inline(content).lower()) if tok not in _STOPWORDS]
    if not tokens:
        return ["update"][:limit]
    first_seen = {token: index for index, token in enumerate(tokens)}
    ranked = sorted(Counter(tokens).items(), key=lambda item: (-item[1], first_seen[item[0]]))
    return [token for token, _ in ranked[:limit]]


def _assemble(profile: PlatformProfile, hook: str, summary: str, cta: str) -> str:
    values = {"hook": hook, "summary": summary, "cta": cta}
    parts = [values[section].strip() for section in profile.sections if values.get(section, "").strip()]
    return profile.separator.join(parts).strip()


def compose_text(source: Source, profile: PlatformProfile) -> tuple[str, list[str]]:
    """Build one platform's body and hashtags, guaranteed to fit the profile limit."""
    title = derive_title(source)
    url = (source.origin_url or "").strip()
    hook = profile.hook_template.format(title=title)
    cta = profile.cta_template.format(url=url) if url else profile.cta_without_url
    hashtags = [f"#{word}" for word in keywords(source.content, profile.hashtag_target)]
    tag_block = " ".join(hashtags)

    budget = profile.max_chars - (len(tag_block) + 2 if hashtags else 0)
    overhead = len(hook) + len(cta) + len(profile.separator) * (len(profile.sections) - 1)
    summary_budget = max(60, min(profile.summary_chars, budget - overhead))
    # How many sentences to take is a property of the platform, not a constant: X wants one
    # idea, a Telegram channel has room for the argument. Without this the 4096-character
    # profile produced the same three sentences as the 280-character one.
    max_sentences = max(3, min(9, profile.summary_chars // 100))
    summary = summarize(
        _without_leading_title(source.content, title), summary_budget, max_sentences
    )
    body = _assemble(profile, hook, summary, cta)

    if len(body) > budget:
        excess = len(body) - budget
        summary = _clip(summary, max(40, len(summary) - excess - 1))
        body = _assemble(profile, hook, summary, cta)
    if len(body) > budget:
        excess = len(body) - budget
        hook = _clip(hook, max(12, len(hook) - excess - 1))
        body = _assemble(profile, hook, summary, cta)
    if len(body) > budget:  # last resort: never emit text the platform would reject
        body = _clip(body, budget)
    return body, hashtags


def apply_validation(variant: Variant) -> ValidationResult:
    profile = get_profile(variant.platform)
    result = validate(variant.text, list(variant.hashtags or []), profile)
    variant.validation = result.as_dict()
    return result


def generate_variants(
    db: Session,
    source: Source,
    platforms: list[str] | None = None,
    *,
    now: datetime,
) -> list[Variant]:
    """Create or refresh draft variants for ``source``.

    Existing non-draft variants are left untouched (they may already be approved,
    scheduled or published) — generation never silently overwrites reviewed content.
    """
    requested = list(PROFILES) if platforms is None else list(dict.fromkeys(platforms))
    unknown = [platform for platform in requested if platform not in PROFILES]
    if unknown:
        raise ValidationFailed(f"Unknown platform(s): {', '.join(unknown)}")
    if not requested:
        raise ValidationFailed(
            "At least one platform is required (or send no platform list to generate all).",
            code="no_platforms",
        )

    results: list[Variant] = []
    for platform in requested:
        profile = PROFILES[platform]
        body, hashtags = compose_text(source, profile)
        result = validate(body, hashtags, profile)

        existing = db.execute(
            select(Variant).where(
                Variant.source_id == source.id, Variant.platform == platform
            )
        ).scalar_one_or_none()

        if existing is not None and existing.status != "draft":
            results.append(existing)
            continue

        if existing is None:
            variant = Variant(
                source_id=source.id,
                platform=platform,
                text=body,
                hashtags=hashtags,
                validation=result.as_dict(),
                status="draft",
                created_at=now,
                updated_at=now,
                status_changed_at=now,
            )
            db.add(variant)
            results.append(variant)
        else:
            existing.text = body
            existing.hashtags = hashtags
            existing.validation = result.as_dict()
            existing.updated_at = now
            results.append(existing)

    db.commit()
    return results


def edit_variant(
    variant: Variant,
    *,
    text: str | None,
    hashtags: list[str] | None,
    now: datetime,
) -> ValidationResult:
    """Edit a draft and revalidate. Editing anything but a draft is a conflict."""
    if variant.status != "draft":
        raise InvalidTransition(
            f"Cannot edit a variant in status '{variant.status}'; only drafts are editable.",
            code="variant_not_editable",
        )
    if text is not None:
        variant.text = text
    if hashtags is not None:
        variant.hashtags = list(hashtags)
    variant.updated_at = now
    return apply_validation(variant)


def refresh_validation(variant: Variant, *, now: datetime) -> ValidationResult:
    variant.updated_at = now
    return apply_validation(variant)


def list_variants(
    db: Session,
    *,
    status: str | None,
    platform: str | None,
    source_id: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Variant], int]:
    conditions = []
    if status:
        conditions.append(Variant.status == status)
    if platform:
        conditions.append(Variant.platform == platform)
    if source_id:
        conditions.append(Variant.source_id == source_id)

    total_query = select(func.count()).select_from(Variant)
    rows_query = select(Variant)
    for condition in conditions:
        total_query = total_query.where(condition)
        rows_query = rows_query.where(condition)

    total = db.scalar(total_query) or 0
    rows = (
        db.execute(
            rows_query.order_by(Variant.created_at.desc(), Variant.id)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


__all__ = [
    "apply_validation",
    "compose_full_text",
    "compose_text",
    "derive_title",
    "edit_variant",
    "generate_variants",
    "keywords",
    "list_variants",
    "refresh_validation",
    "summarize",
]
