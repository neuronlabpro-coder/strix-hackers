"""SuperAdmin administra topes y ve la resolución aislada por tenant."""

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.llm_router.models import LLMCostLimit
from backend.apps.organizations.models import Organization, User
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_superadmin_crea_y_previsualiza_un_tope_aislado(
    integration_session: AsyncSession,
) -> None:
    tenant_a = Organization(name=f"Límite A {uuid.uuid4().hex}", slug=f"lim-a-{uuid.uuid4().hex}")
    tenant_b = Organization(name=f"Límite B {uuid.uuid4().hex}", slug=f"lim-b-{uuid.uuid4().hex}")
    admin = User(
        email=f"limites-{uuid.uuid4().hex}@example.com",
        hashed_password="not-used",
        full_name="Admin límites",
        email_verified=True,
        is_superuser=True,
    )
    integration_session.add_all((tenant_a, tenant_b, admin))
    await integration_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(admin.id)})}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        creada = await client.post(
            "/api/v1/admin/cost-limits/",
            json={
                "scope": "ORGANIZACION",
                "organization_id": str(tenant_a.id),
                "max_budget_usd": "4.25",
                "max_turns": 19,
            },
            headers=headers,
        )
        editada = await client.patch(
            f"/api/v1/admin/cost-limits/{creada.json()['id']}",
            json={
                "scope": "ORGANIZACION",
                "organization_id": str(tenant_a.id),
                "max_budget_usd": "5.50",
                "max_turns": 21,
            },
            headers=headers,
        )
        vista_a = await client.get(
            "/api/v1/admin/cost-limits/preview",
            params={"organization_id": str(tenant_a.id), "operation": "PENTEST_QUICK"},
            headers=headers,
        )
        eliminada = await client.delete(
            f"/api/v1/admin/cost-limits/{editada.json()['id']}", headers=headers
        )
        vista_tras_eliminar = await client.get(
            "/api/v1/admin/cost-limits/preview",
            params={"organization_id": str(tenant_a.id), "operation": "PENTEST_QUICK"},
            headers=headers,
        )
        vista_b = await client.get(
            "/api/v1/admin/cost-limits/preview",
            params={"organization_id": str(tenant_b.id), "operation": "PENTEST_QUICK"},
            headers=headers,
        )
        anonimo = await client.get("/api/v1/admin/cost-limits/")

    assert creada.status_code == 201
    assert editada.status_code == 200
    assert eliminada.status_code == 204
    assert vista_a.status_code == 200
    assert Decimal(vista_a.json()["max_budget_usd"]) == Decimal("5.50")
    assert vista_a.json()["presupuesto_origen"]["nivel"] == "ORGANIZACION"
    assert vista_tras_eliminar.status_code == 200
    assert vista_tras_eliminar.json()["presupuesto_origen"]["nivel"] != "ORGANIZACION"
    assert vista_b.status_code == 200
    assert vista_b.json()["presupuesto_origen"]["nivel"] != "ORGANIZACION"
    assert anonimo.status_code == 401
    await integration_session.execute(
        delete(LLMCostLimit).where(LLMCostLimit.organization_id == tenant_a.id)
    )
    await integration_session.execute(
        delete(Organization).where(Organization.id.in_((tenant_a.id, tenant_b.id)))
    )
    await integration_session.commit()
