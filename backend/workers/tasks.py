"""Tareas Celery de ejecución e ingesta de Strix."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from billiard.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from celery.signals import worker_ready
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.apps.pentests.models import PentestRun, ScanStatusEnum
from backend.apps.repositories.models import (
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.vulnerabilities.models import Vulnerability
from backend.core.config import settings
from backend.core.database import create_database_engine
from backend.workers.celery_app import celery_app
from backend.workers.parser.strix_parser import extract_strix_scan_id, parse_strix_output
from backend.workers.runner.exceptions import SandboxCleanupError, SandboxTimeoutError
from backend.workers.runner.sandbox import StrixSandboxManager

logger = logging.getLogger(__name__)
KillContainer = Callable[[str], None]


class StrixIngestionError(RuntimeError):
    """Error de dominio al asociar una salida con un run válido."""


def _parse_uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError) as error:
        raise StrixIngestionError(f"{field_name} no contiene un UUID válido") from error


def _session_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_database_engine(settings)
    return engine, async_sessionmaker[AsyncSession](
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _get_run_organization_id(run_id: UUID) -> UUID | None:
    """Resuelve el tenant de un run interno antes de aplicar estados de error."""

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(PentestRun.organization_id).where(PentestRun.id == run_id)
            )
            return result.scalar_one_or_none()
    finally:
        await engine.dispose()


async def _mark_failed(
    organization_id: UUID | None,
    run_id: UUID,
    error_code: str,
    *,
    exit_code: str | None = None,
    container_id: str | None = None,
) -> None:
    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            conditions = [PentestRun.id == run_id]
            if organization_id is not None:
                conditions.append(PentestRun.organization_id == organization_id)
            result = await session.execute(
                select(PentestRun).where(*conditions).with_for_update()
            )
            run = result.scalar_one_or_none()
            if run is None or run.status not in {
                ScanStatusEnum.QUEUED,
                ScanStatusEnum.RUNNING,
            }:
                return
            if exit_code is not None:
                run.exit_code = exit_code
            if container_id is not None:
                run.container_id = container_id
            run.status = ScanStatusEnum.FAILED
            run.finished_at = datetime.now(UTC)
            run.error_message = error_code
            await session.commit()
    finally:
        await engine.dispose()


async def mark_run_timed_out(
    session: AsyncSession,
    run_id: UUID,
    organization_id: UUID | None = None,
) -> bool:
    """Transiciona un run activo a TIMED_OUT dentro de la sesión del worker."""

    conditions = [PentestRun.id == run_id]
    if organization_id is not None:
        conditions.append(PentestRun.organization_id == organization_id)
    result = await session.execute(
        select(PentestRun).where(*conditions).with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is None or run.status not in {
        ScanStatusEnum.QUEUED,
        ScanStatusEnum.RUNNING,
    }:
        return False
    run.status = ScanStatusEnum.TIMED_OUT
    run.finished_at = datetime.now(UTC)
    run.error_message = "STRIX_TIMEOUT"
    await session.commit()
    return True


async def _mark_timed_out(
    organization_id: UUID | None,
    run_id: UUID,
) -> None:
    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            await mark_run_timed_out(session, run_id, organization_id)
    finally:
        await engine.dispose()


async def _set_container_reference(
    organization_id: UUID,
    run_id: UUID,
    container_reference: str,
) -> bool:
    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(PentestRun)
                .where(
                    PentestRun.id == run_id,
                    PentestRun.organization_id == organization_id,
                )
                .with_for_update()
            )
            run = result.scalar_one_or_none()
            if run is None or run.status not in {
                ScanStatusEnum.QUEUED,
                ScanStatusEnum.RUNNING,
            }:
                return False
            run.container_id = container_reference
            await session.commit()
            return True
    finally:
        await engine.dispose()


async def _set_cleanup_pending(
    organization_id: UUID,
    run_id: UUID,
    cleanup_pending: bool,
) -> None:
    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(PentestRun)
                .where(
                    PentestRun.id == run_id,
                    PentestRun.organization_id == organization_id,
                )
                .with_for_update()
            )
            run = result.scalar_one_or_none()
            if run is None:
                return
            run.cleanup_pending = cleanup_pending
            await session.commit()
    finally:
        await engine.dispose()


async def _claim_run_for_execution(
    run_id: UUID,
) -> tuple[UUID, str, str, str] | None:
    """Cambia un run QUEUED a RUNNING de forma atómica y devuelve su contexto."""

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(PentestRun).where(PentestRun.id == run_id).with_for_update()
            )
            run = result.scalar_one_or_none()
            if run is None:
                raise StrixIngestionError("El run no existe")
            if run.status != ScanStatusEnum.QUEUED:
                return None
            run.status = ScanStatusEnum.RUNNING
            run.started_at = datetime.now(UTC)
            await session.commit()
            return (
                run.organization_id,
                run.target_identifier,
                str(run.scan_mode),
                str(run.target_type),
            )
    finally:
        await engine.dispose()


def _same_immutable_evidence(
    stored: Vulnerability,
    incoming: Vulnerability,
) -> bool:
    """Compara el contenido R4 completo, no solo el ID externo del finding."""

    immutable_fields = (
        "organization_id",
        "run_id",
        "source_finding_id",
        "title",
        "description",
        "severity",
        "cvss_score",
        "cve_id",
        "affected_target",
        "affected_line",
        "poc_reproduction_raw",
        "autofix_patch_diff",
    )
    return all(getattr(stored, field) == getattr(incoming, field) for field in immutable_fields)


async def _ingest_output(
    organization_id: UUID,
    run_id: UUID,
    output_json: str,
    expected_scan_id: str,
    *,
    exit_code: str | None = None,
    container_id: str | None = None,
) -> int:
    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            try:
                result = await session.execute(
                    select(PentestRun)
                    .where(
                        PentestRun.id == run_id,
                        PentestRun.organization_id == organization_id,
                    )
                    .with_for_update()
                )
                run = result.scalar_one_or_none()
                if run is None:
                    raise StrixIngestionError("El run no existe para la organización indicada")
                if run.source_scan_id is not None and run.source_scan_id != expected_scan_id:
                    raise StrixIngestionError("El scan_id no coincide con el run ya ingerido")
                run.source_scan_id = expected_scan_id
                if exit_code is not None:
                    run.exit_code = exit_code
                if container_id is not None:
                    run.container_id = container_id

                findings = parse_strix_output(
                    output_json,
                    organization_id=organization_id,
                    run_id=run_id,
                    db=None,
                    expected_scan_id=expected_scan_id,
                )
                incoming_ids = {
                    finding.source_finding_id
                    for finding in findings
                    if finding.source_finding_id is not None
                }
                if run.status == ScanStatusEnum.COMPLETED:
                    stored_result = await session.execute(
                        select(Vulnerability).where(
                            Vulnerability.run_id == run_id,
                            Vulnerability.organization_id == organization_id,
                        )
                    )
                    stored_findings = list(stored_result.scalars().all())
                    stored_ids = {
                        finding.source_finding_id
                        for finding in stored_findings
                        if finding.source_finding_id is not None
                    }
                    if stored_ids != incoming_ids or any(
                        not _same_immutable_evidence(stored, incoming)
                        for stored, incoming in zip(
                            sorted(
                                stored_findings,
                                key=lambda finding: finding.source_finding_id or "",
                            ),
                            sorted(
                                findings,
                                key=lambda finding: finding.source_finding_id or "",
                            ),
                            strict=False,
                        )
                    ):
                        raise StrixIngestionError(
                            "El reporte no coincide con los findings ya ingeridos"
                        )
                    await session.commit()
                    return len(findings)
                if run.status not in {ScanStatusEnum.QUEUED, ScanStatusEnum.RUNNING}:
                    raise StrixIngestionError("El run no está en un estado susceptible de ingesta")

                session.add_all(findings)
                run.status = ScanStatusEnum.COMPLETED
                run.finished_at = datetime.now(UTC)
                await session.commit()
                return len(findings)
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


async def _execute_pentest_run(run_id: UUID) -> str:
    claimed = await _claim_run_for_execution(run_id)
    if claimed is None:
        return "SKIPPED"
    organization_id, target, scan_mode, target_type = claimed
    manager = StrixSandboxManager(
        str(run_id),
        target,
        scan_mode,
        target_type=target_type,
    )
    started_event = threading.Event()
    started_container_ids: list[str] = []

    def on_started(container_id: str) -> None:
        started_container_ids.append(container_id)
        started_event.set()

    manager_task = asyncio.create_task(
        asyncio.to_thread(
            manager.run,
            timeout_seconds=settings.strix_hard_timeout_seconds,
            soft_timeout_seconds=settings.strix_soft_timeout_seconds,
            on_started=on_started,
        )
    )
    while not started_event.is_set() and not manager_task.done():
        await asyncio.sleep(0.05)

    if started_event.is_set():
        container_id = started_container_ids[0]
        container_reference_set = await _set_container_reference(
            organization_id,
            run_id,
            container_id,
        )
        if not container_reference_set:
            try:
                StrixSandboxManager.kill_container(container_id, client=manager.client)
            except Exception:
                logger.exception("No se pudo detener el contenedor ya iniciado %s", run_id)
            try:
                await manager_task
            except Exception:
                logger.exception("El sandbox abortado %s terminó con error", run_id)
            if manager.cleanup_pending:
                await _set_cleanup_pending(organization_id, run_id, True)
            return "ABORTED"

    try:
        result = await manager_task
    except BaseException:
        if manager.cleanup_pending:
            await _set_cleanup_pending(organization_id, run_id, True)
        raise
    if result.exit_code != 0:
        await _mark_failed(
            organization_id,
            run_id,
            "STRIX_NONZERO_EXIT",
            exit_code=str(result.exit_code),
            container_id=result.container_id,
        )
        return "FAILED"
    expected_scan_id = extract_strix_scan_id(result.output_json)
    await _ingest_output(
        organization_id,
        run_id,
        result.output_json,
        expected_scan_id,
        exit_code=str(result.exit_code),
        container_id=result.container_id,
    )
    return "COMPLETED"


async def reconcile_orphaned_runs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    kill_container: KillContainer | None = None,
    workspace_root: Path | str | None = None,
) -> int:
    """Marca como FAILED runs RUNNING obsoletos y limpia sus contenedores."""

    current_time = now or datetime.now(UTC)
    stale_before = current_time - timedelta(seconds=settings.strix_watchdog_stale_after_seconds)
    result = await session.execute(
        select(PentestRun)
        .where(
            or_(
                and_(
                    PentestRun.status == ScanStatusEnum.RUNNING,
                    or_(
                        PentestRun.started_at.is_(None),
                        PentestRun.started_at < stale_before,
                    ),
                ),
                PentestRun.cleanup_pending.is_(True),
            )
        )
        .with_for_update()
    )
    stale_runs = list(result.scalars().all())
    for run in stale_runs:
        cleanup_succeeded = True
        container_reference = run.container_id or StrixSandboxManager.container_name_for_run(
            str(run.id)
        )
        try:
            if kill_container is None:
                StrixSandboxManager.kill_container(
                    container_reference,
                    expected_run_id=str(run.id),
                )
            else:
                kill_container(container_reference)
        except Exception:
            cleanup_succeeded = False
            logger.exception("No se pudo limpiar el contenedor huérfano %s", run.id)
        try:
            StrixSandboxManager.remove_network_for_run(str(run.id))
        except Exception:
            cleanup_succeeded = False
            logger.exception("No se pudo limpiar la red huérfana %s", run.id)
        try:
            StrixSandboxManager.purge_workspace(str(run.id), workspace_root)
        except (OSError, SandboxCleanupError):
            cleanup_succeeded = False
            logger.exception("No se pudo purgar el workspace huérfano %s", run.id)
        run.cleanup_pending = not cleanup_succeeded
        if run.status == ScanStatusEnum.RUNNING:
            run.status = ScanStatusEnum.FAILED
            run.finished_at = current_time
            run.error_message = "WORKER_WATCHDOG_ORPHANED"
            review_result = await session.execute(
                select(PullRequestReview)
                .where(
                    PullRequestReview.run_id == run.id,
                    PullRequestReview.organization_id == run.organization_id,
                )
                .with_for_update()
            )
            for review in review_result.scalars().all():
                if review.status in {
                    PRReviewStatusEnum.QUEUED,
                    PRReviewStatusEnum.SCANNING,
                }:
                    review.status = PRReviewStatusEnum.ERROR
                    review.finished_at = current_time
    queued_before = current_time - timedelta(seconds=settings.pr_review_stale_after_seconds)
    terminal_review_result = await session.execute(
        select(PullRequestReview)
        .join(PentestRun, PentestRun.id == PullRequestReview.run_id)
        .where(
            PullRequestReview.status == PRReviewStatusEnum.SCANNING,
            PullRequestReview.organization_id == PentestRun.organization_id,
            PullRequestReview.created_at < queued_before,
            PentestRun.status.in_(
                {
                    ScanStatusEnum.COMPLETED,
                    ScanStatusEnum.FAILED,
                    ScanStatusEnum.TIMED_OUT,
                    ScanStatusEnum.ABORTED,
                }
            ),
            PentestRun.finished_at.is_not(None),
            PentestRun.finished_at < queued_before,
        )
    )
    for terminal_review in terminal_review_result.scalars().all():
        terminal_review.status = PRReviewStatusEnum.ERROR
        terminal_review.finished_at = current_time
    queued_result = await session.execute(
        select(PullRequestReview)
        .join(Repository, Repository.id == PullRequestReview.repository_id)
        .where(
            PullRequestReview.status == PRReviewStatusEnum.QUEUED,
            PullRequestReview.created_at < queued_before,
            PullRequestReview.organization_id == Repository.organization_id,
            Repository.is_active.is_(True),
            Repository.pr_reviews_enabled.is_(True),
        )
    )
    from backend.apps.repositories.tasks import run_pr_security_pipeline

    for queued_review in queued_result.scalars().all():
        try:
            run_pr_security_pipeline.delay(  # pyright: ignore[reportFunctionMemberAccess]
                str(queued_review.id)
            )
        except Exception:
            logger.exception(
                "No se pudo reencolar la revisión PR obsoleta %s",
                queued_review.id,
            )
    await session.commit()
    return len(stale_runs)


async def _run_watchdog() -> int:
    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            return await reconcile_orphaned_runs(session)
    finally:
        await engine.dispose()


@celery_app.task(name="pentests.execute")
def execute_pentest_run(run_id: str) -> str:
    """Ejecuta un sandbox y persiste su resultado de forma transaccional.

    El trabajo largo se delega en ``_execute_pentest_run`` para que el límite de
    Celery pueda interrumpirlo y el sandbox limpie sus recursos en ``finally``.
    """

    parsed_run_id = _parse_uuid(run_id, "run_id")
    organization_id: UUID | None = None
    try:
        organization_id = asyncio.run(_get_run_organization_id(parsed_run_id))
        return asyncio.run(_execute_pentest_run(parsed_run_id))
    except (SandboxTimeoutError, SoftTimeLimitExceeded, TimeLimitExceeded):
        logger.warning("El run %s superó el timeout de Strix", parsed_run_id)
        try:
            asyncio.run(_mark_timed_out(organization_id, parsed_run_id))
        except Exception:
            logger.exception("No se pudo marcar el run %s como TIMED_OUT", parsed_run_id)
        raise
    except Exception:
        logger.exception("Falló la ejecución del run %s", parsed_run_id)
        try:
            asyncio.run(_mark_failed(organization_id, parsed_run_id, "STRIX_EXECUTION_FAILED"))
        except Exception:
            logger.exception("No se pudo marcar el run %s como FAILED", parsed_run_id)
        raise


@celery_app.task(name="pentests.ingest_strix_output")
def ingest_strix_output(
    organization_id: str,
    run_id: str,
    output_json: str,
    expected_scan_id: str,
) -> int:
    """Persiste atómicamente un reporte Strix y cierra el run como completado."""

    parsed_organization_id = _parse_uuid(organization_id, "organization_id")
    parsed_run_id = _parse_uuid(run_id, "run_id")
    try:
        if not expected_scan_id.strip():
            raise StrixIngestionError("expected_scan_id no puede estar vacío")
        return asyncio.run(
            _ingest_output(
                parsed_organization_id,
                parsed_run_id,
                output_json,
                expected_scan_id,
            )
        )
    except Exception:
        logger.exception("Falló la ingesta de resultados Strix para run %s", parsed_run_id)
        try:
            asyncio.run(
                _mark_failed(
                    parsed_organization_id,
                    parsed_run_id,
                    "STRIX_INGESTION_FAILED",
                )
            )
        except Exception:
            logger.exception("No se pudo marcar el run %s como FAILED", parsed_run_id)
        raise


@celery_app.task(name="pentests.watchdog_orphaned_runs")
def watchdog_orphaned_runs() -> int:
    """Ejecuta la reconciliación periódica de runs huérfanos."""

    return asyncio.run(_run_watchdog())


@worker_ready.connect
def schedule_startup_watchdog(**_kwargs: object) -> None:
    """Encola la reconciliación al arrancar un worker Celery."""

    try:
        watchdog_orphaned_runs.delay()  # pyright: ignore[reportFunctionMemberAccess]
    except Exception:
        logger.exception("No se pudo encolar el watchdog de arranque")
