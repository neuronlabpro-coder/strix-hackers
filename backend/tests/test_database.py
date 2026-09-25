from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

from backend.core.config import Settings
from backend.core.database import create_database_engine
from backend.tests.test_config import build_environment_values


@pytest.mark.asyncio
async def test_database_engine_uses_async_pool_configuration() -> None:
    settings = Settings(_env_file=None, **build_environment_values())  # pyright: ignore[reportCallIssue]

    engine = create_database_engine(settings)
    pool = cast(QueuePool, engine.pool)

    assert isinstance(engine, AsyncEngine)
    assert pool.size() == settings.db_pool_size
    assert pool._max_overflow == settings.db_max_overflow
    assert pool._recycle == settings.db_pool_recycle_seconds
    assert pool._pre_ping is True

    await engine.dispose()
