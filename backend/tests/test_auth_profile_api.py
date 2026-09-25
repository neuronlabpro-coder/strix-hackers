"""Pruebas del endpoint de perfil del usuario autenticado."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from backend.apps.organizations.models import User
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_current_user_profile_exposes_superuser_flag(integration_session) -> None:  # type: ignore[no-untyped-def]
    assert integration_session is not None
    suffix = uuid.uuid4().hex
    owner = User(
        email=f"owner-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Propietario",
        email_verified=True,
        is_superuser=True,
    )
    member = User(
        email=f"member-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Miembro",
        email_verified=True,
    )
    integration_session.add_all([owner, member])
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        owner_response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {create_access_token({'sub': str(owner.id)})}"},
        )
        member_response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {create_access_token({'sub': str(member.id)})}"},
        )
        anonymous = await client.get("/api/v1/auth/me")

    assert owner_response.status_code == 200
    assert owner_response.json() == {
        "id": str(owner.id),
        "email": owner.email,
        "full_name": "Propietario",
        "is_superuser": True,
    }
    assert member_response.status_code == 200
    assert member_response.json()["is_superuser"] is False
    assert anonymous.status_code == 401
