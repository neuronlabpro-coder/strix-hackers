"""Comprueba de solo lectura la conectividad con PostgreSQL y Redis."""

import asyncio
import sys

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from backend.core.config import settings
from backend.core.database import engine
from backend.core.redis import check_redis_health, create_redis_client


async def check_postgres() -> None:
    """Valida la sesión, el motor asíncrono, la base y el servidor PostgreSQL."""

    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT current_database() AS database_name"))
        server = result.mappings().one()

    if server["database_name"] != settings.db_name:
        raise RuntimeError("PostgreSQL respondió desde una base de datos inesperada")

    print(
        f"[OK] PostgreSQL: {settings.db_host}:{settings.db_port}/{settings.db_name} "
        f"mediante pool asíncrono (pool_pre_ping=True)"
    )


async def check_redis() -> None:
    """Valida autenticación y disponibilidad de Redis con PING."""

    client = create_redis_client(settings)
    try:
        await check_redis_health(client)
    finally:
        await client.aclose()

    print(
        f"[OK] Redis: {settings.redis_host}:{settings.redis_port}/{settings.redis_db} "
        "respondió a PING"
    )


async def run_check() -> None:
    """Ejecuta todas las comprobaciones de infraestructura en orden."""

    await check_postgres()
    await check_redis()


async def main() -> int:
    """Devuelve un código de salida seguro para automatización y CI."""

    try:
        await run_check()
    except (RedisError, RuntimeError, SQLAlchemyError, OSError, TimeoutError) as error:
        service_name: str = "Redis" if isinstance(error, RedisError) else "infraestructura"
        message = f"[ERROR] Fallo de conectividad de {service_name}: {type(error).__name__}"
        print(message, file=sys.stderr)
        return 1

    print("[OK] Conectividad local-remota verificada mediante Tailscale")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
