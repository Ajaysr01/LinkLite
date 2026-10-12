import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db, get_redis_client
from app.schemas.link import LinkCreateRequest, LinkResponse
from app.services.link_service import AliasAlreadyExistsError, LinkService

router = APIRouter(prefix="/api/links", tags=["Links"])
settings = get_settings()


@router.post(
    "",
    response_model=LinkResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a short link",
    description=(
        "Shortens a destination URL, supporting optional custom alias and expiration timestamp."
    ),
)
async def create_link(
    payload: LinkCreateRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis_client),
) -> LinkResponse:
    try:
        link = await LinkService.create_link(session=db, redis=redis, data=payload)
    except AliasAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Custom alias '{exc.alias}' is already in use.",
        ) from exc

    short_url = f"{settings.BASE_URL.rstrip('/')}/{link.code}"
    return LinkResponse(
        code=link.code,
        short_url=short_url,
        long_url=link.long_url,
        created_at=link.created_at,
        expires_at=link.expires_at,
        click_count=link.click_count,
    )
