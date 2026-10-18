from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.main import app
from app.services.cache_service import CacheService
from app.services.link_service import LinkService

settings = get_settings()


def test_calculate_ttl_past_and_future_expiry():
    # Past expiry should return 0
    past = datetime.now(UTC) - timedelta(seconds=10)
    assert CacheService.calculate_ttl(past) == 0

    # Near future expiry should cap TTL
    near_future = datetime.now(UTC) + timedelta(seconds=300)
    ttl = CacheService.calculate_ttl(near_future)
    assert 0 < ttl <= 300

    # Distant future expiry capped by REDIS_CACHE_TTL_SECONDS (+/- jitter)
    distant_future = datetime.now(UTC) + timedelta(days=365)
    ttl_distant = CacheService.calculate_ttl(distant_future)
    base = settings.REDIS_CACHE_TTL_SECONDS
    assert (base * 0.90) <= ttl_distant <= (base * 1.10)


@pytest.mark.asyncio
async def test_cache_service_redis_error_handling():
    """Verify that all CacheService methods fail open/gracefully when Redis errors occur."""
    mock_redis = AsyncMock()
    mock_redis.get.side_effect = RedisError("Simulated Redis Failure")
    mock_redis.set.side_effect = RedisError("Simulated Redis Failure")
    mock_redis.delete.side_effect = RedisError("Simulated Redis Failure")

    # get_link returns None on error
    assert await CacheService.get_link(mock_redis, "test_code") is None

    # set_link does not raise exception
    await CacheService.set_link(mock_redis, "test_code", "https://example.com")

    # get_negative returns False on error
    assert await CacheService.get_negative(mock_redis, "test_code") is False

    # set_negative does not raise exception
    await CacheService.set_negative(mock_redis, "test_code")

    # delete_link does not raise exception
    await CacheService.delete_link(mock_redis, "test_code")


@pytest.mark.asyncio
async def test_link_service_nonexistent_lookups():
    """Verify LinkService returns None/False when interacting with non-existent links."""
    mock_redis = AsyncMock()
    async with AsyncSessionLocal() as session:
        # Non-existent link lookup
        link = await LinkService.get_link_by_code(session, "completely_nonexistent_code")
        assert link is None

        # Non-existent link soft delete returns False
        deleted = await LinkService.soft_delete_link(
            session, mock_redis, "completely_nonexistent_code"
        )
        assert deleted is False


@pytest.mark.asyncio
async def test_api_links_404_responses():
    """Verify GET and DELETE /api/links/{code} return 404 for unknown codes."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # GET details for missing code
        r1 = await client.get("/api/links/missing_code_999")
        assert r1.status_code == 404
        assert r1.json()["detail"] == "Link not found"

        # DELETE for missing code
        r2 = await client.delete("/api/links/missing_code_999")
        assert r2.status_code == 404
        assert r2.json()["detail"] == "Link not found"
