import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_user_cannot_access_another_organization_with_tenant_header() -> None:
    suffix = uuid.uuid4().hex
    user_a_email = f"alpha-{suffix}@example.com"
    user_b_email = f"beta-{suffix}@example.com"
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
        verification_a = register_a.json()["verification_token"]
        verify_a = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": verification_a},
        )
        assert verify_a.status_code == 200

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
        verification_b = register_b.json()["verification_token"]
        verify_b = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": verification_b},
        )
        assert verify_b.status_code == 200

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
