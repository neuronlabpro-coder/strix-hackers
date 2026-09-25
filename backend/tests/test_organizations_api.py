import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.apps.organizations.models import Organization, User
from backend.core.database import AsyncSessionLocal
from backend.main import app

pytestmark = pytest.mark.integration


async def _remove_test_data(user_email: str, organization_ids: list[uuid.UUID]) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(delete(User).where(User.email == user_email))
            await session.execute(delete(Organization).where(Organization.id.in_(organization_ids)))


@pytest.mark.asyncio
async def test_registration_login_organization_listing_and_creation() -> None:
    suffix = uuid.uuid4().hex
    user_email = f"api-{suffix}@example.com"
    organization_ids: list[uuid.UUID] = []

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            register = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": user_email,
                    "password": "contraseña-de-prueba-segura-123",
                    "full_name": "Usuario API",
                    "organization_name": "Workspace inicial",
                },
            )
            assert register.status_code == 201
            initial_organization_id = uuid.UUID(register.json()["organization"]["id"])
            organization_ids.append(initial_organization_id)

            login = await client.post(
                "/api/v1/auth/login",
                json={"email": user_email, "password": "contraseña-de-prueba-segura-123"},
            )
            assert login.status_code == 200
            token = login.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            organizations = await client.get("/api/v1/organizations/me", headers=headers)
            assert organizations.status_code == 200
            assert [item["id"] for item in organizations.json()] == [str(initial_organization_id)]

            created = await client.post(
                "/api/v1/organizations/",
                headers=headers,
                json={"name": "Workspace adicional"},
            )
            assert created.status_code == 201
            created_organization_id = uuid.UUID(created.json()["id"])
            organization_ids.append(created_organization_id)
            assert created.json()["role"] == "admin"

            updated_organizations = await client.get("/api/v1/organizations/me", headers=headers)
            assert updated_organizations.status_code == 200
            assert len(updated_organizations.json()) == 2

            invitation = await client.post(
                f"/api/v1/organizations/{created_organization_id}/invite",
                headers={
                    **headers,
                    "X-Organization-Id": str(created_organization_id),
                },
                json={"email": f"invitado-{suffix}@example.com", "role": "member"},
            )
            assert invitation.status_code == 201
            assert invitation.json()["role"] == "member"
    finally:
        await _remove_test_data(user_email, organization_ids)
