from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

settings = get_settings()

# Async PostgreSQL engine and sessionmaker
engine: AsyncEngine = create_async_engine(
    settings.async_database_url,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# Async Redis client pool
redis_client: aioredis.Redis | None = None


def get_redis_client() -> aioredis.Redis:
    """Returns singleton Async Redis client instance."""
    global redis_client
    if redis_client is None:
        redis_client = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
    return redis_client


async def close_redis_client() -> None:
    """Closes Redis client connections gracefully."""
    global redis_client
    if redis_client is not None:
        await redis_client.aclose()
        redis_client = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
