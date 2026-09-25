import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import PentestRun, ScanModeEnum, TargetTypeEnum
from backend.apps.vulnerabilities.models import SeverityEnum, Vulnerability

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_vulnerability_evidence_cannot_be_updated(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    organization = Organization(name="R4 Evidence", slug=f"r4-evidence-{uuid.uuid4().hex}")
    integration_session.add(organization)
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="r4.example.test",
        scan_mode=ScanModeEnum.STANDARD,
    )
    integration_session.add(run)
    await integration_session.flush()
    vulnerability = Vulnerability(
        organization_id=organization.id,
        run_id=run.id,
        title="Immutable evidence",
        description="Evidence cannot be changed.",
        severity=SeverityEnum.HIGH,
        cvss_score=8.0,
        cve_id=None,
        affected_target="r4.example.test",
        poc_reproduction_raw="original-poc",
    )
    integration_session.add(vulnerability)
    await integration_session.flush()

    with pytest.raises(DBAPIError, match="inmutables"):
        await integration_session.execute(
            text(
                "UPDATE vulnerabilities "
                "SET poc_reproduction_raw = :new_poc "
                "WHERE id = :vulnerability_id"
            ),
            {"new_poc": "changed-poc", "vulnerability_id": vulnerability.id},
        )


@pytest.mark.asyncio
async def test_pentest_run_cannot_be_moved_to_another_tenant(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    suffix = uuid.uuid4().hex
    organization_a = Organization(name=f"R3 Run A {suffix}", slug=f"r3-run-a-{suffix}")
    organization_b = Organization(name=f"R3 Run B {suffix}", slug=f"r3-run-b-{suffix}")
    integration_session.add_all([organization_a, organization_b])
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization_a.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="r3-run-a.example.test",
        scan_mode=ScanModeEnum.STANDARD,
    )
    integration_session.add(run)
    await integration_session.flush()

    with pytest.raises(DBAPIError, match="tenant"):
        await integration_session.execute(
            text("UPDATE pentest_runs SET organization_id = :organization_id WHERE id = :run_id"),
            {"organization_id": organization_b.id, "run_id": run.id},
        )


@pytest.mark.asyncio
async def test_pentest_scan_provenance_can_be_assigned_once(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization = Organization(name="R3 Scan", slug=f"r3-scan-{uuid.uuid4().hex}")
    integration_session.add(organization)
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="r3-scan.example.test",
        scan_mode=ScanModeEnum.STANDARD,
    )
    integration_session.add(run)
    await integration_session.flush()
    await integration_session.execute(
        text("UPDATE pentest_runs SET source_scan_id = :scan_id WHERE id = :run_id"),
        {"scan_id": "scan-001", "run_id": run.id},
    )
    await integration_session.commit()

    with pytest.raises(DBAPIError, match="inmutable"):
        await integration_session.execute(
            text("UPDATE pentest_runs SET source_scan_id = :scan_id WHERE id = :run_id"),
            {"scan_id": "scan-002", "run_id": run.id},
        )


