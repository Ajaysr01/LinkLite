import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.api.links import create_link_limiter
from app.api.redirect import redirect_limiter
from app.core.rate_limiter import RateLimiter
from app.db.session import AsyncSessionLocal, get_redis_client
from app.main import app
from app.models.click_stat import ClickStat
from app.models.link import Link
from app.services.analytics_service import AnalyticsService


@pytest.mark.asyncio
async def test_concurrency_50_parallel_creates_same_alias():
    """Concurrency Requirement 1:

    50 parallel requests attempting to register the exact same custom alias
    must result in exactly ONE 201 Created and 49 409 Conflict responses.
    Enforced via PostgreSQL UNIQUE constraint on `code`, NOT check-then-insert.
    """
    alias = "contest-flash-sale-2026"
    # Override rate limiter for this endpoint so all 50 reach the database layer
    app.dependency_overrides[create_link_limiter] = lambda: None

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

            async def send_create(idx: int):
                return await client.post(
                    "/api/links",
                    json={
                        "long_url": f"https://example.com/target-page/{idx}",
                        "custom_alias": alias,
                    },
                )

            # Fire all 50 requests simultaneously
            responses = await asyncio.gather(*(send_create(i) for i in range(50)))

            statuses = [r.status_code for r in responses]
            success_count = statuses.count(201)
            conflict_count = statuses.count(409)

            assert success_count == 1, (
                f"Expected exactly 1 success (201), but got {success_count}. Statuses: {statuses}"
            )
            assert conflict_count == 49, (
                f"Expected exactly 49 conflicts (409), but got {conflict_count}"
            )

            # Verify in PostgreSQL that exactly one link exists with this alias
            async with AsyncSessionLocal() as session:
                stmt = select(Link).where(Link.code == alias)
                res = await session.execute(stmt)
                links = res.scalars().all()
                assert len(links) == 1
    finally:
        app.dependency_overrides.pop(create_link_limiter, None)


@pytest.mark.asyncio
async def test_concurrency_100_parallel_redirects_accurate_counter():
    """Concurrency Requirement 2:

    100 parallel redirects to the same link must increment the Redis counter
    exactly 100 times, and after a background flush, persist exactly 100 clicks
    in both `links.click_count` and `click_stats` without lost updates.
    """
    # Override redirect limiter so all 100 reach the handler
    app.dependency_overrides[redirect_limiter] = lambda: None

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Create a short link
            create_res = await client.post(
                "/api/links",
                json={"long_url": "https://example.org/concurrent-target"},
            )
            assert create_res.status_code == 201
            code = create_res.json()["code"]

            # 2. Fire 100 parallel redirects
            async def send_redirect():
                return await client.get(f"/{code}", follow_redirects=False)

            responses = await asyncio.gather(*(send_redirect() for _ in range(100)))

            assert all(r.status_code == 302 for r in responses), (
                "All 100 redirect requests should return 302 Found"
            )

            # 3. Harvest and flush from Redis to PostgreSQL
            redis = get_redis_client()
            flushed = await AnalyticsService.flush_click_counters(AsyncSessionLocal, redis)
            assert flushed == 100, f"Expected 100 flushed click events, got {flushed}"

            # 4. Verify in PostgreSQL
            async with AsyncSessionLocal() as session:
                link_res = await session.execute(select(Link).where(Link.code == code))
                link = link_res.scalar_one()
                assert link.click_count == 100, (
                    f"Expected link.click_count to be 100, got {link.click_count}"
                )

                stat_res = await session.execute(select(ClickStat).where(ClickStat.code == code))
                stat = stat_res.scalar_one()
                assert stat.count == 100, f"Expected click_stats.count to be 100, got {stat.count}"
    finally:
        app.dependency_overrides.pop(redirect_limiter, None)


@pytest.mark.asyncio
async def test_concurrency_rate_limiter_allows_exact_configured_quota():
    """Concurrency Requirement 3:

    Under heavy parallel async load, the rate limiter allows exactly the configured
    quota and blocks all excess requests with HTTP 429.
    Tested against a dedicated limiter with quota=10 under 40 parallel requests.
    """
    from fastapi import FastAPI, Request, Response

    test_app = FastAPI()
    quota = 10
    limiter = RateLimiter(limit_per_window=quota, window_seconds=60, scope="test_concurrency_quota")

    @test_app.get("/burst-test")
    async def burst_endpoint(request: Request, response: Response):
        await limiter(request, response)
        return {"status": "ok"}

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Launch 40 parallel requests
        responses = await asyncio.gather(*(client.get("/burst-test") for _ in range(40)))

        statuses = [r.status_code for r in responses]
        allowed = statuses.count(200)
        blocked = statuses.count(429)

        assert allowed == quota, f"Expected exactly {quota} requests allowed, but got {allowed}"
        assert blocked == 40 - quota, (
            f"Expected {40 - quota} requests blocked with 429, but got {blocked}"
        )
