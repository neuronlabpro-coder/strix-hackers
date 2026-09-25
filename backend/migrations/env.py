"""Entorno Alembic asíncrono para las migraciones del backend."""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.apps.organizations import models as organization_models  # noqa: F401
from backend.apps.pentests import models as pentest_models  # noqa: F401
from backend.apps.repositories import models as repository_models  # noqa: F401
from backend.apps.vulnerabilities import models as vulnerability_models  # noqa: F401
from backend.core.config import settings
from backend.core.database import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Genera SQL sin abrir una conexión, usando la URL validada del entorno."""

    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Ejecuta las migraciones sobre una conexión sincrónica entregada por asyncpg."""

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Abre un engine asíncrono aislado para ejecutar Alembic y lo libera siempre."""

    connectable = create_async_engine(
        settings.database_url,
        poolclass=pool.NullPool,
        connect_args={"timeout": settings.db_connect_timeout_seconds},
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Punto de entrada online de Alembic."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
