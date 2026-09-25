"""Cálculo del progreso de onboarding a partir del estado persistido."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.onboarding.schemas import (
    OnboardingResponse,
    OnboardingStep,
    OnboardingStepKeyEnum,
)
from backend.apps.pentests.models import PentestRun, ScanModeEnum
from backend.apps.repositories.models import GitCredential, Repository


async def _has_credential(session: AsyncSession, organization_id: UUID) -> bool:
    result = await session.execute(
        select(func.count(GitCredential.id)).where(
            GitCredential.organization_id == organization_id
        )
    )
    return int(result.scalar_one()) > 0


async def _has_repository(session: AsyncSession, organization_id: UUID) -> bool:
    result = await session.execute(
        select(func.count(Repository.id)).where(
            Repository.organization_id == organization_id
        )
    )
    return int(result.scalar_one()) > 0


async def _has_full_scan(session: AsyncSession, organization_id: UUID) -> bool:
    """Un escaneo `QUICK` lo dispara el webhook de un PR, no el usuario.

    Contarlo como "primer escaneo" completaría el checklist sin que nadie haya
    lanzado nada desde el panel, que es justo lo que este widget promete.
    """

    result = await session.execute(
        select(func.count(PentestRun.id)).where(
            PentestRun.organization_id == organization_id,
            PentestRun.scan_mode != ScanModeEnum.QUICK,
        )
    )
    return int(result.scalar_one()) > 0


async def build_onboarding_status(
    session: AsyncSession,
    organization_id: UUID,
) -> OnboardingResponse:
    """Devuelve los tres pasos del checklist con su estado real.

    MENU-MAP §1.1 describe seis pasos. Este bloque implementa los tres primeros
    porque son los únicos verificables contra datos existentes: los pasos 4 a 6
    dependen de pantallas que siguen siendo placeholder (`/pr-reviews`,
    integraciones y miembros) y marcarlos como completados sería mentir.
    """

    completion = {
        OnboardingStepKeyEnum.CONNECT_GIT: await _has_credential(session, organization_id),
        OnboardingStepKeyEnum.IMPORT_REPOSITORY: await _has_repository(session, organization_id),
        OnboardingStepKeyEnum.RUN_FIRST_SCAN: await _has_full_scan(session, organization_id),
    }
    steps = [
        OnboardingStep(key=key, completed=completed) for key, completed in completion.items()
    ]
    completed_steps = sum(1 for step in steps if step.completed)
    return OnboardingResponse(
        steps=steps,
        completed_steps=completed_steps,
        total_steps=len(steps),
        is_complete=completed_steps == len(steps),
    )
