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
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.pricing import credits_per_usd, scan_credit_cost
from backend.apps.billing.service import ZERO, apply_credit_delta
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
from backend.workers.runner.diagnostico import (
    CODIGO_DESCONOCIDO,
    MOTIVOS_DE_DESPLIEGUE,
    diagnosticar_fallo,
    es_fallo_de_despliegue,
)
from backend.workers.runner.exceptions import SandboxCleanupError, SandboxTimeoutError
from backend.workers.runner.host import HostRunResult, StrixHostRunner
from backend.workers.runner.replay import (
    StrixReplayRunner,
    es_replay,
    referencia_de_replay,
)
from backend.workers.runner.sandbox import StrixSandboxManager
from backend.workers.runner.strix_artefactos import EjecucionStrix
from backend.workers.runner.telemetry import (
    LlmUsageTelemetry,
    TokenUsage,
    extract_token_usage,
    select_runtime_models,
)

logger = logging.getLogger(__name__)
KillContainer = Callable[[str], None]

# Fallos que un cambio de modelo puede arreglar. Un contenedor que ni arrancó o
# un error de configuración no cambian por usar otro modelo, así que no se insiste.
# Los motivos de despliegue llevan ahora código propio (`diagnostico.py`), con lo que
# salen de este conjunto por construcción: reintentar un host sin cerco con los cinco
# modelos del catálogo solo gasta la cola y ensucia el registro.
#
# ## Por qué `CODIGO_DESCONOCIDO` está aquí aunque hoy no pueda ganar nada
#
# Porque **no puede ganar nada**, y esa es la razón de que se quede: `_AttemptOutcome.error_code`
# solo toma el valor `STRIX_NONZERO_EXIT` —el único desenlace que devuelve un código— o `None`, y
# el código desconocido se escribe al final del camino, en `_mark_failed`, cuando ya no queda otro
# modelo que probar. La entrada es, por tanto, inalcanzable hoy.
#
# Se conserva aun así por tres razones, y las tres son de coste, no de gusto:
#
# 1. **Es una guarda, y las guardas se pagan por lo que cuesta cuando faltan.** Si mañana
#    `_run_attempt` devuelve un cuarto desenlace —un corte suave, por ejemplo— con su propio código
#    y ese fallo sí mejora cambiando de modelo, la cadena de fallback se quedaría corta **sin
#    ningún aviso**: el bucle `break` en una sola vuelta y nadie ve por qué. Un conjunto que se
#    queda corto no da error, deja de hacer su trabajo.
# 2. **Quitarla no cambiaría nada observable.** Es una entrada de un `frozenset` que se consulta
#    con `in`; mientras el valor no aparezca en `error_code`, borrarla y dejarla son el mismo
#    programa. Y R4 no obliga a reescribir el pasado para que el código muerto lo sea de verdad.
# 3. **La alternativa es peor.** Poner el `else` del bucle a reintentar siempre —o sea, borrar el
#    conjunto— cambia la política de reintentos de todo el módulo cada vez que se clasifique un
#    motivo nuevo, y eso sí es un cambio de comportamiento por una decisión que nadie tomó.
#
# Lo que sí hay que tener presente al tocarla es que `diagnostico.py` documenta esta entrada con
# las palabras «código muerto hoy», y eso es exacto: muerto **hoy**, vivo como protección. Quien
# lea los dos sitios tiene que encontrar la misma frase, y por eso está aquí y no solo allí.
_FALLBACK_ELIGIBLE_ERRORS = frozenset({"STRIX_NONZERO_EXIT", CODIGO_DESCONOCIDO})


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
    usage: TokenUsage | None,
    reserved_credits: Decimal,
) -> None:
    """Tarifica el consumo real del run y ajusta la reserva del tenant.

    La diferencia entre lo reservado y lo realmente consumido se ajusta en el
    ledger: si el motor gastó de menos se devuelve el excedente y si gastó de más
    se cobra. Un feed sin datos de tokens deja la reserva intacta, que es la
    opción conservadora.

    ## Por qué recibe `TokenUsage` y no el JSON del informe

    Porque hay dos ejecuciones que cobran tokens reales y cada una los publica en un sitio
    distinto: el modo contenedor los deja en el reporte (`extract_token_usage`) y el modo host
    en `run.json.llm_usage`. Si esta función recibiera el documento crudo, cada llamada tendría
    que saber de qué modo vino el run, y ese es exactamente el dato que no debe vivir en la
    función de cobrar: quien llama lo lee de donde toca y aquí solo se tarifica.

    `None` significa lo mismo que siempre ha significado: **no lo sabemos**, no cero.

    ## Por qué además escribe `pentest_runs.catalogue_cost_usd`

    Porque el desglose que sale de aquí es **la estimación de la plataforma**, y al lado del importe
    que el proveedor cobró de verdad —`provider_cost_usd`, que escribe la ingesta del modo host—
    es lo único que permite **medir** si el catálogo se está desviando. Guardar solo uno de los dos
    deja al otro como una cifra de la que nadie se puede fiar: si el catálogo dice 6,80 USD y el
    proveedorsays 1,85, sin los dos a la vista cualquiera de los dos parece el bueno.

    Y no ajusta lo que se cobra. El importe está sellado en el ledger (R4) y cambiarlo es una
    decisión comercial: aquí solo se deja la medida.
    """

    model = await _load_model_config(model_id)
    if usage is None or model is None:
        logger.info(
            "Run %s sin telemetria de tokens utilizable; la reserva se mantiene", run_id
        )
        return
    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            desglose = await LlmUsageTelemetry(
                model=model,
                use_case=_use_case_for_scan_mode(scan_mode),
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
            ).charge(
                session=session,
                organization_id=organization_id,
                run_id=run_id,
                credits_per_usd=credits_per_usd(),
                reserved_credits=reserved_credits,
            )
            if desglose is not None:
                await _registrar_coste_de_catalogo(
                    session, organization_id, run_id, desglose.base_cost_usd
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


async def _registrar_coste_de_catalogo(
    session: AsyncSession,
    organization_id: UUID,
    run_id: UUID,
    coste: Decimal,
) -> None:
    """Sella en el run lo que el catálogo estimó que costó su consumo.

    ## Por qué se escribe y no se calcula al leer

    Porque el precio de un modelo cambia y el importe que se cobró en su día no. Si se calculara en
    la consulta, un cambio de `base_cost_input_m` reescribiría la historia: el mismo run mostraría
    hoy un catálogo distinto del que usó cuando se tarificó, y la comparación con
    `provider_cost_usd` —que sí está sellado— no significaría nada.

    R3: el `WHERE` lleva `organization_id`. `run_id` es un UUID adivinable, y sin el filtro
    cualquiera que tenga una sesión podría escribir el coste de catálogo del escaneo de otro
    tenant, que es un número que aparece en su ficha.

    ## Por qué **no** propaga el fallo

    Porque es un dato de diagnóstico, no el cobro. El cobro ya está escrito y confirmado por
    `LlmUsageTelemetry.charge` en la misma transacción; si esta escritura falla, el run está
    correctamente tarificado y lo que se pierde es la medida de la divergencia, que se puede
    recalcular desde `llm_usage_events`. Perder la medida es mejor que tumbar un run que ya
    entregó su resultado.
    """

    try:
        resultado = await session.execute(
            select(PentestRun)
            .where(PentestRun.id == run_id, PentestRun.organization_id == organization_id)
            .with_for_update()
        )
        run = resultado.scalar_one_or_none()
        if run is None:
            logger.warning(
                "El run %s no existe para la organización %s; no se registra su coste de catálogo",
                run_id,
                organization_id,
            )
            return
        run.catalogue_cost_usd = coste
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("No se pudo registrar el coste de catálogo del run %s", run_id)


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
                del incoming_ids
                # La comprobacion de reingesta vive en `_mismos_hallazgos_ingeridos` y la
                # comparten los dos caminos de ingesta, el del modo contenedor y el del modo
                # host. Dos copias de esta comparacion divergirian: una acabaria aceptando un
                # reingesta con evidencia distinta, que es una violacion de R4 en silencio.
                if await _mismos_hallazgos_ingeridos(session, run, findings):
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


async def _persistir_artefactos_del_motor(
    session: AsyncSession,
    run: PentestRun,
    ejecucion: EjecucionStrix,
    *,
    exit_code: str | None,
    referencia: str | None,
) -> int:
    """Escribe el estado real del run y sus hallazgos, y devuelve cuántos escribió.

    ## Por qué es una función aparte y no una bandera dentro de `_ingest_output`

    Porque `_ingest_output` recibe **un documento JSON con el contrato antiguo** —`scan_id`,
    `status`, `findings[]`— que el motor no produce. Meter los artefactos reales en ese camino
    obligaría a fabricar ese documento para que el parser antiguo siguiera funcionando, y un
    documento fabricado es un documento que puede mentir. Este camino lee lo que el motor
    escribió y no inventa la forma intermedia.

    Lo que **sí** es el mismo es la garantía de idempotencia y la de R4, y por eso la lógica de
    reingesta se apoya en `_mismos_hallazgos_ingeridos` y no en una copia.

    R3: el `WHERE` lleva `organization_id` y el `FOR UPDATE` es sobre la fila del tenant. Sin
    el filtro, un identificador de run adivinado bastaría para escribir sobre el escaneo de otro
    cliente.
    """

    if run.source_scan_id is not None and run.source_scan_id != ejecucion.run_id:
        raise StrixIngestionError("El identificador del run no coincide con el ya ingerido")
    run.source_scan_id = ejecucion.run_id[:128]
    if exit_code is not None:
        run.exit_code = exit_code
    if referencia is not None:
        run.container_id = referencia

    # Las horas del motor, no las del worker. `started_at` ya lo puso `_claim_run_for_execution`
    # con el reloj del servidor; el motor sabe cuando empezo el escaneo de verdad, y en un run de
    # veinte minutos la diferencia entre los dos es la que responde a "cuanto tardo en arrancar".
    if ejecucion.start_time is not None:
        run.started_at = ejecucion.start_time
    run.finished_at = ejecucion.end_time or datetime.now(UTC)
    run.coverage = ejecucion.cobertura.a_json() if ejecucion.cobertura is not None else None
    run.provider_cost_usd = ejecucion.coste
    run.provider_tokens = ejecucion.total_tokens

    hallazgos = [
        hallazgo.a_vulnerabilidad(organization_id=run.organization_id, run_id=run.id)
        for hallazgo in ejecucion.hallazgos
    ]
    if await _mismos_hallazgos_ingeridos(session, run, hallazgos):
        await session.commit()
        return len(hallazgos)
    if run.status not in {ScanStatusEnum.QUEUED, ScanStatusEnum.RUNNING}:
        raise StrixIngestionError("El run no está en un estado susceptible de ingesta")

    session.add_all(hallazgos)
    run.status = ScanStatusEnum.COMPLETED
    await session.commit()
    # Los eventos van **después** del commit y en este orden, por el mismo motivo que en
    # `_ingest_output`: un receptor que reaccione a `pentest.completed` buscando las
    # vulnerabilidades de ese run tiene que encontrarlas ya.
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
                for finding in hallazgos
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
            findings_count=len(hallazgos),
        ),
    )
    return len(hallazgos)


