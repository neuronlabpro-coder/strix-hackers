"""Tareas Celery de ejecución e ingesta de Strix."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple
from uuid import UUID

from billiard.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from celery.signals import worker_ready
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.apps.billing.models import LedgerReasonEnum
from backend.apps.billing.pricing import scan_credit_cost
from backend.apps.billing.service import apply_credit_delta
from backend.apps.llm_router.models import LLMModelConfig, LLMUseCaseEnum
from backend.apps.llm_router.routing import (
    LLMAllModelsInactiveError,
    resolve_model_chain,
)
from backend.apps.pentests.models import PentestRun, ScanModeEnum, ScanStatusEnum
from backend.apps.repositories.models import (
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.vulnerabilities.models import Vulnerability
from backend.apps.webhooks.emission import (
    EventType,
    pentest_payload,
    pr_review_payload,
    publish_event,
    vulnerability_created_payload,
)
from backend.core.config import settings
from backend.core.database import create_database_engine
from backend.workers.celery_app import celery_app
from backend.workers.parser.strix_parser import extract_strix_scan_id, parse_strix_output
from backend.workers.runner.exceptions import SandboxCleanupError, SandboxTimeoutError
from backend.workers.runner.sandbox import StrixSandboxManager
from backend.workers.runner.telemetry import (
    LlmUsageTelemetry,
    extract_token_usage,
    select_runtime_models,
)

logger = logging.getLogger(__name__)
KillContainer = Callable[[str], None]

# Fallos que un cambio de modelo puede arreglar. Un contenedor que no arrancó o
# un error de configuración no cambian por usar otro modelo, así que no se insiste.
_FALLBACK_ELIGIBLE_ERRORS = frozenset({"STRIX_NONZERO_EXIT", "STRIX_EXECUTION_FAILED"})


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
            await publish_event(
                session,
                EventType.PENTEST_FAILED,
                run.organization_id,
                pentest_payload(
                    run_id=run.id,
                    status=ScanStatusEnum.FAILED.value,
                    target_type=run.target_type,
                    target_value=run.target_identifier,
                    scan_mode=run.scan_mode,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    error_code=error_code,
                ),
            )
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
    await publish_event(
        session,
        EventType.PENTEST_TIMED_OUT,
        run.organization_id,
        pentest_payload(
            run_id=run.id,
            status=ScanStatusEnum.TIMED_OUT.value,
            target_type=run.target_type,
            target_value=run.target_identifier,
            scan_mode=run.scan_mode,
            started_at=run.started_at,
            finished_at=run.finished_at,
            error_code=run.error_message,
        ),
    )
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


def _use_case_for_scan_mode(scan_mode: str) -> LLMUseCaseEnum:
    """Un escaneo `QUICK` enruta a los modelos rápidos; el resto, a los potentes."""

    return (
        LLMUseCaseEnum.QUICK_SCAN
        if scan_mode.upper() == ScanModeEnum.QUICK.name
        else LLMUseCaseEnum.DEEP_PENTEST
    )


async def _resolve_model_chain(scan_mode: str) -> list[LLMModelConfig]:
    """Cadena de modelos para un run. Vacía si el catálogo no tiene ninguno activo."""

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            try:
                chain = await resolve_model_chain(
                    session, _use_case_for_scan_mode(scan_mode)
                )
            except LLMAllModelsInactiveError:
                logger.warning(
                    "No hay modelos de LLM activos; el run usará DEFAULT_STRIX_LLM"
                )
                return []
            return list(chain)
    finally:
        await engine.dispose()


async def _load_model_config(model_id: str) -> LLMModelConfig | None:
    """Carga la configuración de un modelo para poder tarificar su consumo."""

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(LLMModelConfig).where(LLMModelConfig.model_id == model_id)
            )
            return result.scalar_one_or_none()
    finally:
        await engine.dispose()


async def _charge_run_usage(
    organization_id: UUID,
    run_id: UUID,
    model_id: str,
    scan_mode: str,
    output_json: str,
    reserved_credits: Decimal,
) -> None:
    """Tarifica el consumo real del run y ajusta la reserva del tenant.

    La diferencia entre lo reservado y lo realmente consumido se ajusta en el
    ledger: si el motor gastó de menos se devuelve el excedente y si gastó de más
    se cobra. Un feed sin datos de tokens deja la reserva intacta, que es la
    opción conservadora.
    """

    usage = extract_token_usage(output_json)
    model = await _load_model_config(model_id)
    if usage is None or model is None:
        logger.info(
            "Run %s sin telemetria de tokens utilizable; la reserva se mantiene", run_id
        )
        return
    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            await LlmUsageTelemetry(
                model=model,
                use_case=_use_case_for_scan_mode(scan_mode),
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
            ).charge(
                session=session,
                organization_id=organization_id,
                run_id=run_id,
                credits_per_usd=settings.credits_per_usd,
                reserved_credits=reserved_credits,
            )
    except Exception:
        # La tarificación es posterior al escaneo: un fallo aquí no debe
        # invalidar un run que ya terminó correctamente. Queda registrado para
        # que el operador pueda reconciliarlo.
        await engine.dispose()
        logger.exception("No se pudo ajustar el consumo del run %s", run_id)
        raise
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
                # Los dos eventos van **después** del commit y en este orden: primero los
                # hallazgos y después la finalización del escaneo. Al revés, un receptor
                # que reaccione a `pentest.completed` buscando las vulnerabilidades de ese
                # run se las encontraría ya, porque se emiten después; en el orden inverso
                # las recibiría antes de existir.
                await publish_event(
                    session,
                    EventType.VULNERABILITY_CREATED,
                    run.organization_id,
                    vulnerability_created_payload(
                        [
                            {
                                "id": str(finding.id),
                                "severity": finding.severity,
                                "title": finding.title,
                                "run_id": str(run.id),
                            }
                            for finding in findings
                        ]
                    ),
                )
                await publish_event(
                    session,
                    EventType.PENTEST_COMPLETED,
                    run.organization_id,
                    pentest_payload(
                        run_id=run.id,
                        status=ScanStatusEnum.COMPLETED.value,
                        target_type=run.target_type,
                        target_value=run.target_identifier,
                        scan_mode=run.scan_mode,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                        findings_count=len(findings),
                    ),
                )
                return len(findings)
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


class _AttemptOutcome(NamedTuple):
    """Desenlace de un intento de ejecución con un modelo concreto.

    Es un `NamedTuple` y no un `dict[str, object]` porque los tres campos se leen en
    cada iteración de la cadena de fallback: con un diccionario habría que asserting
    el tipo en cada lectura, y un `str` olvidado en un `outcome["output_json"]`
    acabaría en un cargo sin salida de informe.
    """

    result: str  # "COMPLETED" | "FAILED" | "ABORTED"
    output_json: str | None
    error_code: str | None


async def _execute_pentest_run(run_id: UUID) -> str:
    claimed = await _claim_run_for_execution(run_id)
    if claimed is None:
        return "SKIPPED"
    organization_id, target, scan_mode, target_type = claimed
    chain = select_runtime_models(await _resolve_model_chain(scan_mode))
    reserved = scan_credit_cost(ScanModeEnum(scan_mode))
    last_error: str | None = None

    # Con el catálogo vacío se recurre al modelo por defecto de configuración: es
    # preferible un modelo de tarificación desconocida a no ejecutar el escaneo.
    for attempt, model_id in enumerate(chain or [settings.default_strix_llm]):
        outcome = await _run_attempt(
            run_id,
            organization_id,
            target,
            scan_mode,
            target_type,
            model_id,
        )
        if outcome.result == "ABORTED":
            return "ABORTED"
        if outcome.result == "COMPLETED":
            if outcome.output_json is not None:
                await _charge_run_usage(
                    organization_id,
                    run_id,
                    model_id,
                    scan_mode,
                    outcome.output_json,
                    reserved,
                )
            return "COMPLETED"
        last_error = outcome.error_code
        has_next_model = attempt + 1 < len(chain)
        if not has_next_model:
            break
        # Fallback: el contenedor habla con OpenRouter por su cuenta, así que el
        # worker no ve el 429 ni el 5xx. Lo que sí puede observar es que la
        # ejecución falló, y reencolar con el siguiente modelo es la respuesta
        # honesta. Un fallo no transitorio (contenedor que ni arrancó) no cambia con
        # otro modelo, así que no se insiste.
        if last_error not in _FALLBACK_ELIGIBLE_ERRORS:
            logger.info(
                "El run %s falló con %s, que no mejora cambiando de modelo",
                run_id,
                last_error,
            )
            break
        logger.warning(
            "El run %s falló con %s en %s; reintentando con %s",
            run_id,
            last_error,
            model_id,
            chain[attempt + 1],
        )
        await _reopen_run_for_retry(run_id)

    await _mark_failed(organization_id, run_id, last_error or "STRIX_EXECUTION_FAILED")
    await _refund_reserved_credits(organization_id, run_id, reserved)
    return "FAILED"


async def _reopen_run_for_retry(run_id: UUID) -> None:
    """Devuelve el run a QUEUED para que el siguiente modelo pueda reintentarlo."""

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(PentestRun).where(PentestRun.id == run_id).with_for_update()
            )
            run = result.scalar_one_or_none()
            if run is None:
                return
            run.status = ScanStatusEnum.QUEUED
            run.started_at = None
            run.error_message = None
            run.exit_code = None
            await session.commit()
    finally:
        await engine.dispose()


async def _refund_reserved_credits(
    organization_id: UUID, run_id: UUID, reserved: Decimal
) -> None:
    """Devuelve la reserva cuando el escaneo no llegó a producir consumo.

    El reembolso no puede propagar el fallo: el run ya está marcado como fallido y
    que el ajuste de créditos no se escriba dejaría al tenant sin sus créditos, así que
    se registra la excepción y el proceso sigue. El `rollback` va sobre la sesión, no
    sobre el motor: `AsyncEngine` no tiene ese método, y llamarlo dejaría el error
    real del reembolso enterrado bajo un `AttributeError`.
    """

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            try:
                await apply_credit_delta(
                    session=session,
                    organization_id=organization_id,
                    amount=reserved,
                    reason=LedgerReasonEnum.SCAN_CONSUMPTION,
                    reference_id=f"{run_id}:refund",
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    except Exception:
        logger.exception("No se pudo reembolsar la reserva del run %s", run_id)
    finally:
        await engine.dispose()


async def _run_attempt(
    run_id: UUID,
    organization_id: UUID,
    target: str,
    scan_mode: str,
    target_type: str,
    model_id: str,
) -> _AttemptOutcome:
    """Ejecuta el sandbox con un modelo concreto y devuelve el desenlace."""

    manager = StrixSandboxManager(
        str(run_id),
        target,
        scan_mode,
        target_type=target_type,
        llm_model=model_id,
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
            return _AttemptOutcome(result="ABORTED", output_json=None, error_code=None)

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
        return _AttemptOutcome(
            result="FAILED", output_json=None, error_code="STRIX_NONZERO_EXIT"
        )
    expected_scan_id = extract_strix_scan_id(result.output_json)
    await _ingest_output(
        organization_id,
        run_id,
        result.output_json,
        expected_scan_id,
        exit_code=str(result.exit_code),
        container_id=result.container_id,
    )
    return _AttemptOutcome(
        result="COMPLETED", output_json=result.output_json, error_code=None
    )


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
    # Las listas se declaran **antes** del bucle porque se leen después del commit, y una
    # variable declarada dentro del bucle no existe si el bucle no llegó a ejecutarse.
    # Adentro se bajarían a ámbito del último run, que es justo el caso en el que el
    # watchdog no encuentra nada huérfano y no debe emitir nada.
    reviews_por_avisar: list[PullRequestReview] = []
    runs_para_avisar: list[PentestRun] = []
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
        # Las revisiones que el watchdog marca se guardan para emitirlas **después** del
        # commit de más abajo. Emitir dentro del bucle sería emitir antes de confirmar, y
        # un receptor que consultara la API en ese instante no vería el estado que el
        # evento anuncia.
        if run.status == ScanStatusEnum.RUNNING:
            run.status = ScanStatusEnum.FAILED
            run.finished_at = current_time
            run.error_message = "WORKER_WATCHDOG_ORPHANED"
            runs_para_avisar.append(run)
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
                    reviews_por_avisar.append(review)
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
        reviews_por_avisar.append(terminal_review)
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
    # Todo el aviso del watchdog va después del commit, y **solo** por lo que esta pasada
    # transitó de verdad. La lista `reviews_por_avisar` se llenó únicamente en las
    # asignaciones de arriba, así que un run que ya estaba terminal no produce ni un
    # evento: repetiría un fallo que el receptor ya tiene.
    for run in runs_para_avisar:
        await publish_event(
            session,
            EventType.PENTEST_FAILED,
            run.organization_id,
            pentest_payload(
                run_id=run.id,
                status=ScanStatusEnum.FAILED.value,
                target_type=run.target_type,
                target_value=run.target_identifier,
                scan_mode=run.scan_mode,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error_code=run.error_message,
            ),
        )
    for review in reviews_por_avisar:
        await publish_event(
            session,
            EventType.PR_REVIEW_FAILED,
            review.organization_id,
            pr_review_payload(
                review_id=review.id,
                repository_id=review.repository_id,
                pr_number=review.pr_number,
                status=PRReviewStatusEnum.ERROR.value,
                findings_count=0,
                blocking=True,
                error_code="WORKER_WATCHDOG_STALE",
            ),
        )
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
