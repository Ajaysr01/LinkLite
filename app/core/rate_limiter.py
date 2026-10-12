import time
from typing import Any

from fastapi import HTTPException, Request, Response, status
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.logging import logger
from app.db.session import get_redis_client

settings = get_settings()

# Atomic Redis Lua script implementing a sliding window rate limiter
SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local clear_before = now - window

-- Remove requests outside the current sliding window
redis.call('ZREMRANGEBYSCORE', key, 0, clear_before)

-- Count requests currently in the window
local current_count = redis.call('ZCARD', key)

if current_count < limit then
    -- Atomically record this request
    local seq_key = key .. ':seq'
    local seq = redis.call('INCR', seq_key)
    redis.call('ZADD', key, now, now .. ':' .. tostring(seq))
    redis.call('EXPIRE', key, math.ceil(window))
    redis.call('EXPIRE', seq_key, math.ceil(window))

    local remaining = limit - current_count - 1
    local reset_time = math.ceil(now + window)
    return {1, remaining, reset_time, 0}
else
    -- Rate limit exceeded: calculate retry_after from oldest entry in window
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = 1
    if oldest and #oldest >= 2 then
        local oldest_time = tonumber(oldest[2])
        retry_after = math.max(1, math.ceil(oldest_time + window - now))
    end
    local reset_time = math.ceil(now + retry_after)
    return {0, 0, reset_time, retry_after}
end
"""


def get_client_ip(request: Request) -> str:
    """Extracts client IP address.

    Honors X-Forwarded-For ONLY when TRUST_PROXY_HEADERS is explicitly True.
    If untrusted, reads directly from socket (request.client.host) to prevent
    IP spoofing and rate-limit evasion.
    """
    if settings.TRUST_PROXY_HEADERS:
        x_forwarded_for = request.headers.get("x-forwarded-for")
        if x_forwarded_for:
            # Client IP is the first entry in the comma-separated chain
            return x_forwarded_for.split(",")[0].strip()

    if request.client and request.client.host:
        return request.client.host

    return "127.0.0.1"


class RateLimiter:
    """FastAPI dependency for Redis-backed sliding window rate limiting."""

    def __init__(
        self,
        limit_per_window: int,
        window_seconds: int = 60,
        scope: str = "default",
    ):
        self.limit = limit_per_window
        self.window = window_seconds
        self.scope = scope

    async def __call__(
        self,
        request: Request,
        response: Response,
    ) -> None:
        client_ip = get_client_ip(request)
        rate_key = f"rate_limit:{self.scope}:{client_ip}"
        now = time.time()

        try:
            redis = get_redis_client()
            res: Any = await redis.eval(
                SLIDING_WINDOW_LUA,
                1,
                rate_key,
                str(now),
                str(self.window),
                str(self.limit),
            )
            # Result: {allowed, remaining, reset_time, retry_after}
            allowed = int(res[0])
            remaining = int(res[1])
            reset_time = int(res[2])
            retry_after = int(res[3])

            # Populate telemetry headers on response
            response.headers["X-RateLimit-Limit"] = str(self.limit)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            response.headers["X-RateLimit-Reset"] = str(reset_time)

            if allowed == 0:
                response.headers["Retry-After"] = str(retry_after)
                logger.warning(
                    "Rate limit exceeded",
                    extra={
                        "extra_fields": {
                            "client_ip": client_ip,
                            "scope": self.scope,
                            "limit": self.limit,
                            "retry_after": retry_after,
                        }
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded. Please try again later.",
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": str(self.limit),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(reset_time),
                    },
                )

        except HTTPException:
            # Re-raise intended 429
            raise
        except (RedisError, Exception) as exc:
            # Graceful Fail-Open: Allow request when Redis is offline
            logger.warning(
                "REDIS_UNAVAILABLE: Rate limiter bypassed (fail-open)",
                exc_info=exc,
                extra={"extra_fields": {"client_ip": client_ip, "scope": self.scope}},
            )
            response.headers["X-RateLimit-Limit"] = str(self.limit)
            response.headers["X-RateLimit-Remaining"] = str(self.limit)
            response.headers["X-RateLimit-Reset"] = str(int(now + self.window))
