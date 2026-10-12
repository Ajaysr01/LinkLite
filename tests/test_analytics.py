import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal, get_redis_client
from app.main import app
from app.models.click_stat import ClickStat
from app.models.link import Link
from app.services.analytics_service import AnalyticsService


@pytest.mark.asyncio
async def test_redirect_buffers_click_in_redis_without_db_write():
    """Verifies that redirects increment the Redis click buffer immediately

    without synchronously writing or locking the PostgreSQL database.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Create short link
        create_res = await client.post(
            "/api/links",
            json={"long_url": "https://example.com/target-analytics"},
        )
        assert create_res.status_code == 201
        code = create_res.json()["code"]

        # 2. First redirect (cache miss -> loads DB, sets cache, records click)
        r1 = await client.get(f"/{code}", follow_redirects=False)
        assert r1.status_code == 302

        # 3. Second redirect (cache hit -> records click)
        r2 = await client.get(f"/{code}", follow_redirects=False)
        assert r2.status_code == 302

        # 4. Check Redis buffer has count = 2
        redis = get_redis_client()
        today_str = datetime.now(UTC).strftime("%Y-%m-%d")
        redis_count = await redis.get(f"click_buffer:{code}:{today_str}")
        assert redis_count == "2"

        # 5. Check PostgreSQL has NOT been updated yet (write-decoupled hot path)
        async with AsyncSessionLocal() as session:
            stmt = select(Link).where(Link.code == code)
            res = await session.execute(stmt)
            link = res.scalar_one()
            assert link.click_count == 0  # Still 0 in PostgreSQL!


@pytest.mark.asyncio
async def test_analytics_flush_persists_to_postgres_safely():
    """Verifies that flush_click_counters harvests in-memory counts via GETSET

    and persists them to PostgreSQL click_stats and links.click_count safely.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create link
        create_res = await client.post(
            "/api/links",
            json={"long_url": "https://example.com/flush-test"},
        )
        code = create_res.json()["code"]

        # Perform 3 redirects
        for _ in range(3):
            await client.get(f"/{code}", follow_redirects=False)

        redis = get_redis_client()

        # Run flush
        flushed = await AnalyticsService.flush_click_counters(AsyncSessionLocal, redis)
        assert flushed == 3

        # Verify DB records
        today = datetime.now(UTC).date()
        async with AsyncSessionLocal() as session:
            # Check links.click_count
            link_res = await session.execute(select(Link).where(Link.code == code))
            link = link_res.scalar_one()
            assert link.click_count == 3

            # Check click_stats table
            stat_res = await session.execute(
                select(ClickStat).where(ClickStat.code == code, ClickStat.date == today)
            )
            stat = stat_res.scalar_one()
            assert stat.count == 3

        # Subsequent flush should find 0 pending counts
        flushed_again = await AnalyticsService.flush_click_counters(AsyncSessionLocal, redis)
        assert flushed_again == 0


@pytest.mark.asyncio
async def test_concurrent_redirects_accuracy_no_lost_increments():
    """Verifies that 100 concurrent redirects increment counters atomically

    without lost updates or race conditions.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_res = await client.post(
            "/api/links",
            json={"long_url": "https://example.com/concurrent-analytics"},
        )
        code = create_res.json()["code"]

        # Launch 100 concurrent redirects
        tasks = [client.get(f"/{code}", follow_redirects=False) for _ in range(100)]
        responses = await asyncio.gather(*tasks)

        # All 100 redirects should succeed
        assert all(r.status_code == 302 for r in responses)

        # Flush buffer to PostgreSQL
        redis = get_redis_client()
        flushed = await AnalyticsService.flush_click_counters(AsyncSessionLocal, redis)
        assert flushed == 100

        # Verify exact count in PostgreSQL
        async with AsyncSessionLocal() as session:
            link_res = await session.execute(select(Link).where(Link.code == code))
            link = link_res.scalar_one()
            assert link.click_count == 100


@pytest.mark.asyncio
async def test_stats_api_combines_db_and_unflushed_buffer():
    """Verifies GET /api/links/{code}/stats returns 30-day breakdown and real-time total

    by combining PostgreSQL committed history with live un-flushed Redis buffer.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_res = await client.post(
            "/api/links",
            json={"long_url": "https://example.com/stats-api-test"},
        )
        code = create_res.json()["code"]

        # 4 redirects
        for _ in range(4):
            await client.get(f"/{code}", follow_redirects=False)

        # Before flush: Stats API reflects real-time unflushed Redis buffer
        stats_res1 = await client.get(f"/api/links/{code}/stats")
        assert stats_res1.status_code == 200
        data1 = stats_res1.json()
        assert data1["code"] == code
        assert data1["total_clicks"] == 4
        assert len(data1["daily_clicks"]) == 30
        today_entry1 = data1["daily_clicks"][-1]
        assert today_entry1["clicks"] == 4

        # Flush buffer to PostgreSQL
        redis = get_redis_client()
        await AnalyticsService.flush_click_counters(AsyncSessionLocal, redis)

        # After flush: Stats API still returns 4 total clicks seamlessly
        stats_res2 = await client.get(f"/api/links/{code}/stats")
        assert stats_res2.status_code == 200
        data2 = stats_res2.json()
        assert data2["total_clicks"] == 4
        assert data2["daily_clicks"][-1]["clicks"] == 4

        # Non-existent link returns 404
        missing_res = await client.get("/api/links/nonexistent123/stats")
        assert missing_res.status_code == 404


