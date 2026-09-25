from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis

from backend.core.config import Settings
from backend.core.redis import check_redis_health, create_redis_client
from backend.tests.test_config import build_environment_values


def test_redis_client_uses_tailscale_endpoint_from_settings() -> None:
    settings = Settings(_env_file=None, **build_environment_values())  # pyright: ignore[reportCallIssue]

    client = create_redis_client(settings)
    connection_options = client.connection_pool.connection_kwargs

    assert isinstance(client, Redis)
    assert connection_options["host"] == settings.redis_host
    assert connection_options["port"] == settings.redis_port
    assert connection_options["db"] == settings.redis_db


@pytest.mark.asyncio
async def test_redis_healthcheck_requires_successful_ping() -> None:
    client = AsyncMock(spec=Redis)
    client.ping = AsyncMock(return_value=True)

    await check_redis_health(client)

    client.ping.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_redis_healthcheck_rejects_failed_ping() -> None:
    client = AsyncMock(spec=Redis)
    client.ping = AsyncMock(return_value=False)

    with pytest.raises(RuntimeError, match="Redis no respondió"):
        await check_redis_health(client)
