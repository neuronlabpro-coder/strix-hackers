"""Consultas agregadas y de solo lectura para el resumen del dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.dashboard.schemas import (
    DashboardSummaryResponse,
    RepositoryDashboardItem,
    RepositoryMonitoringStatusEnum,
    SeverityCount,
)
from backend.apps.dashboard.score import compute_security_score
from backend.apps.pentests.models import PentestRun, ScanModeEnum
from backend.apps.repositories.models import (
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.vulnerabilities.models import IssueStatusEnum, SeverityEnum, Vulnerability

_OPEN_ISSUE_STATUSES = (IssueStatusEnum.OPEN, IssueStatusEnum.IN_PROGRESS)
_EXCLUDED_FROM_FIX_RATE = (IssueStatusEnum.IGNORED, IssueStatusEnum.SNOOZED)
_TERMINAL_REVIEW_STATUSES = (
    PRReviewStatusEnum.PASSED,
    PRReviewStatusEnum.FAILED,
    PRReviewStatusEnum.ERROR,
)
_IN_PROGRESS_REVIEW_STATUSES = (PRReviewStatusEnum.QUEUED, PRReviewStatusEnum.SCANNING)
_REVIEW_WINDOW = timedelta(days=30)
_FIX_RATE_PRECISION = 4
_SEVERITY_ORDER = (
    SeverityEnum.CRITICAL,
    SeverityEnum.HIGH,
    SeverityEnum.MEDIUM,
    SeverityEnum.LOW,
    SeverityEnum.INFO,
)


async def _severity_counts(session: AsyncSession, organization_id: UUID) -> dict[SeverityEnum, int]:
    result = await session.execute(
        select(Vulnerability.severity, func.count(Vulnerability.id))
        .where(
            Vulnerability.organization_id == organization_id,
            Vulnerability.status.in_(_OPEN_ISSUE_STATUSES),
        )
        .group_by(Vulnerability.severity)
    )
    return {severity: int(total) for severity, total in result.all()}


async def _status_counts(
    session: AsyncSession,
    organization_id: UUID,
) -> dict[IssueStatusEnum, int]:
    result = await session.execute(
        select(Vulnerability.status, func.count(Vulnerability.id))
        .where(Vulnerability.organization_id == organization_id)
        .group_by(Vulnerability.status)
    )
    return {status: int(total) for status, total in result.all()}


async def _review_metrics(
    session: AsyncSession,
    organization_id: UUID,
    now: datetime,
) -> tuple[int, int]:
    total_result = await session.execute(
        select(func.count(PullRequestReview.id)).where(
            PullRequestReview.organization_id == organization_id
        )
    )
    reviewed_result = await session.execute(
        select(func.count(PullRequestReview.id)).where(
            PullRequestReview.organization_id == organization_id,
            PullRequestReview.status.in_(_TERMINAL_REVIEW_STATUSES),
            PullRequestReview.finished_at.is_not(None),
            PullRequestReview.finished_at >= now - _REVIEW_WINDOW,
        )
    )
    return int(reviewed_result.scalar_one()), int(total_result.scalar_one())


async def _pentest_total(session: AsyncSession, organization_id: UUID) -> int:
    result = await session.execute(
        select(func.count(PentestRun.id)).where(
            PentestRun.organization_id == organization_id,
            PentestRun.scan_mode != ScanModeEnum.QUICK,
        )
    )
    return int(result.scalar_one())


async def _repository_open_vulnerabilities(
    session: AsyncSession,
    organization_id: UUID,
) -> dict[UUID, int]:
    """Aggregate hallazgos abiertos de revisiones de PR por repositorio.

    Se cuentan vulnerabilidades distintas porque una misma ejecución puede quedar
    referenciada por más de una revisión (por ejemplo, un rerun del mismo commit).
    """

    result = await session.execute(
        select(PullRequestReview.repository_id, func.count(Vulnerability.id.distinct()))
        .join(PentestRun, PentestRun.id == PullRequestReview.run_id)
        .join(Vulnerability, Vulnerability.run_id == PentestRun.id)
        .where(
            PullRequestReview.organization_id == organization_id,
            PullRequestReview.run_id.is_not(None),
            Vulnerability.organization_id == organization_id,
            Vulnerability.status.in_(_OPEN_ISSUE_STATUSES),
        )
        .group_by(PullRequestReview.repository_id)
    )
    return {repository_id: int(total) for repository_id, total in result.all()}


async def _repository_review_state(
    session: AsyncSession,
    organization_id: UUID,
) -> tuple[dict[UUID, datetime], set[UUID]]:
    """Devuelve la última auditoría finalizada y los repositorios con revisión en curso."""

    last_tested_result = await session.execute(
        select(PullRequestReview.repository_id, func.max(PullRequestReview.finished_at))
        .where(
            PullRequestReview.organization_id == organization_id,
            PullRequestReview.finished_at.is_not(None),
        )
        .group_by(PullRequestReview.repository_id)
    )
    in_progress_result = await session.execute(
        select(PullRequestReview.repository_id).where(
            PullRequestReview.organization_id == organization_id,
            PullRequestReview.status.in_(_IN_PROGRESS_REVIEW_STATUSES),
        )
    )
    last_tested = {
        repository_id: last_tested_at
        for repository_id, last_tested_at in last_tested_result.all()
        if last_tested_at is not None
    }
    return last_tested, set(in_progress_result.scalars())


def _monitoring_status(
    repository_id: UUID,
    last_tested: dict[UUID, datetime],
    scanning: set[UUID],
) -> RepositoryMonitoringStatusEnum:
    if repository_id in scanning:
        return RepositoryMonitoringStatusEnum.SCANNING
    if repository_id in last_tested:
        return RepositoryMonitoringStatusEnum.TESTED
    return RepositoryMonitoringStatusEnum.NOT_TESTED


async def build_dashboard_summary(
    session: AsyncSession,
    organization_id: UUID,
    now: datetime | None = None,
) -> DashboardSummaryResponse:
    """Agrega postura, hallazgos y repositorios acotados a una organización."""

    current_time = now or datetime.now(UTC)
    severity_counts = await _severity_counts(session, organization_id)
    status_counts = await _status_counts(session, organization_id)
    prs_reviewed, prs_reviewed_total = await _review_metrics(session, organization_id, current_time)
    pentests_total = await _pentest_total(session, organization_id)
    open_by_repository = await _repository_open_vulnerabilities(session, organization_id)
    last_tested, scanning = await _repository_review_state(session, organization_id)

    repositories_result = await session.execute(
        select(Repository)
        .where(Repository.organization_id == organization_id)
        .order_by(Repository.created_at.desc(), Repository.id)
    )
    repositories = repositories_result.scalars().all()

    open_issues = sum(severity_counts.values())
    fixable_total = sum(
        total
        for status, total in status_counts.items()
        if status not in _EXCLUDED_FROM_FIX_RATE
    )
    fixed_issues = status_counts.get(IssueStatusEnum.FIXED, 0)
    fix_rate = round(fixed_issues / fixable_total, _FIX_RATE_PRECISION) if fixable_total else 0.0

    return DashboardSummaryResponse(
        security_score=compute_security_score(
            {severity.value: total for severity, total in severity_counts.items()}
        ),
        open_issues=open_issues,
        total_issues=sum(status_counts.values()),
        fix_rate=fix_rate,
        prs_reviewed=prs_reviewed,
        prs_reviewed_total=prs_reviewed_total,
        pentests_total=pentests_total,
        repositories_monitored=sum(
            1
            for repository in repositories
            if repository.pr_reviews_enabled and repository.is_active
        ),
        severity_distribution=[
            SeverityCount(severity=severity, total=severity_counts.get(severity, 0))
            for severity in _SEVERITY_ORDER
        ],
        repositories=[
            RepositoryDashboardItem(
                id=repository.id,
                provider=repository.provider,
                name=repository.name,
                full_name=repository.full_name,
                is_active=repository.is_active,
                pr_reviews_enabled=repository.pr_reviews_enabled,
                webhook_registered=repository.webhook_id is not None,
                status=_monitoring_status(repository.id, last_tested, scanning),
                open_vulnerabilities=open_by_repository.get(repository.id, 0),
                last_tested_at=last_tested.get(repository.id),
            )
            for repository in repositories
        ],
        generated_at=current_time,
    )
