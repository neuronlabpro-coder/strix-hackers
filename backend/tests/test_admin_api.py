"""Pruebas de la consola de SuperAdmin y del sondeo de infraestructura."""

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import (
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _tenant(
    session: AsyncSession,
    *,
    is_superuser: bool,
    role: RoleEnum = RoleEnum.ADMIN,
) -> tuple[Organization, User, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"Admin {suffix}",
        slug=f"admin-{suffix}",
        plan_tier=PlanTierEnum.PRO,
        credit_balance=42.5,
    )
    user = User(
        email=f"admin-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Admin User",
        email_verified=True,
        is_superuser=is_superuser,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=role))
    await session.commit()
    return organization, user, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


@pytest.mark.asyncio
async def test_superuser_lists_organizations_with_plan_and_credits(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _user, headers = await _tenant(integration_session, is_superuser=True)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/organizations", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 1
    item = next(entry for entry in payload["items"] if entry["id"] == str(organization.id))
    assert item["plan_tier"] == "PRO"
    # `credit_balance` viaja como cadena porque es un `Decimal`. Antes de la consola de
    # SuperAdmin se serializaba como `float` y llegaba como `42.5`; ahora llega `"42.5"`.
    # La cadena es la forma correcta en JSON: un número binario en coma flotante no puede
    # representar 42,10 sin error, y un saldo que se desincroniza del ledger por un redondeo
    # de coma flotante es un saldo que no cuadra en la auditoría.
    assert Decimal(str(item["credit_balance"])) == Decimal("42.5")
    assert item["slug"] == organization.slug


@pytest.mark.asyncio
async def test_admin_routes_reject_regular_users(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    _organization, _user, headers = await _tenant(integration_session, is_superuser=False)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        organizations = await client.get("/api/v1/admin/organizations", headers=headers)
        health = await client.get("/api/v1/admin/health", headers=headers)

    assert organizations.status_code == 403
    assert health.status_code == 403


@pytest.mark.asyncio
async def test_admin_routes_require_authentication() -> None:
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/organizations")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_infrastructure_health_reports_dependencies(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _organization, _user, headers = await _tenant(integration_session, is_superuser=True)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/admin/health", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["database"]["status"] == "online"
    assert payload["cache"]["status"] == "online"
    assert payload["status"] == "healthy"
