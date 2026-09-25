"""Pruebas del endpoint de resumen para el dashboard multi-tenant."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.dashboard.score import compute_security_score
from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.vulnerabilities.models import IssueStatusEnum, SeverityEnum, Vulnerability
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({}, 100),
        ({"INFO": 99}, 100),
        ({"LOW": 4}, 99),
        ({"MEDIUM": 1}, 99),
        ({"HIGH": 1}, 98),
        ({"CRITICAL": 1}, 95),
        ({"CRITICAL": 20}, 0),
        ({"CRITICAL": 40}, 0),
    ],
)
def test_security_score_is_deterministic_and_clamped(
    counts: dict[str, int],
    expected: int,
) -> None:
    assert compute_security_score(counts) == expected


async def _seed_tenant(session: AsyncSession) -> tuple[Organization, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Dashboard {suffix}", slug=f"dashboard-{suffix}")
    user = User(
        email=f"dashboard-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Dashboard Admin",
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


def _make_vulnerability(
    organization_id: uuid.UUID,
    run_id: uuid.UUID,
    severity: SeverityEnum,
    status: IssueStatusEnum,
    index: int,
) -> Vulnerability:
    return Vulnerability(
        organization_id=organization_id,
        run_id=run_id,
        source_finding_id=f"finding-{index}-{severity.value}-{status.value}",
        title=f"Hallazgo {index}",
        description="Descripcion del hallazgo",
        severity=severity,
        cvss_score=7.5,
        affected_target="acme/app#PR-1",
        poc_reproduction_raw="curl https://example.com",
        status=status,
    )


@pytest.mark.asyncio
async def test_dashboard_summary_of_empty_tenant(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    _organization, headers = await _seed_tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/dashboard/summary", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["security_score"] == 100
    assert payload["open_issues"] == 0
    assert payload["total_issues"] == 0
    assert payload["fix_rate"] == 0.0
    assert payload["prs_reviewed"] == 0
    assert payload["prs_reviewed_total"] == 0
    assert payload["pentests_total"] == 0
    assert payload["repositories"] == []
    assert payload["repositories_monitored"] == 0
    assert [item["severity"] for item in payload["severity_distribution"]] == [
        "CRITICAL",
        "HIGH",
        "MEDIUM",
        "LOW",
        "INFO",
    ]
    assert all(item["total"] == 0 for item in payload["severity_distribution"])


@pytest.mark.asyncio
async def test_dashboard_summary_aggregates_findings_reviews_and_repositories(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, headers = await _seed_tenant(integration_session)
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id="900",
        name="app",
        full_name="acme/app",
        clone_url="https://github.com/acme/app.git",
        pr_reviews_enabled=True,
        webhook_id="webhook-900",
    )
    idle_repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITLAB,
        remote_repo_id="901",
        name="legacy",
        full_name="acme/legacy",
        clone_url="https://gitlab.com/acme/legacy.git",
        pr_reviews_enabled=False,
    )
    integration_session.add_all([repository, idle_repository])
    await integration_session.flush()

    finished_at = datetime.now(UTC) - timedelta(hours=6)
    review_passed = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        pr_number=1,
        pr_title="Ajustes de autenticacion",
        pr_author="octocat",
        source_branch="feature/auth",
        target_branch="main",
        commit_sha="a" * 40,
        status=PRReviewStatusEnum.PASSED,
        finished_at=finished_at,
    )
    review_scanning = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        pr_number=2,
        pr_title="Cambio en curso",
        pr_author="hubot",
        source_branch="feature/live",
        target_branch="main",
        commit_sha="b" * 40,
        status=PRReviewStatusEnum.SCANNING,
    )
    old_review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        pr_number=3,
        pr_title="Revision antigua",
        pr_author="octocat",
        source_branch="feature/old",
        target_branch="main",
        commit_sha="c" * 40,
        status=PRReviewStatusEnum.FAILED,
        finished_at=finished_at - timedelta(days=90),
    )
    integration_session.add_all([review_passed, review_scanning, old_review])
    await integration_session.flush()

    review_run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.REPOSITORY,
        target_identifier="acme/app#PR-1",
        scan_mode=ScanModeEnum.QUICK,
        status=ScanStatusEnum.COMPLETED,
    )
    review_scanning_run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.REPOSITORY,
        target_identifier="acme/app#PR-2",
        scan_mode=ScanModeEnum.QUICK,
        status=ScanStatusEnum.RUNNING,
    )
    deep_run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="app.example.com",
        scan_mode=ScanModeEnum.DEEP,
        status=ScanStatusEnum.COMPLETED,
    )
    integration_session.add_all([review_run, review_scanning_run, deep_run])
    await integration_session.flush()
    review_passed.run_id = review_run.id
    review_scanning.run_id = review_scanning_run.id
    old_review.run_id = review_run.id

    integration_session.add_all(
        [
            _make_vulnerability(
                organization.id, review_run.id, SeverityEnum.CRITICAL, IssueStatusEnum.OPEN, 1
            ),
            _make_vulnerability(
                organization.id, review_run.id, SeverityEnum.HIGH, IssueStatusEnum.FIXED, 2
            ),
            _make_vulnerability(
                organization.id, review_run.id, SeverityEnum.LOW, IssueStatusEnum.OPEN, 3
            ),
            _make_vulnerability(
                organization.id, deep_run.id, SeverityEnum.MEDIUM, IssueStatusEnum.IGNORED, 4
            ),
        ]
    )
    await integration_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/dashboard/summary", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["open_issues"] == 2
    assert payload["total_issues"] == 4
    assert payload["security_score"] == 94  # CRITICAL (5) + LOW (0.25) = 5.25 -> 6 puntos
    assert payload["fix_rate"] == 0.3333
    assert payload["prs_reviewed"] == 1
    assert payload["prs_reviewed_total"] == 3
    assert payload["pentests_total"] == 1
    assert payload["repositories_monitored"] == 1

    distribution = {item["severity"]: item["total"] for item in payload["severity_distribution"]}
    assert distribution == {"CRITICAL": 1, "HIGH": 0, "MEDIUM": 0, "LOW": 1, "INFO": 0}

    repositories = {item["full_name"]: item for item in payload["repositories"]}
    assert set(repositories) == {"acme/app", "acme/legacy"}
    assert repositories["acme/app"]["status"] == "SCANNING"
    assert repositories["acme/app"]["open_vulnerabilities"] == 2
    assert repositories["acme/app"]["last_tested_at"] is not None
    assert repositories["acme/app"]["webhook_registered"] is True
    assert repositories["acme/legacy"]["status"] == "NOT_TESTED"
    assert repositories["acme/legacy"]["open_vulnerabilities"] == 0
    assert repositories["acme/legacy"]["last_tested_at"] is None


@pytest.mark.asyncio
async def test_dashboard_summary_never_leaks_other_tenants(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization_a, headers_a = await _seed_tenant(integration_session)
    organization_b, _headers_b = await _seed_tenant(integration_session)
    repository_b = Repository(
        organization_id=organization_b.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id="950",
        name="secreto",
        full_name="beta/secreto",
        clone_url="https://github.com/beta/secreto.git",
    )
    integration_session.add(repository_b)
    await integration_session.flush()
    run_b = PentestRun(
        organization_id=organization_b.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="secreto.example.com",
        scan_mode=ScanModeEnum.DEEP,
        status=ScanStatusEnum.COMPLETED,
    )
    integration_session.add(run_b)
    await integration_session.flush()
    integration_session.add(
        _make_vulnerability(
            organization_b.id,
            run_b.id,
            SeverityEnum.CRITICAL,
            IssueStatusEnum.OPEN,
            9,
        )
    )
    await integration_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/dashboard/summary", headers=headers_a)

    assert response.status_code == 200
    payload = response.json()
    assert payload["repositories"] == []
    assert payload["open_issues"] == 0
    assert payload["total_issues"] == 0
    assert payload["security_score"] == 100
    assert organization_a.id != organization_b.id
    assert "secreto" not in response.text
