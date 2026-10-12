import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_create_link_auto_generated():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(
            "/api/links",
            json={"long_url": "https://fastapi.tiangolo.com/tutorial/"},
        )
        assert response.status_code == 201
        data = response.json()
        assert "code" in data
        assert len(data["code"]) >= 6  # Minimum 6 characters guaranteed by offset
        assert data["long_url"] == "https://fastapi.tiangolo.com/tutorial/"
        assert data["short_url"] == f"http://localhost:8000/{data['code']}"
        assert data["expires_at"] is None
        assert "x-request-id" in response.headers


@pytest.mark.asyncio
async def test_create_link_custom_alias_and_expiry():
    future_expiry = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    alias = "custom-my-link"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(
            "/api/links",
            json={
                "long_url": "https://python.org",
                "custom_alias": alias,
                "expires_at": future_expiry,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["code"] == alias
        assert data["short_url"] == f"http://localhost:8000/{alias}"
        assert data["expires_at"] is not None


@pytest.mark.asyncio
async def test_create_link_alias_conflict_409():
    alias = "unique-promo-code"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # First creation succeeds
        res1 = await ac.post(
            "/api/links",
            json={"long_url": "https://github.com", "custom_alias": alias},
        )
        assert res1.status_code == 201

        # Second creation with identical alias must return 409 Conflict
        res2 = await ac.post(
            "/api/links",
            json={"long_url": "https://gitlab.com", "custom_alias": alias},
        )
        assert res2.status_code == 409
        assert f"Custom alias '{alias}' is already in use" in res2.json()["detail"]


@pytest.mark.asyncio
async def test_concurrent_alias_creation():
    """Verify concurrency correctness: 20 simultaneous requests for the same alias

    must yield exactly one 201 and 19 409 conflicts.
    """
    alias = "high-concurrency-alias"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:

        async def make_request(idx: int):
            return await ac.post(
                "/api/links",
                json={
                    "long_url": f"https://example.com/item/{idx}",
                    "custom_alias": alias,
                },
            )

        responses = await asyncio.gather(*(make_request(i) for i in range(20)))

        statuses = [r.status_code for r in responses]
        success_count = statuses.count(201)
        conflict_count = statuses.count(409)

        assert success_count == 1, f"Expected exactly 1 success, got {success_count}"
        assert conflict_count == 19, f"Expected 19 conflicts, got {conflict_count}"


@pytest.mark.parametrize(
    "invalid_url",
    [
        "not-a-valid-url",
        "javascript:alert('XSS')",
        "data:text/html,<h1>test</h1>",
        "ftp://mirror.ox.ac.uk/sites/",
        "http://localhost:8000/loopback",
        "http://localhost:8000/subpath/nested",
        "",
        "https://" + ("a" * 2050) + ".com",
    ],
)
@pytest.mark.asyncio
async def test_url_validation_rejections(invalid_url):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(
            "/api/links",
            json={"long_url": invalid_url},
        )
        assert response.status_code == 422


@pytest.mark.parametrize(
    "invalid_alias",
    [
        "ab",  # too short (< 3)
        "a" * 33,  # too long (> 32)
        "api",  # reserved
        "docs",  # reserved
        "health",  # reserved
        "admin",  # reserved
        "has space",  # invalid chars
        "special!char",  # invalid chars
    ],
)
@pytest.mark.asyncio
async def test_custom_alias_rejections(invalid_alias):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(
            "/api/links",
            json={"long_url": "https://valid.com", "custom_alias": invalid_alias},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_past_expiration_rejected():
    past_expiry = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(
            "/api/links",
            json={
                "long_url": "https://valid.com",
                "expires_at": past_expiry,
            },
        )
        assert response.status_code == 422
