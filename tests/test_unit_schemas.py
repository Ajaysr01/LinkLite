from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.link import DailyClickStat, LinkCreateRequest, LinkResponse, LinkStatsResponse


def test_valid_link_create_request():
    req = LinkCreateRequest(
        long_url="https://example.com/some/path?query=1#frag",
        custom_alias="my-valid-alias_123",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert req.long_url == "https://example.com/some/path?query=1#frag"
    assert req.custom_alias == "my-valid-alias_123"
    assert req.expires_at is not None


def test_link_create_url_empty_or_whitespace():
    with pytest.raises(ValidationError) as exc:
        LinkCreateRequest(long_url="   ")
    assert "URL cannot be empty" in str(exc.value)


def test_link_create_url_exceeds_max_length():
    huge_url = "https://example.com/" + ("x" * 2050)
    with pytest.raises(ValidationError) as exc:
        LinkCreateRequest(long_url=huge_url)
    err = str(exc.value).lower()
    assert "2048" in err or "string_too_long" in err or "length" in err


def test_link_create_disallowed_schemes():
    disallowed = [
        "ftp://files.example.com",
        "javascript:alert(1)",
        "data:text/html,hello",
        "mailto:admin@example.com",
        "file:///etc/passwd",
    ]
    for url in disallowed:
        with pytest.raises(ValidationError) as exc:
            LinkCreateRequest(long_url=url)
        assert "Invalid URL scheme" in str(exc.value)


def test_link_create_missing_hostname():
    with pytest.raises(ValidationError) as exc:
        LinkCreateRequest(long_url="https://")
    assert "URL must include a valid host" in str(exc.value)


def test_link_create_loopback_rejection():
    # LinkLite BASE_URL defaults to http://localhost:8000
    loopbacks = [
        "http://localhost:8000/some-short-code",
        "http://localhost:8000/",
        "http://localhost:8000",
    ]
    for url in loopbacks:
        with pytest.raises(ValidationError) as exc:
            LinkCreateRequest(long_url=url)
        assert "loop prevention" in str(exc.value)


def test_custom_alias_length_constraints():
    # Less than 3 chars
    with pytest.raises(ValidationError) as exc:
        LinkCreateRequest(long_url="https://valid.com", custom_alias="ab")
    assert "Custom alias must be 3-32 characters long" in str(exc.value)

    # Greater than 32 chars
    with pytest.raises(ValidationError) as exc:
        LinkCreateRequest(long_url="https://valid.com", custom_alias="a" * 33)
    assert "Custom alias must be 3-32 characters long" in str(exc.value)


def test_custom_alias_character_constraints():
    invalid_aliases = [
        "hello world",
        "link@promo",
        "link#tag",
        "link/slash",
        "link.dot",
    ]
    for alias in invalid_aliases:
        with pytest.raises(ValidationError) as exc:
            LinkCreateRequest(long_url="https://valid.com", custom_alias=alias)
        assert "Custom alias must be 3-32 characters long and contain only" in str(exc.value)


def test_custom_alias_reserved_words():
    # Words that match regex ^[A-Za-z0-9_-]{3,32}$ and are reserved
    reserved = ["api", "docs", "health", "admin", "static", "redoc"]
    for word in reserved:
        with pytest.raises(ValidationError) as exc:
            LinkCreateRequest(long_url="https://valid.com", custom_alias=word)
        assert "is a reserved keyword" in str(exc.value)

        # Case-insensitive check
        with pytest.raises(ValidationError) as exc:
            LinkCreateRequest(long_url="https://valid.com", custom_alias=word.upper())
        assert "is a reserved keyword" in str(exc.value)


def test_expiration_date_in_past_rejected():
    past = datetime.now(UTC) - timedelta(minutes=5)
    with pytest.raises(ValidationError) as exc:
        LinkCreateRequest(long_url="https://valid.com", expires_at=past)
    assert "Expiration date must be in the future" in str(exc.value)


def test_response_models():
    now = datetime.now(UTC)
    resp = LinkResponse(
        code="abc123",
        short_url="http://localhost:8000/abc123",
        long_url="https://example.com",
        created_at=now,
        click_count=42,
    )
    assert resp.code == "abc123"
    assert resp.click_count == 42
    assert resp.expires_at is None

    stats_resp = LinkStatsResponse(
        code="abc123",
        total_clicks=10,
        daily_clicks=[DailyClickStat(date="2026-10-18", clicks=10)],
    )
    assert stats_resp.total_clicks == 10
    assert len(stats_resp.daily_clicks) == 1
