import json
import random
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.logging import logger

settings = get_settings()


class CacheService:
    @staticmethod
    def _link_key(code: str) -> str:
        return f"link:{code}"

    @staticmethod
    def _negative_key(code: str) -> str:
        return f"link_not_found:{code}"

    @staticmethod
    def calculate_ttl(expires_at: datetime | None) -> int:
        """Calculates cache TTL with random jitter to prevent cache stampedes,

        capped by the link's remaining expiration time.
        """
        base_ttl = settings.REDIS_CACHE_TTL_SECONDS
        # Apply +/- 5% jitter (e.g. for 86400s, jitter is +/- 4320s)
        jitter = random.randint(-int(base_ttl * 0.05), int(base_ttl * 0.05))
        ttl = max(60, base_ttl + jitter)

        if expires_at:
            now = datetime.now(UTC)
            expiry_utc = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
            remaining_seconds = int((expiry_utc - now).total_seconds())
            if remaining_seconds <= 0:
                return 0
            ttl = min(ttl, remaining_seconds)

        return ttl

    @classmethod
    async def get_link(cls, redis: aioredis.Redis, code: str) -> dict[str, Any] | None:
        """Retrieves cached link payload. Fails open on Redis errors."""
        try:
            val = await redis.get(cls._link_key(code))
            if val is not None:
                return json.loads(val)
            return None
        except (RedisError, Exception) as exc:
            logger.warning(
                "REDIS_UNAVAILABLE: Failed to get cached link, falling back to DB",
                exc_info=exc,
                extra={"extra_fields": {"code": code, "error": str(exc)}},
            )
            return None

    @classmethod
    async def set_link(
        cls,
        redis: aioredis.Redis,
        code: str,
        long_url: str,
        expires_at: datetime | None = None,
    ) -> None:
        """Stores link payload in Redis with jittered/capped TTL."""
        ttl = cls.calculate_ttl(expires_at)
        if ttl <= 0:
            return

        payload = {
            "long_url": long_url,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
        try:
            await redis.set(cls._link_key(code), json.dumps(payload), ex=ttl)
        except (RedisError, Exception) as exc:
            logger.warning(
                "REDIS_UNAVAILABLE: Failed to populate link cache",
                exc_info=exc,
                extra={"extra_fields": {"code": code, "error": str(exc)}},
            )

    @classmethod
    async def get_negative(cls, redis: aioredis.Redis, code: str) -> bool:
        """Returns True if the code is in the negative cache (known not found)."""
        try:
            val = await redis.get(cls._negative_key(code))
            return val is not None
        except (RedisError, Exception) as exc:
            logger.warning(
                "REDIS_UNAVAILABLE: Failed to check negative cache",
                exc_info=exc,
                extra={"extra_fields": {"code": code, "error": str(exc)}},
            )
            return False

    @classmethod
    async def set_negative(cls, redis: aioredis.Redis, code: str) -> None:
        """Caches a 404 lookup with a short TTL to protect against cache penetration."""
        try:
            await redis.set(
                cls._negative_key(code),
                "1",
                ex=settings.REDIS_NEGATIVE_CACHE_TTL_SECONDS,
            )
        except (RedisError, Exception) as exc:
            logger.warning(
                "REDIS_UNAVAILABLE: Failed to set negative cache",
                exc_info=exc,
                extra={"extra_fields": {"code": code, "error": str(exc)}},
            )

    @classmethod
    async def delete_link(cls, redis: aioredis.Redis, code: str) -> None:
        """Invalidates both positive and negative cache entries for a code."""
        try:
            await redis.delete(cls._link_key(code), cls._negative_key(code))
        except (RedisError, Exception) as exc:
            logger.warning(
                "REDIS_UNAVAILABLE: Failed to invalidate cache keys",
                exc_info=exc,
                extra={"extra_fields": {"code": code, "error": str(exc)}},
            )