@pytest.mark.asyncio
async def test_flush_failure_rollback_to_redis():
    """Verifies that if PostgreSQL transaction fails during flush, counts are rolled back

    to the Redis buffer via INCRBY so zero click events are lost.
    """
    redis = get_redis_client()
    today_str = datetime.now(UTC).strftime("%Y-%m-%d")
    key = f"click_buffer:failtest:{today_str}"
    await redis.set(key, "15")

    from unittest.mock import MagicMock

    # Mock session commit to simulate database failure
    mock_session = AsyncMock()
    mock_session.commit.side_effect = Exception("Simulated DB connection error")
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session
    mock_session_factory.return_value.__aexit__.return_value = None

    flushed = await AnalyticsService.flush_click_counters(mock_session_factory, redis)
    assert flushed == 0

    # Key should have been restored back in Redis
    restored_val = await redis.get(key)
    assert restored_val == "15"


@pytest.mark.asyncio
async def test_frontend_dashboard_and_static_assets():
    """Verifies that the dashboard root / and static CSS/JS files are served properly."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Root index.html
        root_res = await client.get("/")
        assert root_res.status_code == 200
        assert "text/html" in root_res.headers.get("content-type", "")
        assert "LinkLite" in root_res.text
        assert "Chart.js" in root_res.text or "chart.umd.min.js" in root_res.text

        # Static CSS
        css_res = await client.get("/static/styles.css")
        assert css_res.status_code == 200
        assert "text/css" in css_res.headers.get("content-type", "")

        # Static JS
        js_res = await client.get("/static/app.js")
        assert js_res.status_code == 200
        assert "javascript" in js_res.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_record_click_redis_failure_fails_gracefully():
    """Verifies that record_click logs a warning without raising an exception

    if Redis encounters an error.
    """
    mock_redis = AsyncMock()
    mock_redis.pipeline.side_effect = Exception("Simulated Redis outage")
    # Should not raise exception
    await AnalyticsService.record_click(mock_redis, "dummycode")


@pytest.mark.asyncio
async def test_flush_handles_empty_buffer_and_scan_errors():
    """Verifies flush returns 0 safely when no keys are buffered or scan fails."""
    redis = get_redis_client()
    flushed = await AnalyticsService.flush_click_counters(AsyncSessionLocal, redis)
    assert flushed == 0

    # Test scan error
    mock_redis = AsyncMock()
    mock_redis.scan_iter.side_effect = Exception("Simulated scan failure")
    flushed_err = await AnalyticsService.flush_click_counters(AsyncSessionLocal, mock_redis)
    assert flushed_err == 0


@pytest.mark.asyncio
async def test_analytics_worker_start_and_stop():
    """Verifies that start_analytics_worker and stop_analytics_worker

    manage background task cleanly.
    """
    from app.services.analytics_service import start_analytics_worker, stop_analytics_worker

    task = start_analytics_worker(AsyncSessionLocal, interval_seconds=1)
    assert task is not None
    assert not task.done()

    # Allow it to run a brief tick
    await asyncio.sleep(0.05)

    await stop_analytics_worker()
    assert task.done()


@pytest.mark.asyncio
async def test_get_link_endpoint_reflects_unflushed_clicks():
    """Verifies GET /api/links/{code} includes in-flight unflushed Redis clicks."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_res = await client.post(
            "/api/links",
            json={"long_url": "https://example.com/unflushed-detail-test"},
        )
        code = create_res.json()["code"]

        # 2 redirects
        await client.get(f"/{code}", follow_redirects=False)
        await client.get(f"/{code}", follow_redirects=False)

        # GET /api/links/{code} should report click_count = 2 immediately
        link_res = await client.get(f"/api/links/{code}")
        assert link_res.status_code == 200
        assert link_res.json()["click_count"] == 2
