import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.rate_limiter import RateLimiter, get_client_ip
from app.db.session import get_redis_client
from app.main import app

settings = get_settings()


@pytest.mark.asyncio
async def test_rate_limiter_allows_and_blocks():
    """Verify rate limiter allows up to limit and returns 429 when exhausted."""
    test_app = FastAPI()
    limiter = RateLimiter(limit_per_window=3, window_seconds=60, scope="test_allow_block")

    @test_app.get("/limited")
    async def limited_endpoint(request: Request, response: Response):
        await limiter(request, response)
        return {"status": "ok"}

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        # Request 1: allowed, remaining 2
        r1 = await ac.get("/limited")
        assert r1.status_code == 200
        assert r1.headers["X-RateLimit-Limit"] == "3"
        assert r1.headers["X-RateLimit-Remaining"] == "2"

        # Request 2: allowed, remaining 1
        r2 = await ac.get("/limited")
        assert r2.status_code == 200
        assert r2.headers["X-RateLimit-Remaining"] == "1"

        # Request 3: allowed, remaining 0
        r3 = await ac.get("/limited")
        assert r3.status_code == 200
        assert r3.headers["X-RateLimit-Remaining"] == "0"

        # Request 4: Blocked with 429
        r4 = await ac.get("/limited")
        assert r4.status_code == 429
        assert "Retry-After" in r4.headers
        assert r4.headers["X-RateLimit-Remaining"] == "0"
        assert int(r4.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_rate_limiter_concurrent_exactness():
    """Verify that under high concurrency, an atomic Lua script guarantees

    that exactly the configured number of requests are permitted.
    """
    test_app = FastAPI()
    exact_limit = 5
    limiter = RateLimiter(limit_per_window=exact_limit, window_seconds=60, scope="test_exactness")

    @test_app.get("/concurrent")
    async def concurrent_endpoint(request: Request, response: Response):
        await limiter(request, response)
        return {"status": "ok"}

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        # Fire 15 simultaneous requests
        responses = await asyncio.gather(*(ac.get("/concurrent") for _ in range(15)))

        status_codes = [r.status_code for r in responses]
        allowed_count = status_codes.count(200)
        blocked_count = status_codes.count(429)

        assert allowed_count == exact_limit, f"Expected {exact_limit} allowed, got {allowed_count}"
        assert blocked_count == 15 - exact_limit


@pytest.mark.asyncio
async def test_ip_spoofing_protection():
    """Verify X-Forwarded-For is ignored when TRUST_PROXY_HEADERS is False."""
    test_app = FastAPI()
    limiter = RateLimiter(limit_per_window=2, window_seconds=60, scope="test_spoof")

    @test_app.get("/ip-check")
    async def ip_check(request: Request, response: Response):
        await limiter(request, response)
        return {"ip": get_client_ip(request)}

    # When untrusted (default), spoofed X-Forwarded-For is ignored
    settings.TRUST_PROXY_HEADERS = False
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        r1 = await ac.get("/ip-check", headers={"X-Forwarded-For": "1.2.3.4"})
        r2 = await ac.get("/ip-check", headers={"X-Forwarded-For": "5.6.7.8"})
        # 3rd request from same client with another fake IP must be blocked!
        r3 = await ac.get("/ip-check", headers={"X-Forwarded-For": "9.10.11.12"})

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429, "Client bypassed rate limiter via fake X-Forwarded-For!"

    # When trusted, each distinct forwarded IP gets its own quota
    settings.TRUST_PROXY_HEADERS = True
    redis = get_redis_client()
    await redis.flushdb()

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        t1 = await ac.get("/ip-check", headers={"X-Forwarded-For": "100.0.0.1"})
        t2 = await ac.get("/ip-check", headers={"X-Forwarded-For": "100.0.0.2"})
        t3 = await ac.get("/ip-check", headers={"X-Forwarded-For": "100.0.0.3"})

        assert t1.status_code == 200
        assert t2.status_code == 200
        assert t3.status_code == 200

    settings.TRUST_PROXY_HEADERS = False


@pytest.mark.asyncio
async def test_rate_limiter_fail_open_on_redis_error():
    """Verify that when Redis is unreachable, the rate limiter fails open."""
    test_app = FastAPI()
    limiter = RateLimiter(limit_per_window=1, window_seconds=60, scope="test_fail_open")

    @test_app.get("/resilient")
    async def resilient_endpoint(request: Request, response: Response):
        await limiter(request, response)
        return {"status": "ok"}

    failing_redis = AsyncMock(spec=aioredis.Redis)
    failing_redis.eval.side_effect = RedisError("Redis cluster unreachable")

    with patch("app.core.rate_limiter.get_redis_client", return_value=failing_redis):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            # Both requests should succeed because it fails open
            r1 = await ac.get("/resilient")
            r2 = await ac.get("/resilient")
            assert r1.status_code == 200
            assert r2.status_code == 200


@pytest.mark.asyncio
async def test_api_endpoints_rate_limit_headers():
    """Verify real application endpoints return X-RateLimit headers."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as ac:
        res = await ac.post(
            "/api/links",
            json={"long_url": "https://fastapi.tiangolo.com/"},
        )
        assert res.status_code == 201
        assert "X-RateLimit-Limit" in res.headers
        assert "X-RateLimit-Remaining" in res.headers
        assert "X-RateLimit-Reset" in res.headers
