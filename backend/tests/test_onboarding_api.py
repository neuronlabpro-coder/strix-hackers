"""Pruebas del estado de onboarding derivado del estado real del tenant."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    Repository,
    generate_webhook_secret,
)
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

_VALID_CIPHERTEXT = "v1." + "A" * 24 + "." + "B" * 43

EXPECTED_STEPS = ("connect_git", "import_repository", "run_first_scan")


async def _tenant(session: AsyncSession) -> tuple[Organization, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Onboarding {suffix}", slug=f"onb-{suffix}")
    user = User(
        email=f"onb-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Onboarding User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return organization, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


def _add_credential(session: AsyncSession, organization_id: uuid.UUID) -> None:
    session.add(
        GitCredential(
            organization_id=organization_id,
            provider=GitProviderEnum.GITHUB,
            encrypted_access_token=_VALID_CIPHERTEXT,
        )
    )


def _add_repository(session: AsyncSession, organization_id: uuid.UUID) -> Repository:
    repository = Repository(
        organization_id=organization_id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=f"remote-{uuid.uuid4().hex}",
        name="app",
        full_name=f"acme/app-{uuid.uuid4().hex[:8]}",
        clone_url=f"https://github.com/acme/app-{uuid.uuid4().hex}.git",
        default_branch="main",
        webhook_secret=generate_webhook_secret(),
    )
    session.add(repository)
    return repository


def _add_run(session: AsyncSession, organization_id: uuid.UUID) -> PentestRun:
    run = PentestRun(
        organization_id=organization_id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="app.example.com",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.COMPLETED,
    )
    session.add(run)
    return run


@pytest.mark.asyncio
async def test_onboarding_starts_with_no_steps_completed(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _organization, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/onboarding/status", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["completed_steps"] == 0
    assert payload["total_steps"] == 3
    assert payload["is_complete"] is False
    assert [step["key"] for step in payload["steps"]] == list(EXPECTED_STEPS)
    assert all(step["completed"] is False for step in payload["steps"])


@pytest.mark.asyncio
async def test_onboarding_completes_steps_progressively(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _add_credential(integration_session, organization.id)
        await integration_session.commit()
        after_credential = await client.get("/api/v1/onboarding/status", headers=headers)

        _add_repository(integration_session, organization.id)
        await integration_session.commit()
        after_repository = await client.get("/api/v1/onboarding/status", headers=headers)

        _add_run(integration_session, organization.id)
        await integration_session.commit()
        after_run = await client.get("/api/v1/onboarding/status", headers=headers)

    assert after_credential.json()["completed_steps"] == 1
    assert after_credential.json()["steps"][0]["completed"] is True
    assert after_credential.json()["steps"][1]["completed"] is False

    assert after_repository.json()["completed_steps"] == 2

    final = after_run.json()
    assert final["completed_steps"] == 3
    assert final["is_complete"] is True
    assert all(step["completed"] is True for step in final["steps"])


@pytest.mark.asyncio
async def test_onboarding_ignores_state_of_other_tenants(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization_a, headers_a = await _tenant(integration_session)
    organization_b, _headers_b = await _tenant(integration_session)
    _add_credential(integration_session, organization_b.id)
    _add_repository(integration_session, organization_b.id)
    _add_run(integration_session, organization_b.id)
    await integration_session.commit()
    assert organization_a.id != organization_b.id
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/onboarding/status", headers=headers_a)

    assert response.status_code == 200
    assert response.json()["completed_steps"] == 0


@pytest.mark.asyncio
async def test_onboarding_requires_authentication() -> None:
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/onboarding/status")

    assert response.status_code == 401