async def _mismos_hallazgos_ingeridos(
    session: AsyncSession,
    run: PentestRun,
    entrantes: list[Vulnerability],
) -> bool:
    """¿Este run ya tiene exactamente estos hallazgos, con la misma evidencia?

    ## Por qué esto decide entre idempotencia y error

    ## Porque hay dos razones por las que se vuelve a llamar a la ingesta con el **mismo**
    contenido, y solo una es un error: un reintento del worker y una doble entrega de la tarea de
    Celery. En las dos, reescribir las filas de `vulnerabilities` violaría R4 —la evidencia es
    inmutable— sin aportar nada. Y hay un tercer caso, **un contenido distinto**, que sí es un
    error de verdad: significaría que dos escaneos distintos claimants sobre el mismo run, o que
    el artefacto cambió bajo los pies del worker. Ese caso lanza, y es lo correcto: alguien tiene
    que mirar el registro.

    La comparación es de contenido R4 completo (`_same_immutable_evidence`), no solo de
    identificadores: dos listas con los mismos ids y distinto CVSS son dos escaneos distintos.
    """

    if run.status != ScanStatusEnum.COMPLETED:
        return False
    resultado = await session.execute(
        select(Vulnerability).where(
            Vulnerability.run_id == run.id,
            Vulnerability.organization_id == run.organization_id,
        )
    )
    guardados = list(resultado.scalars().all())
    ids_guardados = {
        finding.source_finding_id for finding in guardados if finding.source_finding_id is not None
    }
    ids_entrantes = {
        finding.source_finding_id
        for finding in entrantes
        if finding.source_finding_id is not None
    }
    if ids_guardados != ids_entrantes:
        raise StrixIngestionError("Los hallazgos del artefacto no coinciden con los ya ingeridos")
    for guardado, entrante in zip(
        sorted(guardados, key=lambda finding: finding.source_finding_id or ""),
        sorted(entrantes, key=lambda finding: finding.source_finding_id or ""),
        strict=False,
    ):
        if not _same_immutable_evidence(guardado, entrante):
            raise StrixIngestionError(
                "Los hallazgos del artefacto no coinciden con los ya ingeridos"
            )
    return True


