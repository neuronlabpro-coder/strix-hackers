"""Engine y sesiones asíncronas para PostgreSQL mediante SQLAlchemy."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from backend.core.config import Settings, settings


class Base(DeclarativeBase):
    """Base declarativa compartida por los modelos SQLAlchemy."""


def create_database_engine(config: Settings) -> AsyncEngine:
    """Crea un engine asíncrono con el pool definido en la configuración."""

    return create_async_engine(
        config.database_url,
        pool_size=config.db_pool_size,
        max_overflow=config.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=config.db_pool_recycle_seconds,
        connect_args={"timeout": config.db_connect_timeout_seconds},
    )


engine = create_database_engine(settings)
AsyncSessionLocal = async_sessionmaker[AsyncSession](
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Entrega una sesión SQLAlchemy por dependencia FastAPI y la cierra al terminar."""

    async with AsyncSessionLocal() as session:
        yield session
