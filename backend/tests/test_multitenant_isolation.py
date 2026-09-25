import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.apps.organizations.models import Organization, User
from backend.core.database import AsyncSessionLocal
from backend.main import app

pytestmark = pytest.mark.integration


async def _remove_test_data(user_emails: list[str], organization_ids: list[uuid.UUID]) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(delete(User).where(User.email.in_(user_emails)))
            await session.execute(delete(Organization).where(Organization.id.in_(organization_ids)))


@pytest.mark.asyncio
async def test_user_cannot_access_another_organization_with_tenant_header() -> None:
    suffix = uuid.uuid4().hex
    user_a_email = f"alpha-{suffix}@example.com"
    user_b_email = f"beta-{suffix}@example.com"
    organization_ids: list[uuid.UUID] = []

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            register_a = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": user_a_email,
                    "password": "contraseña-de-prueba-segura-123",
                    "full_name": "Usuario Alpha",
                    "organization_name": "Organización Alpha",
                },
            )
            assert register_a.status_code == 201
            organization_a_id = uuid.UUID(register_a.json()["organization"]["id"])
            organization_ids.append(organization_a_id)

            register_b = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": user_b_email,
                    "password": "contraseña-de-prueba-segura-123",
                    "full_name": "Usuario Beta",
                    "organization_name": "Organización Beta",
                },
            )
            assert register_b.status_code == 201
            organization_b_id = uuid.UUID(register_b.json()["organization"]["id"])
            organization_ids.append(organization_b_id)

            login_a = await client.post(
                "/api/v1/auth/login",
                json={"email": user_a_email, "password": "contraseña-de-prueba-segura-123"},
            )
            assert login_a.status_code == 200
            token_a = login_a.json()["access_token"]

            forbidden_request = await client.post(
                f"/api/v1/organizations/{organization_b_id}/invite",
                headers={
                    "Authorization": f"Bearer {token_a}",
                    "X-Organization-Id": str(organization_b_id),
                },
                json={
                    "email": f"miembro-{suffix}@example.com",
                    "role": "member",
                },
            )

            assert forbidden_request.status_code == 403
            assert forbidden_request.json() == {"detail": "Acceso a la organización denegado"}
    finally:
        await _remove_test_data([user_a_email, user_b_email], organization_ids)
