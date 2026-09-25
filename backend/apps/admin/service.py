"""Sondeo de PostgreSQL y Redis para la consola de SuperAdmin."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.admin.schemas import (
    DependencyHealth,
    DependencyStatusEnum,
    InfrastructureHealthResponse,
)
from backend.core.redis import redis_client


async def _probe_database(session: AsyncSession) -> DependencyHealth:
    started = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return DependencyHealth(status="offline", latency_ms=0)
    return DependencyHealth(
        status="online",
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def _probe_cache(client: Redis) -> DependencyHealth:
    started = time.perf_counter()
    try:
        await client.ping()
    except (RedisError, OSError, TimeoutError):
        return DependencyHealth(status="offline", latency_ms=0)
    return DependencyHealth(
        status="online",
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def check_infrastructure(
    session: AsyncSession,
    cache: Redis | None = None,
    now: datetime | None = None,
) -> InfrastructureHealthResponse:
    """Sondea ambas dependencias y degrada a `degraded` sin filtrar detalles."""

    database = await _probe_database(session)
    cache_health = await _probe_cache(cache if cache is not None else redis_client)
    aggregate = (
        DependencyStatusEnum.HEALTHY
        if database.status == "online" and cache_health.status == "online"
        else DependencyStatusEnum.DEGRADED
    )
    return InfrastructureHealthResponse(
        status=aggregate,
        database=database,
        cache=cache_health,
        checked_at=now or datetime.now(UTC),
    )
