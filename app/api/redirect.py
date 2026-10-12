from datetime import UTC, datetime

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.rate_limiter import RateLimiter
from app.db.session import get_db, get_redis_client
from app.services.analytics_service import AnalyticsService
from app.services.cache_service import CacheService
from app.services.link_service import LinkService

router = APIRouter(tags=["Redirect"])
settings = get_settings()

redirect_limiter = RateLimiter(
    limit_per_window=settings.RATE_LIMIT_REDIRECT_PER_MINUTE,
    window_seconds=60,
    scope="redirect",
)


@router.get(
    "/{code}",
    status_code=status.HTTP_302_FOUND,
    dependencies=[Depends(redirect_limiter)],
    summary="Redirect to target URL",
    description=(
        "Redirects a short code to its destination URL via HTTP 302 Found. "
        "Utilizes Redis cache-aside with negative caching and Postgres fallback."
    ),
    response_class=RedirectResponse,
)
async def redirect_link(
    code: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_client),
) -> RedirectResponse:
    # 1. Check Redis cache (hot path)
    cached = await CacheService.get_link(redis, code)
    if cached:
        # Check cached expiration if present
        expires_at_str = cached.get("expires_at")
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            now = datetime.now(UTC)
            expiry_utc = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
            if now >= expiry_utc:
                await CacheService.delete_link(redis, code)
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail="Link has expired",
                )

        await AnalyticsService.record_click(redis, code)
        return RedirectResponse(
            url=cached["long_url"],
            status_code=status.HTTP_302_FOUND,
        )

    # 2. Check Negative Cache (prevent cache penetration)
    if await CacheService.get_negative(redis, code):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Link not found",
        )

    # 3. Cache Miss: Query PostgreSQL
    link = await LinkService.get_link_raw(db, code)

    # Unknown or soft-deleted
    if link is None or link.is_deleted:
        await CacheService.set_negative(redis, code)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Link not found",
        )

    # Expired in database
    if link.is_expired():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Link has expired",
        )

    # Active link: Populate Redis cache, record click, and redirect
    await CacheService.set_link(redis, code, link.long_url, link.expires_at)
    await AnalyticsService.record_click(redis, code)
    return RedirectResponse(
        url=link.long_url,
        status_code=status.HTTP_302_FOUND,
    )
