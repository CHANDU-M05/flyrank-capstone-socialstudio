"""Variant generation: determinism, limits, and the rule that variants need a stored source."""
from __future__ import annotations

import pytest

from app.models.entities import Source
from app.services import review, sources, variants
from app.services.errors import InvalidTransition, NotFoundError
from app.services.profiles import PROFILES, compose_full_text, validate


def _store(db, markdown: str, now) -> Source:
    return sources.create_pasted_source(
        db, content=markdown, title=None, content_type="text/markdown", now=now
    )


def test_variants_are_generated_from_a_stored_source(db, now, sample_markdown):
    source = _store(db, sample_markdown, now)
    created = variants.generate_variants(db, source, now=now)

    assert {variant.platform for variant in created} == {"x", "linkedin", "telegram"}
    for variant in created:
        assert variant.source_id == source.id
        assert variant.status == "draft"
        assert variant.text
        assert variant.hashtags
        assert variant.validation["valid"] is True
        assert variant.created_at.tzinfo is not None


@pytest.mark.parametrize("platform", ["x", "linkedin", "telegram"])
def test_generated_variants_fit_their_platform_profile(db, now, sample_markdown, platform):
    source = _store(db, sample_markdown, now)
    variant = variants.generate_variants(db, source, [platform], now=now)[0]
    profile = PROFILES[platform]

    full = compose_full_text(variant.text, variant.hashtags)
    assert len(full) <= profile.max_chars
    assert profile.min_hashtags <= len(variant.hashtags) <= profile.max_hashtags
    assert validate(variant.text, variant.hashtags, profile).valid


def test_generation_is_deterministic(db, now, sample_markdown):
    source = _store(db, sample_markdown, now)
    first = variants.generate_variants(db, source, ["x"], now=now)[0]
    text, hashtags = first.text, list(first.hashtags)

    # Regenerating a draft replaces it in place with identical content.
    again = variants.generate_variants(db, source, ["x"], now=now)[0]
    assert again.id == first.id
    assert again.text == text
    assert again.hashtags == hashtags


def test_generation_never_overwrites_a_reviewed_variant(db, now, sample_markdown):
    source = _store(db, sample_markdown, now)
    variant = variants.generate_variants(db, source, ["x"], now=now)[0]
    review.approve_variant(db, variant, now=now)

    regenerated = variants.generate_variants(db, source, ["x"], now=now)[0]
    assert regenerated.id == variant.id
    assert regenerated.status == "approved"


def test_generation_requires_an_existing_source(db):
    with pytest.raises(NotFoundError):
        sources.get_source(db, "does-not-exist")


def test_unknown_platform_is_rejected(db, now, sample_markdown):
    from app.services.errors import ValidationFailed

    source = _store(db, sample_markdown, now)
    with pytest.raises(ValidationFailed):
        variants.generate_variants(db, source, ["myspace"], now=now)


def test_empty_platform_list_is_rejected(db, now, sample_markdown):
    from app.services.errors import ValidationFailed

    source = _store(db, sample_markdown, now)
    with pytest.raises(ValidationFailed):
        variants.generate_variants(db, source, [], now=now)


def test_editing_a_draft_revalidates(db, now, sample_markdown):
    source = _store(db, sample_markdown, now)
    variant = variants.generate_variants(db, source, ["x"], now=now)[0]

    result = variants.edit_variant(variant, text="too short", hashtags=["#tag"], now=now)
    assert result.valid is False
    assert variant.validation["valid"] is False
    assert variant.status == "draft"


def test_editing_a_non_draft_is_a_conflict(db, now, sample_markdown):
    source = _store(db, sample_markdown, now)
    variant = variants.generate_variants(db, source, ["x"], now=now)[0]
    review.approve_variant(db, variant, now=now)

    with pytest.raises(InvalidTransition) as excinfo:
        variants.edit_variant(variant, text="anything", hashtags=None, now=now)
    assert excinfo.value.code == "variant_not_editable"


def test_x_variants_contain_no_markdown_emphasis(db, now, sample_markdown):
    source = _store(db, sample_markdown, now)
    variant = variants.generate_variants(db, source, ["x"], now=now)[0]
    assert "**" not in variant.text
    assert "##" not in variant.text


def test_source_content_is_not_duplicated_into_the_variant(db, now, sample_markdown):
    source = _store(db, sample_markdown, now)
    variant = variants.generate_variants(db, source, ["x"], now=now)[0]
    assert len(variant.text) < len(source.content)
    assert variant.text != source.content


def test_generated_hashtags_are_lowercase_and_warning_free(db, now, sample_markdown):
    """The generator must not create the warnings it then reports."""
    source = _store(db, sample_markdown, now)
    for variant in variants.generate_variants(db, source, now=now):
        assert all(tag == tag.lower() for tag in variant.hashtags), variant.hashtags
        assert variant.validation["warnings"] == [], variant.validation["warnings"]


def test_the_title_is_not_repeated_in_the_summary(db, now):
    """A pasted document whose first line is the title must not publish it twice."""
    markdown = (
        "# Why durable schedules matter\n\n"
        "A timer lives inside one process, while a row lives in the database, so a restart "
        "cannot lose the work. That single sentence is the whole argument for keeping the "
        "schedule in the same store as the content.\n"
    )
    source = sources.create_pasted_source(
        db, content=markdown, title="Why durable schedules matter", content_type="text/markdown", now=now
    )
    variant = variants.generate_variants(db, source, ["x"], now=now)[0]

    assert variant.text.count("Why durable schedules matter") == 1, variant.text
    assert variant.text.startswith("Why durable schedules matter.")


def test_a_different_first_line_is_not_stripped(db, now):
    """Only an exact repeat of the title is removed — a real opening line is content."""
    markdown = (
        "Scheduling is harder than it looks.\n\n"
        "A timer lives inside one process, while a row lives in the database, so a restart "
        "cannot lose the work.\n"
    )
    source = sources.create_pasted_source(
        db, content=markdown, title="Why durable schedules matter", content_type="text/markdown", now=now
    )
    variant = variants.generate_variants(db, source, ["x"], now=now)[0]
    assert "Scheduling is harder than it looks." in variant.text


def test_the_roomier_platform_gets_the_longer_summary(db, now, sample_markdown):
    """Length per platform must follow the profile, not one shared sentence count."""
    source = _store(db, sample_markdown, now)
    lengths = {
        variant.platform: len(variant.text)
        for variant in variants.generate_variants(db, source, now=now)
    }
    assert lengths["x"] < lengths["linkedin"] < lengths["telegram"]
