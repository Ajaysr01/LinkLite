from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.session import get_db, get_redis_client

router = APIRouter(tags=["Health"])


@router.get("/health", summary="Health check verifying database and cache connectivity")
async def health_check(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Probes PostgreSQL and Redis to verify overall operational readiness."""
    health_status: dict[str, str] = {
        "status": "healthy",
        "postgres": "ok",
        "redis": "ok",
    }
    is_healthy = True

    # 1. Check PostgreSQL
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Health check failed on PostgreSQL connection", exc_info=exc)
        health_status["postgres"] = "error"
        is_healthy = False

    # 2. Check Redis
    try:
        redis = get_redis_client()
        pong = await redis.ping()
        if not pong:
            health_status["redis"] = "error"
            is_healthy = False
    except Exception as exc:
        logger.warning("Health check failed on Redis connection", exc_info=exc)
        health_status["redis"] = "error"
        is_healthy = False

    if not is_healthy:
        health_status["status"] = "degraded"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return health_status
