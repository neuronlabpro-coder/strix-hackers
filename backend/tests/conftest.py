"""Fixtures compartidas para aislar conexiones y datos de pruebas."""

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import delete, or_, select

from backend.apps.organizations.models import Membership, Organization, User
from backend.core.config import settings
from backend.core.database import AsyncSessionLocal, engine
from backend.core.rate_limit import enforce_login_rate_limit, enforce_register_rate_limit
from backend.main import app

TEST_EMAIL_PREFIXES = ("alpha-", "beta-", "api-")


def pytest_configure(config: pytest.Config) -> None:
    """Impide que una suite de pruebas pueda ejecutarse contra producción."""

    if settings.environment == "production":
        raise pytest.UsageError("La suite de pruebas está bloqueada en ENVIRONMENT=production")


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
    yield
    app.dependency_overrides.pop(enforce_login_rate_limit, None)
    app.dependency_overrides.pop(enforce_register_rate_limit, None)


@pytest.fixture(autouse=True)
async def cleanup_integration_test_data(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Elimina estrictamente los registros canary creados por pruebas de integración."""

    yield
    if request.node.get_closest_marker("integration") is None:
        return

    try:
        canary_filter = or_(
            *(User.email.like(f"{prefix}%@example.com") for prefix in TEST_EMAIL_PREFIXES)
        )
        async with AsyncSessionLocal() as session:
            async with session.begin():
                organization_ids = list(
                    (
                        await session.execute(
                            select(Membership.organization_id)
                            .join(User, User.id == Membership.user_id)
                            .where(canary_filter)
                        )
                    ).scalars()
                )
                await session.execute(delete(User).where(canary_filter))
                if organization_ids:
                    await session.execute(
                        delete(Organization).where(Organization.id.in_(organization_ids))
                    )
    finally:
        await engine.dispose()
