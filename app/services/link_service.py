import redis.asyncio as aioredis
from sqlalchemy import Sequence, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base62 import encode_id_with_offset
from app.core.logging import logger
from app.models.link import Link
from app.schemas.link import LinkCreateRequest

# Sequence object for auto-increment IDs
links_id_seq = Sequence("links_id_seq")


class AliasAlreadyExistsError(Exception):
    """Raised when a custom alias is already occupied."""

    def __init__(self, alias: str):
        self.alias = alias
        super().__init__(f"Custom alias '{alias}' is already in use.")


class LinkService:
    @staticmethod
    async def create_link(
        session: AsyncSession,
        redis: aioredis.Redis,
        data: LinkCreateRequest,
    ) -> Link:
        """Creates a new short link, safely handling concurrency and alias collisions.

        Args:
            session: Async database session.
            redis: Async Redis client.
            data: Link creation payload.

        Returns:
            The persisted Link model.

        Raises:
            AliasAlreadyExistsError: If custom_alias collides with an existing link.
        """
        if data.custom_alias:
            # 1. Custom Alias Path
            link = Link(
                code=data.custom_alias,
                long_url=data.long_url,
                expires_at=data.expires_at,
            )
            session.add(link)
            try:
                await session.commit()
                await session.refresh(link)
            except IntegrityError as exc:
                await session.rollback()
                logger.warning(
                    "Collision on custom alias insertion",
                    extra={"extra_fields": {"alias": data.custom_alias}},
                )
                raise AliasAlreadyExistsError(data.custom_alias) from exc

            # Evict any negative cache entry for this alias
            await LinkService._evict_negative_cache(redis, data.custom_alias)
            return link

        # 2. Auto-Generated Code Path (Base62 over sequence ID)
        max_retries = 3
        for attempt in range(max_retries):
            # Fetch atomic nextval from sequence
            seq_id = await session.scalar(links_id_seq)
            code = encode_id_with_offset(seq_id)

            link = Link(
                id=seq_id,
                code=code,
                long_url=data.long_url,
                expires_at=data.expires_at,
            )
            session.add(link)
            try:
                await session.commit()
                await session.refresh(link)
                # Evict negative cache entry if one was cached
                await LinkService._evict_negative_cache(redis, code)
                return link
            except IntegrityError:
                await session.rollback()
                logger.warning(
                    "Unexpected collision on auto-generated code, retrying",
                    extra={"extra_fields": {"code": code, "attempt": attempt + 1}},
                )
                continue

        raise RuntimeError("Failed to generate a unique short code after multiple attempts.")

    @staticmethod
    async def get_link_by_code(session: AsyncSession, code: str) -> Link | None:
        """Retrieves a link by its unique code if not soft-deleted."""
        stmt = select(Link).where(Link.code == code, Link.is_deleted.is_(False))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def _evict_negative_cache(redis: aioredis.Redis, code: str) -> None:
        """Evicts negative cache key in Redis if present, failing open if Redis is down."""
        try:
            await redis.delete(f"link_not_found:{code}")
        except Exception as exc:
            logger.warning(
                "Failed to evict negative cache in Redis (failing open)",
                exc_info=exc,
                extra={"extra_fields": {"code": code}},
            )
