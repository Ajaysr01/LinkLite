import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import logger
from app.db.session import get_redis_client
from app.models.click_stat import ClickStat
from app.models.link import Link


class AnalyticsService:
    @staticmethod
    async def record_click(redis: aioredis.Redis, code: str) -> None:
        """Atomically increments daily click counter buffer in Redis.

        Executed on the hot redirect path with zero synchronous DB writes.
        """
        today_str = datetime.now(UTC).strftime("%Y-%m-%d")
        daily_key = f"click_buffer:{code}:{today_str}"

        try:
            pipe = redis.pipeline()
            pipe.incr(daily_key)
            pipe.expire(daily_key, 172800)  # 48 hours TTL
            await pipe.execute()
        except (RedisError, Exception) as exc:
            logger.warning(
                "REDIS_UNAVAILABLE: Failed to buffer click counter (analytics degraded)",
                exc_info=exc,
                extra={"extra_fields": {"code": code}},
            )

    @classmethod
    async def flush_click_counters(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis,
    ) -> int:
        """Flushes buffered in-memory Redis click counts to PostgreSQL safely.

        Guarantees no lost increments and no double counting via atomic GETSET.
        """
        try:
            # Discover buffered daily keys
            daily_keys = []
            async for k in redis.scan_iter(match="click_buffer:*"):
                if isinstance(k, bytes):
                    k = k.decode("utf-8")
                daily_keys.append(k)
        except (RedisError, Exception) as exc:
            logger.warning("REDIS_UNAVAILABLE: Failed to scan click buffer keys", exc_info=exc)
            return 0

        if not daily_keys:
            return 0

        items_to_persist: list[dict[str, Any]] = []

        # 1. Atomically harvest counts from Redis using GETSET to 0
        for key in daily_keys:
            # key format: click_buffer:{code}:{YYYY-MM-DD}
            parts = key.split(":")
            if len(parts) != 3:
                continue
            code, date_str = parts[1], parts[2]

            try:
                # Atomic harvest: resets key to 0 so new incoming clicks accumulate cleanly
                val = await redis.getset(key, 0)
                count = int(val) if val else 0
                if count > 0:
                    items_to_persist.append(
                        {
                            "key": key,
                            "code": code,
                            "date": date.fromisoformat(date_str),
                            "count": count,
                        }
                    )
            except (RedisError, Exception) as exc:
                logger.warning(
                    "Error harvesting key during analytics flush",
                    exc_info=exc,
                    extra={"extra_fields": {"key": key}},
                )

        if not items_to_persist:
            return 0

        # 2. Persist harvested items to PostgreSQL in a single database transaction
        flushed_count = 0
        async with session_factory() as session:
            try:
                # Group totals by link code for updating links.click_count
                code_totals: dict[str, int] = {}

                for item in items_to_persist:
                    code = item["code"]
                    dt = item["date"]
                    cnt = item["count"]
                    code_totals[code] = code_totals.get(code, 0) + cnt

                    # Upsert into click_stats: ON CONFLICT (code, date) DO UPDATE
                    upsert_stmt = (
                        pg_insert(ClickStat)
                        .values(code=code, date=dt, count=cnt)
                        .on_conflict_do_update(
                            constraint="uq_click_stats_code_date",
                            set_={"count": ClickStat.count + cnt},
                        )
                    )
                    await session.execute(upsert_stmt)

                # Update denormalized click_count on links table
                for code, total_inc in code_totals.items():
                    link_res = await session.execute(
                        select(Link).where(Link.code == code).with_for_update()
                    )
                    link_obj = link_res.scalar_one_or_none()
                    if link_obj:
                        link_obj.click_count += total_inc

                await session.commit()
                flushed_count = sum(item["count"] for item in items_to_persist)

                logger.info(
                    "Flushed click analytics to PostgreSQL",
                    extra={"extra_fields": {"events_flushed": flushed_count}},
                )

            except Exception as exc:
                await session.rollback()
                logger.error(
                    "DB error during analytics flush, rolling back counts to Redis buffer",
                    exc_info=exc,
                )
                # Rollback counts back into Redis buffer so they are not lost!
                for item in items_to_persist:
                    try:
                        await redis.incrby(item["key"], item["count"])
                    except Exception:
                        pass
                return 0

        return flushed_count

    @staticmethod
    async def get_link_stats(
        session: AsyncSession,
        redis: aioredis.Redis,
        code: str,
        days: int = 30,
    ) -> dict[str, Any] | None:
        """Retrieves total clicks and daily click breakdown for the past N days,

        combining committed database records with active in-memory Redis buffer.
        """
        # Verify link exists and is not soft-deleted
        link_stmt = select(Link).where(Link.code == code, Link.is_deleted.is_(False))
        link_res = await session.execute(link_stmt)
        link = link_res.scalar_one_or_none()
        if not link:
            return None

        today = datetime.now(UTC).date()
        start_date = today - timedelta(days=days - 1)

        # 1. Fetch DB daily stats
        stats_stmt = (
            select(ClickStat)
            .where(
                ClickStat.code == code,
                ClickStat.date >= start_date,
            )
            .order_by(ClickStat.date.asc())
        )
        stats_res = await session.execute(stats_stmt)
        db_stats = {s.date: s.count for s in stats_res.scalars()}

        # 2. Check un-flushed buffer in Redis for today
        unflushed_today = 0
        try:
            today_str = today.strftime("%Y-%m-%d")
            today_buf = await redis.get(f"click_buffer:{code}:{today_str}")
            if today_buf:
                unflushed_today = int(today_buf)
        except Exception:
            pass

        # 3. Assemble daily time-series with zero-fill for days without clicks
        daily_clicks: list[dict[str, Any]] = []
        for i in range(days):
            current_date = start_date + timedelta(days=i)
            count = db_stats.get(current_date, 0)
            if current_date == today:
                count += unflushed_today

            daily_clicks.append({"date": current_date.isoformat(), "clicks": count})

        total_clicks = link.click_count + unflushed_today

        return {
            "code": code,
            "total_clicks": total_clicks,
            "daily_clicks": daily_clicks,
        }


# Background task handle
_analytics_task: asyncio.Task | None = None


async def analytics_worker_loop(
    session_factory: async_sessionmaker[AsyncSession],
    interval_seconds: int,
) -> None:
    """Continuous background worker that flushes click counters periodically."""
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                redis = get_redis_client()
                await AnalyticsService.flush_click_counters(session_factory, redis)
            except Exception as exc:
                logger.error("Unexpected error in analytics flush loop", exc_info=exc)
    except asyncio.CancelledError:
        # Final graceful drain on shutdown
        logger.info("Draining final click analytics buffer before shutdown...")
        try:
            redis = get_redis_client()
            await AnalyticsService.flush_click_counters(session_factory, redis)
        except Exception as exc:
            logger.error("Error during final analytics flush", exc_info=exc)


def start_analytics_worker(
    session_factory: async_sessionmaker[AsyncSession],
    interval_seconds: int,
) -> asyncio.Task:
    global _analytics_task
    _analytics_task = asyncio.create_task(analytics_worker_loop(session_factory, interval_seconds))
    return _analytics_task


async def stop_analytics_worker() -> None:
    global _analytics_task
    if _analytics_task and not _analytics_task.done():
        _analytics_task.cancel()
        try:
            await _analytics_task
        except asyncio.CancelledError:
            pass
        _analytics_task = None