class _AttemptOutcome(NamedTuple):
    """Desenlace de un intento de ejecución con un modelo concreto.

    Es un `NamedTuple` y no un `dict[str, object]` porque los campos se leen en
    cada iteración de la cadena de fallback: con un diccionario habría queirmar el
    tipo en cada lectura, y un `str` olvidado en un `outcome["output_json"]`
    acabaría en un cargo sin salida de informe.

    ## Por qué hay dos campos de salida y no uno

    Porque hay dos modos de ejecución y cada uno produce un documento distinto, y meterlos en el
    mismo campo obligaría a que cada consumidor comprobara el modo antes de leerlo:

    - `output_json`: el reporte del modo contenedor.
    - `ejecucion`: los artefactos del modo host, ya reducidos a `EjecucionStrix`.

    Mutuamente excluyentes por construcción: el que devuelve `COMPLETED` en modo contenedor
    lleva `output_json` y `ejecucion=None`, y al revés en modo host.

    ## Por qué `sin_tarificar` es un campo y no un caso especial del caller

    Porque el replay produce un `EjecucionStrix` **real** —con tokens y con `llm_usage.cost` del
    run reproducido— y el caller lo lee por `consumo` para ajustar el ledger. Si el replay no dijera
    que no hay que tarificar, su camino sería indistinguible del modo host y cobraría por un
    escaneo que no ocurrió. Es un booleano y no un valor de consumo a cero porque «cobró cero» y
    «no se cobró nada» no son lo mismo: el primero deja asiento y el segundo no lo deja, y el
    ledger es *append-only* (R4), así que un asiento de cero no se puede borrar después.
    """

    result: str  # "COMPLETED" | "FAILED" | "ABORTED"
    output_json: str | None
    error_code: str | None
    ejecucion: EjecucionStrix | None = None
    #: `True` cuando este desenlace **no** debe pasar por `_charge_run_usage`. Hoy solo lo pone el
    #: replay, y el nombre es negativo a propósito: lo que se lee en el caller es «¿cobro?», y la
    #: pregunta tiene que tener una respuesta por defecto que no haga daño.
    sin_tarificar: bool = False

    @property
    def consumo(self) -> TokenUsage | None:
        """Los tokens del intento, sea cual sea el modo.

        La pregunta «cuánto costó esto» la tiene que poder responder el mismo sitio en los dos
        modos, porque quien decide si devuelve dinero no debería importar de dónde vino el run.
        """

        if self.ejecucion is not None:
            if self.ejecucion.total_tokens is None:
                return None
            # El motor solo publica el total en la raiz de `llm_usage`. Repartirlo entre
            # entrada y salida seria inventar el desglose que la tarificacion necesita, y
            # `compute_charge` cobra entrada y salida a precios distintos: un reparto inventado
            # es un cargo inventado. Sin desglose published, no hay cargo.
            desglose = _desglose_de_consumo(self.ejecucion)
            return desglose
        if self.output_json is None:
            return None
        return extract_token_usage(self.output_json)


