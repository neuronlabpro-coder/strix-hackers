"""Fixtures compartidas para aislar conexiones y datos de pruebas."""

from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.pentests.router import get_dispatch_pentest_run
from backend.core.config import settings
from backend.core.database import engine, get_db
from backend.core.rate_limit import (
    enforce_autofix_rate_limit,
    enforce_create_organization_rate_limit,
    enforce_email_resend_rate_limit,
    enforce_email_verification_rate_limit,
    enforce_git_webhook_rate_limit,
    enforce_invitation_accept_rate_limit,
    enforce_invitation_rate_limit,
    enforce_login_rate_limit,
    enforce_pentest_rate_limit,
    enforce_register_rate_limit,
    get_rate_limit_redis,
)
from backend.main import app


def pytest_configure(config: pytest.Config) -> None:
    """Impide que una suite de pruebas pueda ejecutarse contra producción."""

    if settings.environment not in {"development", "test"}:
        raise pytest.UsageError(
            "La suite de pruebas solo puede ejecutarse en ENVIRONMENT=development o test"
        )
    if settings.email_verification_delivery_mode != "development":
        raise pytest.UsageError(
            "Las pruebas no pueden ejecutar el delivery real de email; usa el modo development"
        )


@pytest.fixture(autouse=True)
async def dispose_database_engine() -> AsyncIterator[None]:
    """Cierra conexiones del pool antes de que termine el event loop de cada test."""

    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
def override_auth_rate_limits() -> Iterator[None]:
    """Desactiva el rate limiter real solo dentro de la suite controlada."""

    app.dependency_overrides[enforce_login_rate_limit] = lambda: None
    app.dependency_overrides[enforce_register_rate_limit] = lambda: None
    app.dependency_overrides[enforce_create_organization_rate_limit] = lambda: None
    app.dependency_overrides[enforce_autofix_rate_limit] = lambda: None
    app.dependency_overrides[enforce_email_verification_rate_limit] = lambda: None
    app.dependency_overrides[enforce_email_resend_rate_limit] = lambda: None
    app.dependency_overrides[enforce_git_webhook_rate_limit] = lambda: None
    app.dependency_overrides[get_rate_limit_redis] = lambda: AsyncMock()
    app.dependency_overrides[enforce_invitation_accept_rate_limit] = lambda: None
    app.dependency_overrides[enforce_invitation_rate_limit] = lambda: None
    app.dependency_overrides[enforce_pentest_rate_limit] = lambda: None
    app.dependency_overrides[get_dispatch_pentest_run] = lambda: (lambda _run_id: "test-task-id")
    yield
    app.dependency_overrides.pop(enforce_login_rate_limit, None)
    app.dependency_overrides.pop(enforce_register_rate_limit, None)
    app.dependency_overrides.pop(enforce_create_organization_rate_limit, None)
    app.dependency_overrides.pop(enforce_autofix_rate_limit, None)
    app.dependency_overrides.pop(enforce_email_verification_rate_limit, None)
    app.dependency_overrides.pop(enforce_email_resend_rate_limit, None)
    app.dependency_overrides.pop(enforce_git_webhook_rate_limit, None)
    app.dependency_overrides.pop(get_rate_limit_redis, None)
    app.dependency_overrides.pop(enforce_invitation_accept_rate_limit, None)
    app.dependency_overrides.pop(enforce_invitation_rate_limit, None)
    app.dependency_overrides.pop(enforce_pentest_rate_limit, None)
    app.dependency_overrides.pop(get_dispatch_pentest_run, None)


@pytest.fixture
async def integration_session(request: pytest.FixtureRequest) -> AsyncIterator[AsyncSession | None]:
    """Proporciona una sesión de integración aislada por transacción y savepoints."""

    if request.node.get_closest_marker("integration") is None:
        yield None
        return

    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        app.dependency_overrides[get_db] = lambda: session
        try:
            yield session
        finally:
            app.dependency_overrides.pop(get_db, None)
            await session.close()
            await transaction.rollback()
            await engine.dispose()


@pytest.fixture(autouse=True)
async def isolate_integration_database(
    integration_session: AsyncSession | None,
) -> AsyncIterator[None]:
    """Garantiza que toda prueba marcada como integración use la sesión rollbackeable."""

    del integration_session
    yield
