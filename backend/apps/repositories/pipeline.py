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
    review: PullRequestReview
    repository: Repository
    credential: GitCredential
    run: PentestRun
    review_id: UUID
    organization_id: UUID
    run_id: UUID
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
        await session.commit()
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
    if review is not None and review.status == PRReviewStatusEnum.SCANNING:
        review.status = PRReviewStatusEnum.ERROR
        review.finished_at = datetime.now(UTC)
    if run is not None and run.status in {ScanStatusEnum.QUEUED, ScanStatusEnum.RUNNING}:
        run.status = ScanStatusEnum.FAILED
        run.finished_at = datetime.now(UTC)
        run.error_message = error_code
    await session.commit()


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
    await session.commit()
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
    await session.commit()
    return claim.review.status.value


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