def _desglose_de_consumo(ejecucion: EjecucionStrix) -> TokenUsage | None:
    """Entrada y salida del run, o `None` si el motor no las publica por separado.

    `run.json` trae `llm_usage.total_tokens` y tambien, en `llm_usage.providers`, el
    `input_tokens` y `output_tokens` por proveedor. Se suman los proveedores, que es donde el
    desglose existe de verdad, y no se reparte el total.
    """

    total = ejecucion.consumo_desglosado
    if total is None:
        return None
    return total


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
            # El replay persiste como un escaneo pero no se cobra como un escaneo: no hubo
            # llamada al proveedor, y mover el saldo por artefactos copiados sería tarificar
            # trabajo que no se ha hecho. La reserva del encolado se queda donde está, que es
            # exactamente lo que hace un escaneo que se completa gastando lo reservado.
            if outcome.sin_tarificar:
                logger.info(
                    "El run %s se completa sin tarificar: es un replay y no hubo consumo real",
                    run_id,
                )
            else:
                await _charge_run_usage(
                    organization_id,
                    run_id,
                    model_id,
                    scan_mode,
                    outcome.consumo,
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

    await _mark_failed(organization_id, run_id, last_error or CODIGO_DESCONOCIDO)
    await _devolver_lo_retenido(
        organization_id,
        run_id,
        referencia=f"{run_id}:refund",
    )
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


async def _saldo_retenido(session: AsyncSession, organization_id: UUID, run_id: UUID) -> Decimal:
    """Cuánto se ha quedado la plataforma de este run, en créditos.

    ## Por qué se lee del ledger y no se vuelve a calcular con `scan_credit_cost`

    Porque lo que hay que devolver es **lo que se cobró**, no lo que la tabla de precios
    dice hoy. `scan_credit_cost` lee `platform_pricing`, que un administrador puede cambiar
    entre que se encola el escaneo y que falla: si el precio subió, devolver el precio nuevo
    haría **regalar** la diferencia, y si bajó, le cobraríamos al tenant la parte que él no
    pagó. El ledger es el único sitio donde el importe cobrado está sellado.

    Y por eso la suma cubre las tres referencias del run, no solo la reserva: el mismo
    identificador puede tener el asiento de consumo (`{id}`), una devolución anterior
    (`{id}:refund`) y el ajuste contra el consumo real (`{id}:usage`). Sumar solo la reserva
    daría un número que deja de ser verdad en cuanto el worker toca cualquiera de las otras
    dos, y devolver sobre esa base es devolver de más.

    ## Por qué el filtro por organización va en el `WHERE` y no después

    Es R3 resuelto tarde: traer el asiento de otro tenant a memoria para descartarlo con un
    `if` es exactamente el patrón que `pentests/service.py` documenta como incorrecto.

    Y sobre el `LIKE`, que merece decirse porque tiene dos comodines: `%` y `_`. Aquí el
    patrón es el `run_id` ya validado, y un UUID es hexadecimal con guiones, así que **no
    puede contener ni `%` ni `_`**. No es una suposición: es la razón por la que se puede
    construir el patrón sin `ESCAPE`, y si algún día esta función recibiera un identificador
    que no fuera un UUID, dejaría de ser cierto.

    ## Por qué devuelve el valor **con signo de devolución**

    Es decir, positivo cuando la plataforma debe algo y cero cuando ya no debe nada. Quien
    llama solo necesita restarlo y compararlo con cero, y no tiene que saber si el signo
    del `SUM` es la dirección de la deuda.
    """

    total = Decimal("0")
    for patron in (str(run_id), f"{run_id}:%"):
        parcial = await session.execute(
            select(func.coalesce(func.sum(CreditLedger.amount_delta), 0)).where(
                CreditLedger.organization_id == organization_id,
                CreditLedger.reason == LedgerReasonEnum.SCAN_CONSUMPTION,
                CreditLedger.reference_id.like(patron),
            )
        )
        total += Decimal(str(parcial.scalar_one()))
    return -total


async def _devolver_lo_retenido(
    organization_id: UUID,
    run_id: UUID,
    *,
    referencia: str,
) -> Decimal:
    """Asienta la devolución de lo retenido por un run, **como mucho una vez**.

    ## Por qué el importe sale del ledger y no de la tabla de precios

    Porque el precio de un escaneo puede cambiar entre que se encola y que falla, y
    `scan_credit_cost` lee el precio **de hoy**. Devolver con el precio de hoy haría que un
    tenant pagase la diferencia si el precio bajó, o que la plataforma regalase la diferencia
    si subió. El importe que hay que devolver es exactamente el que salió de su cartera, y
    ese dato solo está en el asiento que se escribió al cobrar.

    ## Por qué el resultado se descuenta, y por qué eso no reembolsa dos veces

    Porque el propio asiento de devolución entra en la suma: la segunda llamada ve un saldo
    retenido de cero y no escribe nada. La idempotencia sale **de la aritmética del ledger**,
    no de un flag en memoria ni de un `UPDATE` sobre el asiento anterior, que además R4
    prohíbe porque la tabla es *append-only*.

    Y es idempotente también entre **procesos distintos**, que es donde importa: dos workers
    reintentando el mismo run pueden entrar a la vez, pero ambos serializan en el
    `SELECT ... FOR UPDATE` de la fila del run, y el segundo ve el asiento que el primero
    acaba de confirmar. Un `asyncio.Lock` no serviría de nada aquí, igual que en
    `token_refresh.py`: la unidad de ejecución no es el proceso.

    ## Por qué nunca propaga el fallo

    Porque el run ya está en estado terminal y que el ajuste de créditos no se escriba
    dejaría al tenant sin su dinero sin que nadie lo supiera. Se registra la excepción con
    su run y se sigue; el siguiente attempt del watchdog, o el siguiente reintento de Celery,
    vuelven a intentar la misma devolución. Un reembolso perdido es un saldo que no cuadra y
    que alguien tiene que reconciliar a mano; un reembolso duplicado es un saldo inflado que
    además es un regalo. El orden de los riesgos no es simétrico, y por eso el segundo caso
    tiene que ser **imposible por construcción** y el primero solo **probable y registrado**.

    ## Por qué `rollback` va sobre la sesión y no sobre el motor

    Porque `AsyncEngine` no tiene ese método, y llamarlo deja el error real del reembolso
    enterrado bajo un `AttributeError`. Está escrito en `docs/testing/fase5-bloque-5.2.tdd.md`
    como un defecto que ya se cometió una vez aquí.
    """

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            # El bloqueo va **antes** de leer el saldo retenido, no dentro de
            # `apply_credit_delta`. Es lo que hace que dos procesos concurrentes se vean: el
            # segundo espera a que el primero confirme y entonces lee el asiento nuevo. Sin
            # este `FOR UPDATE` los dos leerían el mismo saldo retenido y los dos escribirían
            # una devolución.
            bloqueado = await session.execute(
                select(PentestRun)
                .where(
                    PentestRun.id == run_id,
                    PentestRun.organization_id == organization_id,
                )
                .with_for_update()
            )
            run = bloqueado.scalar_one_or_none()
            if run is None:
                logger.error(
                    "No se devuelve nada del run %s: no existe para la organización %s",
                    run_id,
                    organization_id,
                )
                return ZERO

            # La fila bloqueada se lee también por su **estado**, y no solo para serializar. Dos
            # estados tienen que impedir la devolución, y los dos por un motivo distinto:
            if run.status == ScanStatusEnum.COMPLETED:
                # El escaneo entregó hallazgos y su consumo ya se tarificó. Devolver aquí sería
                # **regalar** el saldo retenido por encima de lo que el cliente ya pagó, y es un
                # camino real: `_ingest_output` confirma el run y es `_charge_run_usage`, que
                # viene **después**, la que puede lanzar. Que el run se completara no borra el
                # fallo de la tarificación.
                logger.info(
                    "El run %s se completó; no se devuelve nada aunque la tarificación haya "
                    "fallado con %s",
                    run_id,
                    run.error_message,
                )
                return ZERO
            if run.status == ScanStatusEnum.ABORTED:
                # Aquí el dinero ya lo decidió otra persona con una regla distinta y mejor
                # informada: `pentests/abort_reason.py` separa «nosotros lo paramos» de «lo
                # paró el cliente», y solo lo primero devuelve. Devolver en el worker sería
                # anular esa decisión: un cliente que cancela y ve como le devuelven los
                # créditos aprende que cancelar es gratis.
                logger.info(
                    "El run %s fue abortado con %s; la devolución la decide el motivo del aborto",
                    run_id,
                    run.error_message,
                )
                return ZERO

            pendiente = await _saldo_retenido(session, organization_id, run_id)
            if pendiente <= ZERO:
                logger.info(
                    "El run %s no tiene nada retenido que devolver (saldo retenido %s)",
                    run_id,
                    pendiente,
                )
                return ZERO

            try:
                await apply_credit_delta(
                    session=session,
                    organization_id=organization_id,
                    amount=pendiente,
                    reason=LedgerReasonEnum.SCAN_CONSUMPTION,
                    reference_id=referencia,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            logger.info(
                "Se devuelven %s créditos del run %s (%s) al tenant %s",
                pendiente,
                run_id,
                run.error_message,
                organization_id,
            )
            return pendiente
    except Exception:
        logger.exception("No se pudo devolver lo retenido por el run %s", run_id)
        return ZERO
    finally:
        await engine.dispose()


async def _devolver_si_el_fallo_fue_de_despliegue(
    organization_id: UUID | None,
    run_id: UUID,
    codigo: str,
) -> Decimal:
    """Devuelve lo retenido **solo** si el motivo clasificado es de despliegue.

    ## Por qué esta política sale del `diagnostico` y no de aquí

    Porque `diagnostico.py` es la autoridad sobre qué motivos dependen de la máquina donde
    corre el worker y cuáles son del análisis o del código, y su `MOTIVOS_DE_DESPLIEGUE` está
    escrito para eso. Reimplementar aquí una lista de códigos sería tener **dos** verdades que
    divergen: alguien añade un motivo al diagnóstico, olvida esta función, y un fallo que
    acaba de clasificarse como «arreglarlo en el host» vuelve a costar créditos al cliente.

    ## Por qué un fallo de código no devuelve

    Porque es un fallo de la plataforma y no del cliente, pero el escaneo **sí llegó a
    ejecutarse**: el contenedor arrancó y el motor trabajó. Lo que no salió es el resultado. Es
    exactamente el caso en que `pentests/abort_reason.py` llama `INFRASTRUCTURE_FAILED` y dice
    que devuelve, y también el caso en que `_execute_pentest_run` ya reembolsa hoy, así que
    **esto no cambia esa política**: solo la hace explícita en el camino que se la saltaba.

    Lo que sí cambia es lo contrario, y es lo que estaba roto: un motivo **de despliegue** —sin
    cerco de salida, sin reconocimiento de exposición, sin imagen del sandbox, sin permisos para
    el workspace, sin demonio de Docker— por el camino de excepción no devolvía nada. El
    cliente pagaba un escaneo que no empezó. Medido en la base de este turno: tres cobros de
    `-10`, `-3` y `-3` créditos sin un solo asiento de devolución.

    ## Por qué la organización va por parámetro y no se resuelve aquí

    Porque el llamador ya lo tiene —lo leyó del propio run antes de empezar— y volver a leerlo
    sería una consulta por el mismo dato. Y si viene `None`, es que ni el run se pudo leer, así
    que no hay cobro del que responder: se dice en el log y no se toca el saldo de nadie.

    ## Por qué la llamada va **después** de `_mark_failed` y no antes

    Porque `_devolver_lo_retenido` bloquea la fila del run con `FOR UPDATE`, igual que
    `_mark_failed`. Marcando primero, el estado que el reembolso lee es el estado **final** del
    run; si el reembolso fuera antes, confirmaría con el run todavía en `RUNNING` y la guarda de
    `COMPLETED` no podría decidir nada.
    """

    if organization_id is None:
        # Sin organización no hay a quién devolverle el dinero, y adivinarlo sería acreditar al
        # tenant equivocado. Es R3 aplicado al dinero: la duda se registra, no se resuelve
        # suponiendo.
        logger.error(
            "No se pudo resolver la organización del run %s; no se devuelve nada porque no hay a "
            "quién devolvérselo, y el fallo es anterior a que se cobrara nada",
            run_id,
        )
        return ZERO
    if codigo not in MOTIVOS_DE_DESPLIEGUE:
        logger.info(
            "El run %s no se reembolsa: %s no es un motivo de despliegue, así que el escaneo se "
            "ejecutó y lo que falló fue el análisis o el código",
            run_id,
            codigo,
        )
        return ZERO
    return await _devolver_lo_retenido(
        organization_id,
        run_id,
        referencia=f"{run_id}:refund",
    )


async def _run_attempt(
    run_id: UUID,
    organization_id: UUID,
    target: str,
    scan_mode: str,
    target_type: str,
    model_id: str,
) -> _AttemptOutcome:
    """Ejecuta el motor con un modelo concreto y devuelve el desenlace.

    ## Por qué el modo se elige aquí y no dentro del runner

    Porque los dos modos no solo se ejecutan distinto: **persisten distinto**. El modo contenedor
    produce un reporte con el contrato antiguo y lo ingiere `_ingest_output`; el modo host produce
    cuatro artefactos y lo ingiere `_persistir_artefactos_del_motor`. Ocultar esa diferencia
    detrás de una única interfaz obligaría a fabricar un documento intermedio para que los dos
    caminos convergieran en el mismo punto, y un documento fabricado puede mentir. La
    bifurcación se ve, y es de dos líneas.

    ## Por qué el replay se comprueba **antes** que el modo de ejecución

    Porque el replay no es un tercer modo de ejecutar el motor: es un modo de **no** ejecutarlo.
    Si se eligiera por `strix_execution_mode`, un despliegue en modo `container` con el replay
    puesto intentaría levantar un contenedor, y un despliegue en modo `host` intentaría lanzar el
    ejecutable. Los dos son justo lo que el replay existe para no hacer. Va primero, y solo se
    mira `strix_execution_mode` cuando no hay replay.
    """

    if settings.strix_replay_source.strip():
        return await _run_attempt_en_replay(run_id, organization_id, target)

    if settings.strix_execution_mode == "host":
        return await _run_attempt_en_host(
            run_id,
            organization_id,
            target,
            scan_mode,
            target_type,
            model_id,
        )
    return await _run_attempt_en_contenedor(
        run_id,
        organization_id,
        target,
        scan_mode,
        target_type,
        model_id,
    )


async def _run_attempt_en_replay(
    run_id: UUID,
    organization_id: UUID,
    target: str,
) -> _AttemptOutcome:
    """Reproduce los artefactos de un run real y los persiste por el camino del modo host.

    ## Por qué **no** cobra ni reembolsa

    Porque un replay no es trabajo: no llama al proveedor, no ejecuta el motor y no gasta nada. Y
    porque la reserva que el panel hizo al encolar **sigue en pie**: el replay termina en
    `COMPLETED`, que es el estado donde `_charge_run_usage` cobra el ajuste y donde
    `_devolver_lo_retenido` se niega a devolver. Un replay se declara como escaneo pero no se cobra
    como escaneo, y esa asimetría es el precio de que el pipeline se pueda probar sin gastar.

    ## Por qué vuelve por `_persistir_artefactos_del_motor` y no por un atajo

    Porque es **la misma función** que usa el modo host, con las mismas reglas de idempotencia, de
    R3 y de R4. Si el replay tuviera su propia escritura, estaríamos probando el atajo, que es
    justo el camino que no existe en producción.

    ## Por qué se marca la referencia antes de leer

    Porque el run tiene que ser localizable mientras dura el replay, igual que en cualquier otro
    modo. Y `replay:` no es un PID: no hay árbol de procesos que matar, y el camino de aborto lo
    reconoce para no inventarse una ejecución que no existe.
    """

    runner = StrixReplayRunner(str(run_id), target=target)
    referencia = referencia_de_replay(runner.source.name if runner.source else "sin-origen")
    if not await _set_container_reference(organization_id, run_id, referencia):
        return _AttemptOutcome(result="ABORTED", output_json=None, error_code=None)

    try:
        ejecucion, referencia = runner.run()
    except BaseException:
        if runner.cleanup_pending:
            await _set_cleanup_pending(organization_id, run_id, True)
        raise

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            try:
                escrito = await session.execute(
                    select(PentestRun)
                    .where(
                        PentestRun.id == run_id,
                        PentestRun.organization_id == organization_id,
                    )
                    .with_for_update()
                )
                run = escrito.scalar_one_or_none()
                if run is None:
                    raise StrixIngestionError("El run no existe para la organización indicada")
                await _persistir_artefactos_del_motor(
                    session,
                    run,
                    ejecucion,
                    exit_code="0",
                    referencia=referencia,
                )
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()
    logger.info(
        "Replay del run %s completado con %d hallazgo(s) y %d registro(s) de cobertura; "
        "no se ha movido el saldo del tenant",
        run_id,
        len(ejecucion.hallazgos),
        ejecucion.registros_cobertura,
    )
    return _AttemptOutcome(
        result="COMPLETED",
        output_json=None,
        error_code=None,
        ejecucion=ejecucion,
        # El consumo se devuelve a propósito: el caller lo lee para tarificar, y un replay no
        # tarifica. Es el único punto donde esta función se separa del modo host.
        sin_tarificar=True,
    )


async def _run_attempt_en_host(
    run_id: UUID,
    organization_id: UUID,
    target: str,
    scan_mode: str,
    target_type: str,
    model_id: str,
) -> _AttemptOutcome:
    """Ejecuta el motor como proceso del host y persiste sus artefactos.

    ## Por qué el subproceso va en `asyncio.to_thread`

    Porque `subprocess.communicate` **bloquea**, y esta corrutina vive en el bucle de eventos del
    worker de Celery. Bloquearlo aquí congela el worker entero: no responde al latido, no saca
    otras tareas de la cola y el watchdog deja de vigilar. En modo contenedor el bloqueo lo
    absorbía la llamada al demonio; en modo host lo absorbe el propio proceso que se lanza, y
    por eso necesita el `to_thread` explícito.

    ## Por qué el PID se guarda en `container_id`

    Porque es la columna por la que el camino de aborto —que vive en el proceso de la API, en
    otro distinto— encuentra la ejecución en curso. La referencia va con prefijo
    `host-pid:` para que quien la lea sepa con qué hay que matarlo; ver `host.py`.
    """

    runner = StrixHostRunner(
        str(run_id),
        target,
        scan_mode,
        target_type=target_type,
        llm_model=model_id,
    )
    started_event = threading.Event()
    started_references: list[str] = []

    def on_started(process_id: str) -> None:
        started_references.append(f"host-pid:{process_id}")
        started_event.set()

    runner_task = asyncio.create_task(
        asyncio.to_thread(
            runner.run,
            timeout_seconds=settings.strix_hard_timeout_seconds,
            soft_timeout_seconds=settings.strix_soft_timeout_seconds,
            on_started=on_started,
        )
    )
    while not started_event.is_set() and not runner_task.done():
        await asyncio.sleep(0.05)

    if started_event.is_set():
        referencia = started_references[0]
        referencia_puesta = await _set_container_reference(
            organization_id,
            run_id,
            referencia,
        )
        if not referencia_puesta:
            process_id = HostRunResult.parsear_referencia(referencia)
            if process_id is not None:
                StrixHostRunner.matar_por_pid(process_id)
            try:
                await runner_task
            except Exception:
                logger.exception("El escaneo abortado %s terminó con error", run_id)
            if runner.cleanup_pending:
                await _set_cleanup_pending(organization_id, run_id, True)
            return _AttemptOutcome(result="ABORTED", output_json=None, error_code=None)

    try:
        result = await runner_task
    except BaseException:
        if runner.cleanup_pending:
            await _set_cleanup_pending(organization_id, run_id, True)
        raise

    engine, session_factory = _session_factory()
    try:
        async with session_factory() as session:
            try:
                escrito = await session.execute(
                    select(PentestRun)
                    .where(
                        PentestRun.id == run_id,
                        PentestRun.organization_id == organization_id,
                    )
                    .with_for_update()
                )
                run = escrito.scalar_one_or_none()
                if run is None:
                    raise StrixIngestionError("El run no existe para la organización indicada")
                count = await _persistir_artefactos_del_motor(
                    session,
                    run,
                    result.ejecucion,
                    exit_code=str(result.exit_code),
                    referencia=result.referencia_de_proceso(),
                )
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()
    logger.info(
        "El run %s terminó en modo host con %d hallazgo(s), %d registro(s) de cobertura y "
        "%d aviso(s) del motor",
        run_id,
        count,
        result.ejecucion.registros_cobertura,
        len(result.ejecucion.advertencias),
    )
    return _AttemptOutcome(
        result="COMPLETED",
        output_json=None,
        error_code=None,
        ejecucion=result.ejecucion,
    )


async def _run_attempt_en_contenedor(
    run_id: UUID,
    organization_id: UUID,
    target: str,
    scan_mode: str,
    target_type: str,
    model_id: str,
) -> _AttemptOutcome:
    """Ejecuta el motor en su contenedor efímero y persiste su reporte."""

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
                # La tercera rama, y la que cierra el hueco más grave que ha tenido este
                # watchdog: **cualquier run con un contenedor registrado entra**, sin importar
                # su estado.
                #
                # Las dos primeras ramas eran «RUNNING obsoleto» y «limpieza pendiente». El camino
                # del timeout —`mark_run_timed_out`, `tasks.py:146`— marca `TIMED_OUT` y
                # `finished_at`, y **no toca `cleanup_pending`**. Así que un run que expiró
                # quedó fuera de las dos: el watchdog no lo veía nunca.
                #
                # ## Por qué el resultado no era un proceso colgado y nada más
                #
                # ## Por qué era peor que un proceso colgado
                #
                # Porque el `finally: self.cleanup()` del sandbox es la **única** red que mata
                # el contenedor, borra su red y purga `/tmp/fenix_workspaces/<run_id>`. Y hay un
                # camino en el que ese `finally` no se ejecuta: el límite de Celery lanza
                # `TimeLimitExceeded` en el hilo principal, no en el hilo del sandbox, así que el
                # `finally` no se dispara. El contenedor de Strix se queda vivo con el código
                # fuente del cliente montado en solo lectura, la red bridge sin recoger y el
                # workspace sin purgar — el código fuente de un cliente, retenido de forma
                # indefinida, que es exactamente lo que R5 prohíbe.
                #
                # ## Por qué esta rama lo cierra sin depender de la memoria
                #
                # ## Por qué no depende de que cada camino de error se acuerde
                #
                # Porque `container_id IS NOT NULL` es un hecho sobre el **estado del mundo**,
                # no sobre cómo acabó el escaneo. El contenedor existe o no existe, y mientras
                # exista hay que_matarlo. Hacerlo depender de que cada rama de error recuerde
                # escribir una bandera es confiar la seguridad del sistema a la memoria de quien
                # escriba el siguiente `except`.
                #
                # Y no puede tocar el contenedor de otro: `kill_container` exige que la etiqueta
                # `fenix.run_id` coincida con el run esperado (`sandbox.py:430-435`), y el
                # nombre aquí se deriva del UUID del propio run.
                and_(
                    PentestRun.container_id.is_not(None),
                    or_(
                        PentestRun.status == ScanStatusEnum.RUNNING,
                        PentestRun.cleanup_pending.is_(True),
                    ),
                ),
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
        # Un replay no tiene contenedor, no tiene red y no tiene proceso: su referencia lo dice, y
        # preguntarle al demonio de Docker por él sería hablar con un servicio que en un despliegue
        # de desarrollo puede no existir. El fallo se convertiría en `cleanup_pending` permanente
        # sobre un run que ya no tiene nada que limpiar, que es el peor sitio donde puede aparecer
        # una marca de limpieza pendiente.
        es_reproduccion = es_replay(container_reference)
        try:
            if es_reproduccion:
                logger.info("El run %s es una reproducción; no hay contenedor que detener", run.id)
            elif kill_container is None:
                StrixSandboxManager.kill_container(
                    container_reference,
                    expected_run_id=str(run.id),
                )
            else:
                kill_container(container_reference)
        except Exception:
            cleanup_succeeded = False
            logger.exception("No se pudo limpiar el contenedor huérfano %s", run.id)
        if not es_reproduccion:
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
        # **No** se devuelve nada aquí, y es deliberado. Un timeout no es un fallo de
        # despliegue: es el motor que estuvo trabajando hasta que se le acabó el tiempo, y el
        # trabajo ese le costó tokens a la plataforma. `MOTIVOS_DE_DESPLIEGUE` no incluye
        # `STRIX_TIMEOUT` por eso. Lo que sí hace este camino es dejar el run en `TIMED_OUT`
        # para que el watchdog lo limpie.
        raise
    except Exception as error:
        # El código que se persiste **no** es `STRIX_EXECUTION_FAILED`: es el que dice qué
        # falló, y hay una diferencia operativa enorme entre los dos. Un despliegue sin el
        # cerco de salida, sin el reconocimiento de exposición o sin la imagen del sandbox
        # produce el mismo fallo visible que un bug, y con el código genérico el operador
        # acaba en el repositorio en vez de en el host. El panel sabe explicar el código
        # concreto, así que la clasificación es lo que hace posible avisar sin abrir el
        # registro del worker.
        diagnostico = diagnosticar_fallo(error)
        if es_fallo_de_despliegue(error):
            logger.error(
                "El run %s falló por configuración del despliegue, no del código: %s (%s). "
                "Arreglarlo es en el host donde corre el worker.",
                parsed_run_id,
                diagnostico.codigo,
                diagnostico.comprobacion,
            )
        else:
            logger.exception("Falló la ejecución del run %s", parsed_run_id)
        try:
            asyncio.run(_mark_failed(organization_id, parsed_run_id, diagnostico.codigo))
        except Exception:
            logger.exception("No se pudo marcar el run %s como FAILED", parsed_run_id)
        asyncio.run(
            _devolver_si_el_fallo_fue_de_despliegue(
                organization_id, parsed_run_id, diagnostico.codigo
            )
        )
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
