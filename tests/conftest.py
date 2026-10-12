import pytest
from sqlalchemy import text

from app.db.session import AsyncSessionLocal, close_redis_client, engine, get_redis_client


@pytest.fixture(autouse=True)
async def cleanup_database_and_connections():
    """Truncates database tables and flushes redis before each test,

    and disposes connection pools afterwards.
    """
    try:
        async with AsyncSessionLocal() as session:
            stmt = text("TRUNCATE TABLE click_stats, links RESTART IDENTITY CASCADE")
            await session.execute(stmt)
            await session.commit()
    except Exception:
        pass

    try:
        redis = get_redis_client()
        await redis.flushdb()
    except Exception:
        pass

    yield

    await engine.dispose()
    await close_redis_client()
