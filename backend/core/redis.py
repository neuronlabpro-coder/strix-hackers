"""Cliente Redis asíncrono y comprobación de salud."""

from inspect import isawaitable

from redis.asyncio import Redis

from backend.core.config import Settings, settings


def create_redis_client(config: Settings) -> Redis:
    """Crea un cliente Redis usando exclusivamente la configuración del entorno."""

    return Redis.from_url(
        config.redis_url,
        decode_responses=True,
        socket_connect_timeout=config.redis_socket_timeout_seconds,
        socket_timeout=config.redis_socket_timeout_seconds,
    )


redis_client = create_redis_client(settings)


async def check_redis_health(client: Redis = redis_client) -> None:
    """Verifica que Redis responda correctamente mediante un `PING` no destructivo."""

    ping_result = client.ping()
    if isawaitable(ping_result):
        ping_result = await ping_result
    if not ping_result:
        raise RuntimeError("Redis no respondió correctamente al PING")