@pytest.mark.asyncio
async def test_vulnerability_identity_cannot_be_reassigned(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    suffix = uuid.uuid4().hex
    organization_a = Organization(name=f"R4 Identity A {suffix}", slug=f"r4-id-a-{suffix}")
    organization_b = Organization(name=f"R4 Identity B {suffix}", slug=f"r4-id-b-{suffix}")
    integration_session.add_all([organization_a, organization_b])
    await integration_session.flush()
    run_a = PentestRun(
        organization_id=organization_a.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="r4-id-a.example.test",
        scan_mode=ScanModeEnum.STANDARD,
    )
    run_b = PentestRun(
        organization_id=organization_b.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="r4-id-b.example.test",
        scan_mode=ScanModeEnum.STANDARD,
    )
    integration_session.add_all([run_a, run_b])
    await integration_session.flush()
    vulnerability = Vulnerability(
        organization_id=organization_a.id,
        run_id=run_a.id,
        title="Immutable identity",
        description="The tenant pair must remain immutable.",
        severity=SeverityEnum.HIGH,
        cvss_score=8.0,
        cve_id=None,
        affected_target="r4-id-a.example.test",
        poc_reproduction_raw="poc",
    )
    integration_session.add(vulnerability)
    await integration_session.flush()

    with pytest.raises(DBAPIError, match="inmutables"):
        await integration_session.execute(
            text(
                "UPDATE vulnerabilities "
                "SET organization_id = :organization_id, run_id = :run_id "
                "WHERE id = :vulnerability_id"
            ),
            {
                "organization_id": organization_b.id,
                "run_id": run_b.id,
                "vulnerability_id": vulnerability.id,
            },
        )


@pytest.mark.asyncio
async def test_vulnerability_evidence_cannot_be_deleted_or_truncated(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization = Organization(name="R4 Delete", slug=f"r4-delete-{uuid.uuid4().hex}")
    integration_session.add(organization)
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="r4-delete.example.test",
        scan_mode=ScanModeEnum.STANDARD,
    )
    integration_session.add(run)
    await integration_session.flush()
    vulnerability = Vulnerability(
        organization_id=organization.id,
        run_id=run.id,
        title="Immutable delete",
        description="Evidence cannot be deleted.",
        severity=SeverityEnum.HIGH,
        cvss_score=8.0,
        cve_id=None,
        affected_target="r4-delete.example.test",
        poc_reproduction_raw="poc",
    )
    integration_session.add(vulnerability)
    await integration_session.flush()

    with pytest.raises(DBAPIError, match="inmutables"):
        await integration_session.execute(
            text("DELETE FROM vulnerabilities WHERE id = :vulnerability_id"),
            {"vulnerability_id": vulnerability.id},
        )


@pytest.mark.asyncio
async def test_vulnerability_table_cannot_be_truncated(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None

    with pytest.raises(DBAPIError, match="inmutables"):
        await integration_session.execute(text("TRUNCATE vulnerabilities"))


@pytest.mark.asyncio
async def test_vulnerability_provenance_cannot_be_changed(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization = Organization(name="R4 Provenance", slug=f"r4-prov-{uuid.uuid4().hex}")
    integration_session.add(organization)
    await integration_session.flush()
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="r4-prov.example.test",
        scan_mode=ScanModeEnum.STANDARD,
    )
    integration_session.add(run)
    await integration_session.flush()
    vulnerability = Vulnerability(
        organization_id=organization.id,
        run_id=run.id,
        source_finding_id="strix-001",
        title="Immutable provenance",
        description="The external finding ID cannot change.",
        severity=SeverityEnum.HIGH,
        cvss_score=8.0,
        cve_id=None,
        affected_target="r4-prov.example.test",
        poc_reproduction_raw="poc",
    )
    integration_session.add(vulnerability)
    await integration_session.flush()

    with pytest.raises(DBAPIError, match="inmutables"):
        await integration_session.execute(
            text(
                "UPDATE vulnerabilities "
                "SET source_finding_id = :source_id "
                "WHERE id = :vulnerability_id"
            ),
            {"source_id": "strix-002", "vulnerability_id": vulnerability.id},
        )


@pytest.mark.asyncio
async def test_vulnerability_cannot_reference_a_run_from_another_tenant(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    suffix = uuid.uuid4().hex
    organization_a = Organization(name=f"R4 Alpha {suffix}", slug=f"r4-alpha-{suffix}")
    organization_b = Organization(name=f"R4 Beta {suffix}", slug=f"r4-beta-{suffix}")
    integration_session.add_all([organization_a, organization_b])
    await integration_session.flush()
    run_b = PentestRun(
        organization_id=organization_b.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="r4-beta.example.test",
        scan_mode=ScanModeEnum.STANDARD,
    )
    integration_session.add(run_b)
    await integration_session.flush()
    invalid_vulnerability = Vulnerability(
        organization_id=organization_a.id,
        run_id=run_b.id,
        title="Cross tenant finding",
        description="The database must reject this relationship.",
        severity=SeverityEnum.HIGH,
        cvss_score=8.0,
        cve_id=None,
        affected_target="r4-beta.example.test",
        poc_reproduction_raw="poc",
    )
    integration_session.add(invalid_vulnerability)

    with pytest.raises(IntegrityError):
        await integration_session.flush()
