"""Orquestación de una revisión de Pull Request aislada y efímera."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.repositories.clients.base import (
    BaseGitClient,
    GitClientError,
    GitRateLimitError,
    GitServerError,
)
from backend.apps.repositories.feedback import build_pr_comment_markdown
from backend.apps.repositories.models import (
    GitCredential,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.services import build_client_for_repository
from backend.apps.repositories.workspace import materialize_pr_workspace
from backend.apps.vulnerabilities.models import SeverityEnum, Vulnerability
from backend.apps.webhooks.emission import (
    EventType,
    pentest_payload,
    pr_review_payload,
    publish_event,
    vulnerability_created_payload,
)
from backend.core.config import settings
from backend.core.database import create_database_engine
from backend.workers.parser.strix_parser import extract_strix_scan_id, parse_strix_output
from backend.workers.runner.sandbox import SandboxRunResult, StrixSandboxManager

logger = logging.getLogger(__name__)


class PRPipelineError(RuntimeError):
    """Error de dominio durante el pipeline de revisión de PR."""


class SessionProvider(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]: ...


ClientBuilder = Callable[[AsyncSession, Repository], Awaitable[BaseGitClient]]
ManagerFactory = Callable[..., StrixSandboxManager]
Materializer = Callable[..., Awaitable[list[str]]]


@dataclass(frozen=True, slots=True)
class PipelineClaim:
    """Lo que el pipeline necesita para trabajar, y lo que necesita para **anunciar**.

    ## Por qué los identificadores están duplicados

    Los objetos de ORM (`review`, `repository`, `run`) soneya convenientes mientras la
    sesión está viva, pero **no sobreviven a un `commit` o un `rollback` con
    `expire_on_commit=True`**: sus atributos pasan a pedir una recarga, y en SQLAlchemy
    asíncrono esa recarga necesita un contexto verde. Leerlos fuera de una operación de base
    de datos —que es exactamente lo que se hace al construir un payload de evento— falla con
    `MissingGreenlet`.

    Los UUID van aparte por eso. Son valores planos, ya resueltos, y funcionan en cualquier
    punto del flujo: después de un commit, después de un rollback, dentro de un `except`.

    La alternativa —leer `claim.repository.id` y confiar en que nadie expire la sesión— es
    lo que produjo el fallo: la sesión de los tests usa el valor por defecto de SQLAlchemy y
    el pipeline de producción usa `expire_on_commit=False`, así que el mismo código pasaba
    en un sitio y reventaba en otro.
    """

    review: PullRequestReview
    repository: Repository
    credential: GitCredential
    run: PentestRun
    review_id: UUID
    organization_id: UUID
    run_id: UUID
    #: Plano, a propósito. Ver la nota de la clase.
    repository_id: UUID
    repository_full_name: str
    commit_sha: str
    pr_number: int


@asynccontextmanager
async def _default_session_provider() -> AsyncIterator[AsyncSession]:
    engine = create_database_engine(settings)
    session_factory = async_sessionmaker[AsyncSession](
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def _parse_review_id(review_id: str) -> UUID:
    try:
        return UUID(review_id)
    except (TypeError, ValueError) as error:
        raise PRPipelineError("review_id no contiene un UUID válido") from error


def _target_identifier(repository: Repository, pr_number: int) -> str:
    return f"{repository.full_name}#PR-{pr_number}"[:512]


async def _claim_review(
    session: AsyncSession,
    review_id: UUID,
    *,
    retry_failed: bool = False,
    celery_task_id: str | None = None,
) -> PipelineClaim | None:
    review_result = await session.execute(
        select(PullRequestReview)
        .where(PullRequestReview.id == review_id)
        .with_for_update()
    )
    review = review_result.scalar_one_or_none()
    if review is None:
        raise PRPipelineError("La revisión de PR no existe")
    if review.status == PRReviewStatusEnum.ERROR and retry_failed:
        review.status = PRReviewStatusEnum.QUEUED
        review.run_id = None
        review.finished_at = None
    if review.status != PRReviewStatusEnum.QUEUED:
        return None
    repository_result = await session.execute(
        select(Repository)
        .where(
            Repository.id == review.repository_id,
            Repository.organization_id == review.organization_id,
        )
        .with_for_update()
    )
    repository = repository_result.scalar_one_or_none()
    if repository is None or not repository.is_active:
        raise PRPipelineError("El repositorio de la revisión no está activo")
    if not repository.pr_reviews_enabled:
        review.status = PRReviewStatusEnum.ERROR
        review.finished_at = datetime.now(UTC)
        # El payload se construye **antes** del commit. Con una sesión que expira al
        # confirmar, leer `review.organization_id` después es una recarga que necesita
        # contexto verde: `MissingGreenlet` sobre una revisión que ya está guardada como
        # fallida, que es una operación correcta reportada como error.
        payload = pr_review_payload(
            review_id=review.id,
            repository_id=repository.id,
            pr_number=review.pr_number,
            status=PRReviewStatusEnum.ERROR.value,
            findings_count=0,
            blocking=True,
            error_code="PR_REVIEWS_DISABLED",
        )
        tenant_id = review.organization_id
        await session.commit()
        # Aquí todavía no existe `claim`: esta función lo construye. Se usan las filas que
        # acaba de leer, que son la misma revisión y el mismo repositorio.
        await publish_event(session, EventType.PR_REVIEW_FAILED, tenant_id, payload)
        raise PRPipelineError("Las revisiones automáticas están deshabilitadas")

    run: PentestRun | None = None
    if review.run_id is not None:
        existing_run = await session.get(PentestRun, review.run_id)
        if existing_run is not None and existing_run.status in {
            ScanStatusEnum.QUEUED,
            ScanStatusEnum.RUNNING,
        }:
            run = existing_run
    if run is None:
        run = PentestRun(
            organization_id=review.organization_id,
            target_type=TargetTypeEnum.REPOSITORY,
            target_identifier=_target_identifier(repository, review.pr_number),
            scan_mode=ScanModeEnum.QUICK,
            status=ScanStatusEnum.RUNNING,
            started_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
    run.status = ScanStatusEnum.RUNNING
    run.started_at = run.started_at or datetime.now(UTC)
    if celery_task_id is not None:
        run.celery_task_id = celery_task_id[:64]
    review.run_id = run.id
    review.status = PRReviewStatusEnum.SCANNING
    review.issues_caught_critical = 0
    review.issues_caught_high = 0
    review.merge_blocked = False
    review.finished_at = None
    await session.commit()

    credential_result = await session.execute(
        select(GitCredential).where(
            GitCredential.organization_id == repository.organization_id,
            GitCredential.provider == repository.provider,
        )
    )
    credential = credential_result.scalar_one_or_none()
    if credential is None:
        raise PRPipelineError("No existe una credencial Git para la organización")
    return PipelineClaim(
        review,
        repository,
        credential,
        run,
        review.id,
        review.organization_id,
        run.id,
        repository.id,
        repository.full_name,
        review.commit_sha,
        review.pr_number,
    )


async def _mark_pipeline_error(
    session: AsyncSession,
    claim: PipelineClaim,
    error_code: str,
) -> None:
    await session.rollback()
    review_result = await session.execute(
        select(PullRequestReview)
        .where(PullRequestReview.id == claim.review_id)
        .with_for_update()
    )
    review = review_result.scalar_one_or_none()
    run_result = await session.execute(
        select(PentestRun)
        .where(
            PentestRun.id == claim.run_id,
            PentestRun.organization_id == claim.organization_id,
        )
        .with_for_update()
    )
    run = run_result.scalar_one_or_none()
    # Las banderas dicen si la transición **ocurrió ahora**, no si el objeto tiene el
    # campo puesto. La función se llama también con un run que ya estaba en estado
    # terminal, y preguntar por `finished_at is not None` daría un falso positivo en
    # cuanto una segunda ejecución pasara por aquí.
    review_transiciono = review is not None and review.status == PRReviewStatusEnum.SCANNING
    run_transiciono = run is not None and run.status in {
        ScanStatusEnum.QUEUED,
        ScanStatusEnum.RUNNING,
    }
    if review is not None and review_transiciono:
        review.status = PRReviewStatusEnum.ERROR
        review.finished_at = datetime.now(UTC)
    if run is not None and run_transiciono:
        run.status = ScanStatusEnum.FAILED
        run.finished_at = datetime.now(UTC)
        run.error_message = error_code
    # Los payloads se construyen **antes** del commit. Después, con una sesión que expire
    # al confirmar, leer `review.id` o `run.target_identifier` dispara una recarga que en
    # SQLAlchemy asíncrono necesita un contexto verde y revienta con `MissingGreenlet` —
    # sobre un pipeline que ya está guardado como fallido. Es el mismo modo de fallo que
    # `_persist_findings`: una operación de dominio correcta reportada como error.
    #
    # Solo se construyen los payloads de las transiciones que **ocurrieron**: repetir un
    # fallo que el receptor ya recibió con su código original lo haría parecer que hay dos
    # incidentes.
    review_payload: dict[str, object] | None = None
    if review is not None and review_transiciono:
        review_payload = pr_review_payload(
            review_id=review.id,
            repository_id=claim.repository_id,
            pr_number=claim.pr_number,
            status=PRReviewStatusEnum.ERROR.value,
            findings_count=0,
            blocking=True,
            error_code=error_code,
        )
    run_payload: dict[str, object] | None = None
    if run is not None and run_transiciono:
        run_payload = pentest_payload(
            run_id=run.id,
            status=ScanStatusEnum.FAILED.value,
            target_type=run.target_type,
            target_value=run.target_identifier,
            scan_mode=run.scan_mode,
            started_at=run.started_at,
            finished_at=run.finished_at,
            error_code=error_code,
        )

    await session.commit()
    # El tenant se toma de `claim`, que es un `dataclass` de valores planos leídos antes
    # del commit. Leerlo de `run` o de `review` después de confirmar pediría sus atributos
    # a la sesión, y con una que expira eso es una recarga que necesita contexto verde.
    if review_payload is not None:
        await publish_event(
            session, EventType.PR_REVIEW_FAILED, claim.organization_id, review_payload
        )
    if run_payload is not None:
        await publish_event(
            session, EventType.PENTEST_FAILED, claim.organization_id, run_payload
        )


async def _set_cleanup_pending(session: AsyncSession, claim: PipelineClaim) -> None:
    await session.rollback()
    result = await session.execute(
        select(PentestRun)
        .where(
            PentestRun.id == claim.run_id,
            PentestRun.organization_id == claim.organization_id,
        )
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is not None:
        run.cleanup_pending = True
        await session.commit()


def _review_url(review_id: UUID) -> str:
    return f"{settings.frontend_base_url.rstrip('/')}/pr-reviews/{review_id}"


async def _publish_status(
    client: BaseGitClient,
    claim: PipelineClaim,
    state: str,
    description: str,
) -> None:
    try:
        await asyncio.to_thread(
            client.set_commit_status,
            claim.repository_full_name,
            claim.commit_sha,
            state,
            description[:130],
            _review_url(claim.review_id),
        )
    except GitClientError:
        raise
    except Exception as error:
        raise PRPipelineError("No se pudo publicar el estado del commit") from error


async def _persist_findings(
    session: AsyncSession,
    claim: PipelineClaim,
    result: SandboxRunResult,
) -> list[Vulnerability]:
    if result.exit_code != 0:
        await _mark_pipeline_error(session, claim, "STRIX_NONZERO_EXIT")
        raise PRPipelineError("Strix terminó con un código de salida no cero")
    expected_scan_id = extract_strix_scan_id(result.output_json)
    run_result = await session.execute(
        select(PentestRun)
        .where(
            PentestRun.id == claim.run_id,
            PentestRun.organization_id == claim.organization_id,
        )
        .with_for_update()
    )
    run = run_result.scalar_one_or_none()
    if run is None:
        raise PRPipelineError("El run de la revisión no existe")
    if run.status == ScanStatusEnum.COMPLETED:
        existing_result = await session.execute(
            select(Vulnerability).where(
                Vulnerability.run_id == claim.run_id,
                Vulnerability.organization_id == claim.organization_id,
            )
        )
        return list(existing_result.scalars().all())
    if run.status not in {ScanStatusEnum.QUEUED, ScanStatusEnum.RUNNING}:
        raise PRPipelineError("El run ya no admite ingesta de resultados")
    if run.source_scan_id is not None and run.source_scan_id != expected_scan_id:
        raise PRPipelineError("El scan_id del reporte no coincide con el run")
    findings = parse_strix_output(
        result.output_json,
        organization_id=claim.organization_id,
        run_id=claim.run_id,
        db=None,
        expected_scan_id=expected_scan_id,
    )
    session.add_all(findings)
    await session.flush()
    run.source_scan_id = expected_scan_id
    run.status = ScanStatusEnum.COMPLETED
    run.finished_at = datetime.now(UTC)
    run.exit_code = str(result.exit_code)
    # El payload se construye **antes** del commit. Después, los objetos de ORM quedan
    # expirados y leer `finding.id` dispara una recarga que en SQLAlchemy asíncrono necesita
    # un contexto verde: sin él, la lectura del payload revienta con `MissingGreenlet` y el
    # escaneo —que ya está confirmado— se reporta como fallido.
    #
    # Además es lo correcto por otro motivo: el cuerpo del evento describe lo que se
    # acaba de guardar, y leerlo antes de confirmar lo lee de los objetos que se van a
    # persistir, no de una recarga que podría devolver otra cosa.
    payload = vulnerability_created_payload(
        [
            {
                "id": str(finding.id),
                "severity": finding.severity,
                "title": finding.title,
                "run_id": str(claim.run_id),
            }
            for finding in findings
        ]
    )
    await session.commit()
    await publish_event(session, EventType.VULNERABILITY_CREATED, claim.organization_id, payload)
    return findings


async def _finalize_review(
    session: AsyncSession,
    claim: PipelineClaim,
    findings: list[Vulnerability],
    client: BaseGitClient,
) -> str:
    critical_count = sum(
        finding.severity == SeverityEnum.CRITICAL for finding in findings
    )
    high_count = sum(finding.severity == SeverityEnum.HIGH for finding in findings)
    blocked = critical_count > 0 or high_count > 0
    claim.review.issues_caught_critical = critical_count
    claim.review.issues_caught_high = high_count
    claim.review.merge_blocked = blocked
    claim.review.status = PRReviewStatusEnum.FAILED if blocked else PRReviewStatusEnum.PASSED
    claim.review.finished_at = datetime.now(UTC)
    await session.flush()
    state = "failure" if blocked else "success"
    description = (
        f"Fenix review: {critical_count} critical, {high_count} high findings."
        if blocked
        else "Fenix review: no critical or high findings."
    )
    comment = build_pr_comment_markdown(
        findings,
        f"{settings.frontend_base_url.rstrip('/')}/issues",
    )
    if claim.review.comment_id:
        await asyncio.to_thread(
            client.update_pr_comment,
            claim.repository_full_name,
            claim.review.comment_id,
            comment,
        )
    elif blocked:
        comment_id = await asyncio.to_thread(
            client.post_pr_comment,
            claim.repository_full_name,
            claim.pr_number,
            comment,
        )
        claim.review.comment_id = comment_id
    await _publish_status(client, claim, state, description)
    # El payload y el valor de retorno se resuelven **antes** del commit. Después, con una
    # sesión que expire, `claim.review.id` o `claim.review.status` piden una recarga que
    # necesita contexto verde: la revisión quedaría guardada como completada y el pipeline
    # se reportaría como fallido al leer su propio resultado.
    payload = pr_review_payload(
        review_id=claim.review_id,
        repository_id=claim.repository_id,
        pr_number=claim.pr_number,
        status=claim.review.status.value,
        findings_count=len(findings),
        blocking=blocked,
    )
    status_final = claim.review.status.value

    await session.commit()
    await publish_event(
        session, EventType.PR_REVIEW_COMPLETED, claim.organization_id, payload
    )
    return status_final


async def _run_pr_security_pipeline(
    review_id: str,
    *,
    session_provider: SessionProvider = _default_session_provider,
    client_builder: ClientBuilder = build_client_for_repository,
    manager_factory: ManagerFactory = StrixSandboxManager,
    materializer: Materializer = materialize_pr_workspace,
    workspace_root: Path | None = None,
    retry_failed: bool = False,
    celery_task_id: str | None = None,
) -> str:
    """Ejecuta el pipeline completo y siempre purga el workspace en ``finally``."""

    parsed_review_id = _parse_review_id(review_id)
    claim: PipelineClaim | None = None
    manager: StrixSandboxManager | None = None
    client: BaseGitClient | None = None
    async with session_provider() as session:
        try:
            claim = await _claim_review(
                session,
                parsed_review_id,
                retry_failed=retry_failed,
                celery_task_id=celery_task_id,
            )
            if claim is None:
                return "SKIPPED"
            client = await client_builder(session, claim.repository)
            await _publish_status(
                client,
                claim,
                "pending",
                "Fenix security review in progress",
            )
            manager = manager_factory(
                run_id=str(claim.run.id),
                target=claim.run.target_identifier,
                scan_mode="quick",
                target_type="REPOSITORY",
                workspace_root=workspace_root or settings.strix_workspace_root,
            )
            workspace_dir = manager.setup_workspace()
            # El directorio `workspace` del host se monta como `/workspace/target`
            # dentro del sandbox; por eso el repositorio se materializa aquí.
            target_dir = workspace_dir / "workspace"
            modified_files = await materializer(
                claim.repository,
                claim.review,
                target_dir,
                credential=claim.credential,
            )
            manager.included_files = modified_files
            result = await asyncio.to_thread(
                manager.run,
                timeout_seconds=settings.pr_scan_hard_timeout_seconds,
                soft_timeout_seconds=settings.pr_scan_soft_timeout_seconds,
                workspace_prepared=True,
            )
            findings = await _persist_findings(session, claim, result)
            return await _finalize_review(session, claim, findings, client)
        except (GitRateLimitError, GitServerError):
            if claim is not None:
                await _mark_pipeline_error(session, claim, "GIT_TRANSIENT_ERROR")
            logger.exception("Fallo Git transitorio en el pipeline PR %s", parsed_review_id)
            raise
        except Exception as error:
            if claim is not None:
                await _mark_pipeline_error(session, claim, "PR_PIPELINE_FAILED")
                if client is not None:
                    try:
                        await _publish_status(
                            client,
                            claim,
                            "error",
                            "Fenix security review could not be completed",
                        )
                    except Exception:
                        logger.exception("No se pudo publicar el estado de error del PR")
            logger.exception("Falló el pipeline de revisión PR %s", parsed_review_id)
            raise PRPipelineError("Falló el pipeline de revisión PR") from error
        finally:
            if manager is not None:
                if manager.temp_dir is not None:
                    try:
                        await asyncio.to_thread(manager.cleanup)
                    except Exception:
                        manager.cleanup_pending = True
                        logger.exception("No se pudo purgar el workspace del PR")
                if manager.cleanup_pending and claim is not None:
                    await _set_cleanup_pending(session, claim)
            if client is not None:
                try:
                    close = getattr(client, "close", None)
                    if callable(close):
                        await asyncio.to_thread(close)
                except Exception:
                    logger.exception("No se pudo cerrar el cliente Git del PR")
