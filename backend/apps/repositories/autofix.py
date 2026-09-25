"""Servicio de autofix programático a partir de evidencia R4."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.apps.repositories.clients.base import BaseGitClient
from backend.apps.repositories.models import PullRequestReview, Repository
from backend.apps.repositories.patches import AutofixPatchError, parse_patch
from backend.apps.repositories.services import build_client_for_repository
from backend.apps.vulnerabilities.models import Vulnerability
from backend.core.config import settings
from backend.core.database import create_database_engine


class AutofixError(RuntimeError):
    """Error controlado al preparar o publicar un autofix."""


class SessionProvider(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]: ...


ClientBuilder = Callable[[AsyncSession, Repository], Awaitable[BaseGitClient]]


@asynccontextmanager
async def _default_session_provider() -> AsyncIterator[AsyncSession]:
    engine = create_database_engine(settings)
    factory = async_sessionmaker[AsyncSession](
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _parse_id(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError) as error:
        raise AutofixError(f"{field_name} no contiene un UUID válido") from error


def validate_autofix_patch(patch: str) -> str:
    """Valida el diff inmutable antes de cualquier llamada externa."""

    if len(patch.encode("utf-8")) > settings.strix_max_autofix_chars:
        raise AutofixError("El diff de autofix excede el tamaño permitido")
    try:
        parsed_files = parse_patch(patch)
    except AutofixPatchError as error:
        raise AutofixError("El diff de autofix no es válido") from error
    if len(parsed_files) > settings.autofix_max_files:
        raise AutofixError("El diff de autofix supera el límite de archivos")
    return patch


def _branch_for_vulnerability(vulnerability_id: UUID) -> str:
    branch = f"{settings.autofix_branch_prefix}{vulnerability_id.hex[:8]}"
    if (
        len(branch) > 255
        or branch.startswith("-")
        or ".." in branch
        or any(char.isspace() or ord(char) < 32 for char in branch)
        or any(char in branch for char in "~^:?*[\\")
    ):
        raise AutofixError("El nombre de rama de autofix no es válido")
    return branch


async def _create_autofix_branch_and_pr(
    review_id: str,
    vulnerability_id: str,
    *,
    session_provider: SessionProvider = _default_session_provider,
    client_builder: ClientBuilder = build_client_for_repository,
) -> str:
    parsed_review_id = _parse_id(review_id, "review_id")
    parsed_vulnerability_id = _parse_id(vulnerability_id, "vulnerability_id")
    async with session_provider() as session:
        vulnerability_result = await session.execute(
            select(Vulnerability).where(Vulnerability.id == parsed_vulnerability_id)
        )
        vulnerability = vulnerability_result.scalar_one_or_none()
        if vulnerability is None:
            raise AutofixError("La vulnerabilidad no existe")
        review_result = await session.execute(
            select(PullRequestReview).where(
                PullRequestReview.id == parsed_review_id,
                PullRequestReview.organization_id == vulnerability.organization_id,
                PullRequestReview.run_id == vulnerability.run_id,
            )
        )
        review = review_result.scalar_one_or_none()
        if review is None:
            raise AutofixError("La vulnerabilidad no pertenece a la revisión")
        repository_result = await session.execute(
            select(Repository).where(
                Repository.id == review.repository_id,
                Repository.organization_id == review.organization_id,
            )
        )
        repository = repository_result.scalar_one_or_none()
        if repository is None or not repository.is_active:
            raise AutofixError("El repositorio no está activo")
        patch = vulnerability.autofix_patch_diff
        if not patch:
            raise AutofixError("La vulnerabilidad no contiene un autofix")
        validated_patch = validate_autofix_patch(patch)
        branch_name = _branch_for_vulnerability(vulnerability.id)
        title = f"[Fenix Security Fix] {vulnerability.title}"[:255]
        client = await client_builder(session, repository)
        try:
            result = await asyncio.to_thread(
                client.create_autofix_branch_and_pr,
                repository.full_name,
                review.target_branch,
                branch_name,
                validated_patch,
                title,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                await asyncio.to_thread(close)
        if not isinstance(result, str) or not result.strip():
            raise AutofixError("El proveedor Git no devolvió una URL de autofix")
        return result


def create_autofix_branch_and_pr(review_id: str, vulnerability_id: str) -> str:
    """Abre una rama y un PR/MR de corrección desde la evidencia validada."""

    return asyncio.run(
        _create_autofix_branch_and_pr(
            review_id,
            vulnerability_id,
        )
    )
