import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.core.rate_limit import enforce_email_resend_rate_limit, get_rate_limit_redis
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def register_and_verify(
    client: AsyncClient,
    email: str,
    full_name: str,
    organization_name: str,
) -> tuple[str, str]:
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "contraseña-de-prueba-segura-123",
            "full_name": full_name,
            "organization_name": organization_name,
        },
    )
    assert register.status_code == 201
    token = register.json()["verification_token"]
    assert token

    verification = await client.post(
        "/api/v1/auth/verify-email",
        json={"token": token},
    )
    assert verification.status_code == 200

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "contraseña-de-prueba-segura-123"},
    )
    assert login.status_code == 200
    return login.json()["access_token"], register.json()["organization"]["id"]


@pytest.mark.asyncio
async def test_unverified_user_cannot_use_a_previously_issued_token() -> None:
    suffix = uuid.uuid4().hex
    email = f"unverified-token-{suffix}@example.com"
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        register = await client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "contraseña-de-prueba-segura-123",
                "full_name": "Usuario Sin Verificar",
                "organization_name": "Workspace Sin Verificar",
            },
        )
        assert register.status_code == 201
        user_id = register.json()["user"]["id"]
        unverified_token = create_access_token({"sub": user_id})

        protected_request = await client.get(
            "/api/v1/organizations/me",
            headers={"Authorization": f"Bearer {unverified_token}"},
        )

        assert protected_request.status_code == 401


@pytest.mark.asyncio
async def test_resend_route_accepts_email_payload_without_dependency_contract_error() -> None:
    redis = AsyncMock()
    redis.eval.side_effect = [1, 1]
    redis.ttl.side_effect = [60, 60]
    app.dependency_overrides.pop(enforce_email_resend_rate_limit, None)
    app.dependency_overrides[get_rate_limit_redis] = lambda: redis
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/auth/resend-verification",
                json={"email": "nonexistent@example.com"},
            )
        assert response.status_code == 202
    finally:
        app.dependency_overrides.pop(get_rate_limit_redis, None)


@pytest.mark.asyncio
async def test_email_verification_can_be_resent_after_delivery_failure() -> None:
    suffix = uuid.uuid4().hex
    email = f"resend-{suffix}@example.com"
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        register = await client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "contraseña-de-prueba-segura-123",
                "full_name": "Usuario Resend",
                "organization_name": "Workspace Resend",
            },
        )
        assert register.status_code == 201
        old_token = register.json()["verification_token"]

        resend = await client.post("/api/v1/auth/resend-verification", json={"email": email})
        assert resend.status_code == 202
        new_token = resend.json()["verification_token"]
        assert new_token
        assert new_token != old_token

        old_verification = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": old_token},
        )
        assert old_verification.status_code == 400

        new_verification = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": new_token},
        )
        assert new_verification.status_code == 200


@pytest.mark.asyncio
async def test_invitation_can_be_accepted_by_the_invited_user() -> None:
    suffix = uuid.uuid4().hex
    admin_email = f"invite-admin-{suffix}@example.com"
    invited_email = f"invite-user-{suffix}@example.com"
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin_token, organization_id = await register_and_verify(
            client,
            admin_email,
            "Admin Invitador",
            "Workspace Invitaciones",
        )
        invite = await client.post(
            f"/api/v1/organizations/{organization_id}/invite",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Organization-Id": organization_id,
            },
            json={"email": invited_email, "role": "member"},
        )
        assert invite.status_code == 201
        invitation_token = invite.json()["invitation_token"]
        assert invitation_token

        invited_token, _ = await register_and_verify(
            client,
            invited_email,
            "Usuario Invitado",
            "Workspace Invitado",
        )
        accept = await client.post(
            "/api/v1/invitations/accept",
            headers={"Authorization": f"Bearer {invited_token}"},
            json={"token": invitation_token},
        )
        assert accept.status_code == 200
        assert accept.json()["organization_id"] == organization_id

        organizations = await client.get(
            "/api/v1/organizations/me",
            headers={"Authorization": f"Bearer {invited_token}"},
        )
        assert organizations.status_code == 200
        assert organization_id in {item["id"] for item in organizations.json()}
