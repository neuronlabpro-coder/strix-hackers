import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.pentests.models import PentestRun, ScanModeEnum, TargetTypeEnum
from backend.apps.vulnerabilities.models import IssueStatusEnum, SeverityEnum, Vulnerability
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_phase2_endpoints_are_tenant_isolated(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    suffix = uuid.uuid4().hex
    organization_a = Organization(name=f"Phase2 Alpha {suffix}", slug=f"phase2-alpha-{suffix}")
    organization_b = Organization(name=f"Phase2 Beta {suffix}", slug=f"phase2-beta-{suffix}")
    user_a = User(
        email=f"phase2-alpha-{suffix}@example.com",
        hashed_password="not-used-in-this-test",
        full_name="Phase2 Alpha",
        email_verified=True,
    )
    user_b = User(
        email=f"phase2-beta-{suffix}@example.com",
        hashed_password="not-used-in-this-test",
        full_name="Phase2 Beta",
        email_verified=True,
    )
    integration_session.add_all([organization_a, organization_b, user_a, user_b])
    await integration_session.flush()
    integration_session.add_all(
        [
            Membership(organization_id=organization_a.id, user_id=user_a.id, role=RoleEnum.ADMIN),
            Membership(organization_id=organization_b.id, user_id=user_b.id, role=RoleEnum.ADMIN),
        ]
    )
    run_a = PentestRun(
        organization_id=organization_a.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="alpha.example.test",
        scan_mode=ScanModeEnum.STANDARD,
    )
    run_b = PentestRun(
        organization_id=organization_b.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="beta.example.test",
        scan_mode=ScanModeEnum.STANDARD,
    )
    integration_session.add_all([run_a, run_b])
    await integration_session.flush()
    vulnerability_a = Vulnerability(
        organization_id=organization_a.id,
        run_id=run_a.id,
        title="Alpha Finding",
        description="Alpha can see this finding.",
        severity=SeverityEnum.CRITICAL,
        cvss_score=9.9,
        cve_id=None,
        affected_target="alpha.example.test",
        poc_reproduction_raw="curl https://alpha.example.test",
        autofix_patch_diff="--- old\n+++ new",
        status=IssueStatusEnum.OPEN,
    )
    vulnerability_b = Vulnerability(
        organization_id=organization_b.id,
        run_id=run_b.id,
        title="Private Beta Finding",
        description="Only Beta can see this finding.",
        severity=SeverityEnum.HIGH,
        cvss_score=8.1,
        cve_id=None,
        affected_target="beta.example.test",
        poc_reproduction_raw="curl https://beta.example.test",
        status=IssueStatusEnum.OPEN,
    )
    integration_session.add_all([vulnerability_a, vulnerability_b])
    await integration_session.flush()

    token_a = create_access_token({"sub": str(user_a.id)})
    headers_a = {
        "Authorization": f"Bearer {token_a}",
        "X-Organization-Id": str(organization_a.id),
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_run = await client.post(
            "/api/v1/pentests/",
            headers=headers_a,
            json={
                "target_type": "REPOSITORY",
                "target_identifier": "https://example.test/repository",
                "scan_mode": "QUICK",
            },
        )
        assert create_run.status_code == 201
        assert create_run.json()["status"] == "QUEUED"
        created_run_id = uuid.UUID(create_run.json()["id"])

        own_run = await client.get(f"/api/v1/pentests/{created_run_id}", headers=headers_a)
        assert own_run.status_code == 200

        foreign_run = await client.get(f"/api/v1/pentests/{run_b.id}", headers=headers_a)
        assert foreign_run.status_code == 404

        vulnerabilities = await client.get("/api/v1/vulnerabilities/", headers=headers_a)
        assert vulnerabilities.status_code == 200
        assert vulnerabilities.json()["total"] == 1
        assert vulnerabilities.json()["items"][0]["title"] == "Alpha Finding"

        filtered_vulnerabilities = await client.get(
            "/api/v1/vulnerabilities/?severity=CRITICAL&status=OPEN",
            headers=headers_a,
        )
        assert filtered_vulnerabilities.status_code == 200
        assert filtered_vulnerabilities.json()["total"] == 1

        target_filtered = await client.get(
            "/api/v1/vulnerabilities/?target=alpha.example.test",
            headers=headers_a,
        )
        assert target_filtered.status_code == 200
        assert target_filtered.json()["total"] == 1

        detail = await client.get(
            f"/api/v1/vulnerabilities/{vulnerability_a.id}",
            headers=headers_a,
        )
        assert detail.status_code == 200
        assert detail.json()["poc_reproduction_raw"].startswith("curl")
        assert detail.json()["autofix_patch_diff"] is not None

        foreign_vulnerability = await client.get(
            f"/api/v1/vulnerabilities/{vulnerability_b.id}", headers=headers_a
        )
        assert foreign_vulnerability.status_code == 404
