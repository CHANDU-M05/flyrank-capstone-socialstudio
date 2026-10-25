"""Constraint profiles: limits, hashtag rules, formatting and boundary values."""
from __future__ import annotations

import pytest

from app.services.profiles import PROFILES, compose_full_text, validate

X = PROFILES["x"]
LINKEDIN = PROFILES["linkedin"]
TELEGRAM = PROFILES["telegram"]

BODY = "A" * 80


def test_all_three_profiles_exist_with_distinct_limits():
    assert set(PROFILES) == {"x", "linkedin", "telegram"}
    assert X.max_chars == 280
    assert LINKEDIN.max_chars == 3000
    assert TELEGRAM.max_chars == 4096
    assert len({profile.tone for profile in PROFILES.values()}) == 3


def test_compose_full_text_appends_hashtags_after_a_blank_line():
    assert compose_full_text("hello", ["#a", "#b"]) == "hello\n\n#a #b"
    assert compose_full_text("hello", []) == "hello"


def test_valid_variant_has_no_errors_or_warnings():
    result = validate(BODY, ["#engineering"], X)
    assert result.valid
    assert result.errors == ()
    assert result.warnings == ()


def test_exactly_at_the_limit_is_valid():
    tags = ["#tag"]
    body = "A" * (X.max_chars - len(compose_full_text("", tags)))
    assert len(compose_full_text(body, tags)) == X.max_chars
    assert validate(body, tags, X).valid


def test_one_character_over_the_limit_is_invalid():
    tags = ["#tag"]
    body = "A" * (X.max_chars - len(compose_full_text("", tags)) + 1)
    result = validate(body, tags, X)
    assert not result.valid
    assert any("allows at most" in error for error in result.errors)


def test_near_the_limit_produces_a_warning_not_an_error():
    tags = ["#tag"]
    body = "A" * (int(X.max_chars * 0.92) - len(compose_full_text("", tags)))
    result = validate(body, tags, X)
    assert result.valid
    assert any("close to the limit" in warning for warning in result.warnings)


def test_too_many_hashtags_is_invalid():
    result = validate(BODY, ["#a", "#b", "#c", "#d"], X)
    assert not result.valid
    assert any("at most 3 hashtags" in error for error in result.errors)


def test_no_hashtags_is_invalid():
    result = validate(BODY, [], X)
    assert not result.valid
    assert any("at least 1 hashtag" in error for error in result.errors)


def test_duplicate_hashtags_are_invalid_case_insensitively():
    result = validate(BODY, ["#Python", "#python"], X)
    assert not result.valid
    assert any("duplicate hashtag" in error for error in result.errors)


def test_malformed_hashtag_is_rejected():
    result = validate(BODY, ["#not valid", "tag"], X)
    assert not result.valid
    errors = " ".join(result.errors)
    assert "is invalid" in errors


def test_uppercase_hashtag_only_warns():
    result = validate(BODY, ["#Python"], X)
    assert result.valid
    assert any("not lowercase" in warning for warning in result.warnings)


def test_text_too_short_is_invalid():
    result = validate("short", ["#tag"], X)
    assert not result.valid
    assert any("too short" in error for error in result.errors)


def test_empty_text_is_invalid():
    result = validate("", ["#tag"], X)
    assert not result.valid
    assert any("must not be empty" in error for error in result.errors)


def test_markdown_is_rejected_where_the_platform_does_not_support_it():
    result = validate("**bold** " + BODY, ["#tag"], X)
    assert not result.valid
    assert any("does not support markdown" in error for error in result.errors)


def test_markdown_is_allowed_on_telegram():
    result = validate("**bold** " + BODY, ["#tag"], TELEGRAM)
    assert result.valid


def test_validation_never_truncates_the_input():
    body = "B" * (X.max_chars + 50)
    result = validate(body, ["#tag"], X)
    assert not result.valid
    assert len(body) == X.max_chars + 50


@pytest.mark.parametrize("profile", [X, LINKEDIN, TELEGRAM])
def test_every_profile_reports_its_own_limit(profile):
    body = "C" * (profile.max_chars + 1)
    result = validate(body, ["#tag"], profile)
    assert any(profile.display_name in error for error in result.errors)
