"""Cálculo del progreso de onboarding a partir del estado persistido."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.onboarding.schemas import (
    OnboardingResponse,
    OnboardingStep,
    OnboardingStepKeyEnum,
)
from backend.apps.pentests.models import PentestRun, ScanModeEnum
from backend.apps.repositories.models import GitCredential, Repository


async def _has_credential(session: AsyncSession, organization_id: UUID) -> bool:
    """Si hay una credencial Git **utilizable**, no solo una fila.

    ## Por qué se comprueba la caducidad y no basta con contar filas

    Porque contar filas responde a «¿hay algo en `git_credentials`?» y la pregunta del checklist
    es «¿esta organización tiene su cuenta de Git conectada?». Una credencial OAuth de GitHub
    dura ocho horas y el panel la guarda para siempre; al día siguiente la fila sigue ahí, el
    proveedor contesta `401` a todo, y el checklist se lo contaba al usuario como paso hecho.

    El síntoma era una pantalla que se contradecía a sí misma en la misma sesión: el modal de
    «Conectar repositorio» decía que no había credencial utilizable y el Dashboard, a la vez,
    decía «3 de 3 completados». Los dos leían la misma fila y cada uno contestó una pregunta
    distinta sin que se supiera cuál era la buena. Esta función responde a la buena.

    ## Por qué `token_expires_at IS NULL` cuenta como conectada

    Porque es lo que deja un token personal de acceso, que no tiene caducidad conocida y se
    guarda con la fecha a `None`. Contarlo como caducado sería inventar un dato, y el panel
    pediría una reconexión que no arregla nada.

    ## Por qué se filtra por `organization_id` primero

    Por R3. Una credencial viva de otra organización no dice nada de esta, y contarla haría que
    el checklist de un tenant saliera completo con el Git de otro. El filtro es además el que
    aprovecha `ix_git_credentials_org_provider`, único sobre esas dos columnas.

    ## Por qué `now()` se calcula en Python y no con `func.now()`

    Porque las dos tienen que decir la misma hora. El reloj de PostgreSQL y el de la aplicación
    pueden diferir en unos segundos —y el de la base es el del contenedor—, así que un
    `token_expires_at` que escribimos hace un segundo puede caer en un lado o en el otro de la
    frontera. Con una sola fuente, el paso del checklist y el `410` que contesta el inventario
    coinciden en el mismo instante.
    """

    ahora = datetime.now(UTC)
    result = await session.execute(
        select(func.count(GitCredential.id)).where(
            GitCredential.organization_id == organization_id,
            or_(
                GitCredential.token_expires_at.is_(None),
                GitCredential.token_expires_at > ahora,
            ),
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

    ## Por qué «estado real» y no «filas que existen»

    Porque un paso se marca por lo que se puede **usar**, no por lo que hay escrito. Hoy solo lo
    importa `connect_git`: la fila de credencial que caducó sigue en la base y sigue contando
    como una cuenta conectada si no se mira la fecha. Las tres funciones de esta comprobación
    usan el mismo criterio que el endpoint que usa esa credencial, para que el checklist y el
    modal de conexión no puedan discrepar.
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
