"""Consultas agregadas y de solo lectura para el resumen del dashboard."""

from __future__ import annotations

import datetime as dt
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.dashboard.schemas import (
    DashboardSummaryResponse,
    FindingsTrendPoint,
    RepositoryDashboardItem,
    RepositoryMonitoringStatusEnum,
    SeverityCount,
    StatusCount,
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

#: Orden del ciclo de vida de la remediación. No es el alfabético: es el orden en el que un
#: hallazgo avanza, y es el que se lee sin pensar en la pantalla de triaje.
_STATUS_ORDER = (
    IssueStatusEnum.OPEN,
    IssueStatusEnum.IN_PROGRESS,
    IssueStatusEnum.REMEDIATION_PROPOSED,
    IssueStatusEnum.FIXED,
    IssueStatusEnum.SNOOZED,
    IssueStatusEnum.IGNORED,
)

#: Ventana de la serie temporal, en días. Es la misma que usa el resumen de agentes para los
#: escaneos, y por el mismo motivo: treinta días es un ciclo de corrección completo en una
#: cadencia semanal, así que la serie responde «¿voy bien?» en vez de «¿qué pasó hoy?».
DIAS_DE_SERIE = 30


async def _findings_trend(
    session: AsyncSession,
    organization_id: UUID,
    now: datetime,
) -> list[FindingsTrendPoint]:
    """Hallazgos detectados por día en la ventana, desglosados por severidad.

    ## Por qué la serie se calcula aquí y no la arma el panel

    Porque la API de issues está paginada. Un «hallazgos por día» montado en el navegador solo
    podría mirar la página que tiene delante —veinticinco filas—, y daría dos mentiras a la vez:
    que no hay hallazgos en las fechas que no salen en esa página, y que los hay en las que sí
    salen pero repetidos de cada vez que se pasa de página. Con cuatro mil hallazgos abiertos, la
    gráfica mentiría en voz alta. Aquí la cuenta la base de datos sobre el conjunto entero, con
    el filtro de organización aplicado, que es además la única forma de que el aislamiento
    multi-tenant (R3) no dependa de qué filas le interpretada el cliente.

    ## Por qué los días vacíos se rellenan con cero

    Porque un gráfico con huecos **lee** como si faltaran datos. Un día sin hallazgos y un día sin
    consultar se ven igual, y quien lee la gráfica concluiría que el escaneo del martes falló.
    Con el cero explícito, la línea baja sola y el vacío se lee como vacío.

    ## Por qué el desglose va por severidad y no por estado

    Porque la severidad es la lectura que ya conoce quien mira un hallazgo, y porque el estado
    cambia con el triaje mientras que la severidad no cambia nunca. Dibujar el estado en la
    serie dibujaría una curva que además depende de quién ha pulsado botones.
    """

    desde = now - dt.timedelta(days=DIAS_DE_SERIE - 1)
    filas = (
        await session.execute(
            select(
                func.date_trunc("day", Vulnerability.discovered_at).label("dia"),
                Vulnerability.severity,
                func.count(Vulnerability.id),
            )
            .where(
                Vulnerability.organization_id == organization_id,
                Vulnerability.discovered_at >= desde,
            )
            .group_by("dia", Vulnerability.severity)
        )
    ).all()

    por_dia: dict[dt.date, dict[SeverityEnum, int]] = {}
    for dia, severity, total in filas:
        por_dia.setdefault(dia.date(), {})[severity] = int(total)

    serie: list[FindingsTrendPoint] = []
    for desplazamiento in range(DIAS_DE_SERIE):
        dia = (desde + dt.timedelta(days=desplazamiento)).date()
        conteo = por_dia.get(dia, {})
        # Las cinco claves siempre presentes: el panel dibuja una serie por severidad y necesita
        # distinguir «cero» de «no hay dato». Ver `FindingsTrendPoint`.
        reparto = {severity: conteo.get(severity, 0) for severity in _SEVERITY_ORDER}
        serie.append(
            FindingsTrendPoint(
                dia=dia,
                total=sum(reparto.values()),
                por_severidad=reparto,
            )
        )
    return serie


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
    findings_trend = await _findings_trend(session, organization_id, current_time)
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
        status_distribution=[
            StatusCount(status=status, total=status_counts.get(status, 0))
            for status in _STATUS_ORDER
        ],
        findings_trend=findings_trend,
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
