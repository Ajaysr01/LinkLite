import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from app.db.session import get_redis_client
from app.main import app


@pytest.mark.asyncio
async def test_redirect_cache_miss_and_cache_hit():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as ac:
        # 1. Create a short link
        create_res = await ac.post(
            "/api/links",
            json={"long_url": "https://fastapi.tiangolo.com/features/"},
        )
        assert create_res.status_code == 201
        code = create_res.json()["code"]

        redis = get_redis_client()
        # Verify cache is empty before first redirect
        cached_before = await redis.get(f"link:{code}")
        assert cached_before is None

        # 2. First redirect: Cache Miss -> loads from DB -> sets cache
        res1 = await ac.get(f"/{code}")
        assert res1.status_code == 302
        assert res1.headers["location"] == "https://fastapi.tiangolo.com/features/"

        # Verify Redis cache is now populated with TTL
        cached_after = await redis.get(f"link:{code}")
        assert cached_after is not None
        cached_data = json.loads(cached_after)
        assert cached_data["long_url"] == "https://fastapi.tiangolo.com/features/"

        ttl = await redis.ttl(f"link:{code}")
        assert ttl > 0  # TTL is set

        # 3. Second redirect: Cache Hit
        res2 = await ac.get(f"/{code}")
        assert res2.status_code == 302
        assert res2.headers["location"] == "https://fastapi.tiangolo.com/features/"


@pytest.mark.asyncio
async def test_negative_caching_for_unknown_code():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as ac:
        unknown_code = "does-not-exist-xyz"
        redis = get_redis_client()

        # 1. First probe: returns 404 and writes to negative cache
        res1 = await ac.get(f"/{unknown_code}")
        assert res1.status_code == 404
        assert res1.json()["detail"] == "Link not found"

        # Check negative cache key exists in Redis
        is_neg = await redis.get(f"link_not_found:{unknown_code}")
        assert is_neg == "1"

        # 2. Second probe: returns 404 from negative cache
        res2 = await ac.get(f"/{unknown_code}")
        assert res2.status_code == 404

        # 3. Create link with that alias: negative cache should be evicted
        create_res = await ac.post(
            "/api/links",
            json={
                "long_url": "https://example.com/restored",
                "custom_alias": unknown_code,
            },
        )
        assert create_res.status_code == 201

        # Verify negative cache key was deleted
        assert await redis.get(f"link_not_found:{unknown_code}") is None

        # 4. Now redirect succeeds with 302
        res3 = await ac.get(f"/{unknown_code}")
        assert res3.status_code == 302
        assert res3.headers["location"] == "https://example.com/restored"


@pytest.mark.asyncio
async def test_redirect_expired_link_returns_410():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as ac:
        # Create link expiring in 1 second
        near_expiry = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        create_res = await ac.post(
            "/api/links",
            json={
                "long_url": "https://example.com/ephemeral",
                "expires_at": near_expiry,
            },
        )
        assert create_res.status_code == 201
        code = create_res.json()["code"]

        # Manually overwrite the cached or db expiration to the past to test 410
        redis = get_redis_client()
        past_expiry = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        await redis.set(
            f"link:{code}",
            json.dumps({"long_url": "https://example.com/ephemeral", "expires_at": past_expiry}),
        )

        # GET /{code} must return 410 Gone and clean the cache
        res = await ac.get(f"/{code}")
        assert res.status_code == 410
        assert res.json()["detail"] == "Link has expired"

        # Cache key should be cleaned
        assert await redis.get(f"link:{code}") is None


@pytest.mark.asyncio
async def test_get_link_details_and_soft_delete():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as ac:
        # 1. Create link
        create_res = await ac.post(
            "/api/links",
            json={"long_url": "https://kernel.org", "custom_alias": "kernel-org"},
        )
        assert create_res.status_code == 201

        # 2. GET /api/links/{code} link details
        details_res = await ac.get("/api/links/kernel-org")
        assert details_res.status_code == 200
        data = details_res.json()
        assert data["code"] == "kernel-org"
        assert data["long_url"] == "https://kernel.org"
        assert data["click_count"] == 0

        # Prime the redirect cache
        redirect_res = await ac.get("/kernel-org")
        assert redirect_res.status_code == 302

        redis = get_redis_client()
        assert await redis.get("link:kernel-org") is not None

        # 3. DELETE /api/links/{code}
        del_res = await ac.delete("/api/links/kernel-org")
        assert del_res.status_code == 204

        # Verify Redis cache is evicted
        assert await redis.get("link:kernel-org") is None

        # 4. Subsequent GET /{code} returns 404
        post_del_redirect = await ac.get("/kernel-org")
        assert post_del_redirect.status_code == 404

        # 5. Subsequent GET /api/links/{code} returns 404
        post_del_details = await ac.get("/api/links/kernel-org")
        assert post_del_details.status_code == 404

        # 6. Second DELETE returns 404
        second_del = await ac.delete("/api/links/kernel-org")
        assert second_del.status_code == 404


@pytest.mark.asyncio
async def test_redis_down_fallback_to_postgres():
    """Verify that when Redis is completely down/unreachable,

    redirects and link creations degrade gracefully (fail open) to Postgres.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as ac:
        # Create a link in DB first
        create_res = await ac.post(
            "/api/links",
            json={"long_url": "https://degraded-mode.example.com", "custom_alias": "db-fallback"},
        )
        assert create_res.status_code == 201

        # Simulate Redis connection failure on redirect
        failing_redis = AsyncMock(spec=aioredis.Redis)
        failing_redis.get.side_effect = RedisError("Connection refused by Redis server")
        failing_redis.set.side_effect = RedisError("Connection refused by Redis server")
        failing_redis.delete.side_effect = RedisError("Connection refused by Redis server")

        with patch("app.api.redirect.get_redis_client", return_value=failing_redis):
            res = await ac.get("/db-fallback")
            # Must succeed with 302 by querying Postgres directly!
            assert res.status_code == 302
            assert res.headers["location"] == "https://degraded-mode.example.com"
