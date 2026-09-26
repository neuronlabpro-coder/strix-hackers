"""Servicio de ciclo de vida de los tokens de API.

Todo lo que crea, lista o revoca un token pasa por aquí, y la razón es que el secreto
no puede procesarse dos veces. Si el alta estuviera en el router, habría que devolverle
el secreto a la capa de arriba para que la respuesta lo includiera, y un `return`
intermedio es exactamente donde un secreto se acaba registrando por accidente.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.api_access.models import (
    API_TOKEN_PREFIX,
    TOKEN_SECRET_BYTES,
    ApiToken,
    token_prefix_for,
)
from backend.apps.api_access.schemas import ApiTokenCreate
from backend.apps.api_access.scopes import (
    Scope,
    normalize_scopes,
    scope_is_known,
)
from backend.core.security import generate_api_token, hash_api_token

logger = logging.getLogger(__name__)


class ApiTokenNotFoundError(LookupError):
    """El token no existe dentro de la organización solicitada.

    Es un `LookupError` y no un `HTTPException` porque el servicio no sabe nada de HTTP.
    El router lo traduce a `404`, y ese `404` es deliberado: un token de otro tenant
    tiene que ser indistinguible de uno que no existe, o el `404` se convierte en un
    oráculo que permite enumerar identificadores ajenos.
    """


async def create_api_token(
    session: AsyncSession,
    organization_id: uuid.UUID,
    payload: ApiTokenCreate,
) -> tuple[ApiToken, str]:
    """Crea un token y devuelve la fila junto al secreto en claro.

    El secreto se devuelve **una vez** y no se vuelve a guardar. Quien llama tiene que
    ponerlo en la respuesta HTTP; si se pierde ahí, no hay recuperación y la única salida
    es revocar el token, que es precisamente lo que hace `--solo-mostrar-una-vez` de
    otras herramientas pero aquí es la única opción.
    """

    raw_token = generate_api_token(API_TOKEN_PREFIX, TOKEN_SECRET_BYTES)
    now = datetime.now(UTC)
    token = ApiToken(
        organization_id=organization_id,
        name=payload.name,
        token_prefix=token_prefix_for(raw_token),
        # Nunca el token. Esta es la línea de la que depende todo el resto del módulo.
        token_hash=hash_api_token(raw_token),
        scopes=[scope.value for scope in payload.resolved_scopes()],
        expires_at=now + timedelta(days=payload.expires_in_days),
    )
    session.add(token)
    await session.commit()
    await session.refresh(token)
    logger.info(
        "Token de API creado: organization_id=%s id=%s scopes=%d",
        organization_id,
        token.id,
        len(token.scopes),
    )
    return token, raw_token


async def list_api_tokens(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    include_revoked: bool = False,
) -> list[ApiToken]:
    """Lista los tokens del tenant, revocados fuera salvo que se pidan.

    El filtro por `organization_id` va en el `WHERE` y no se aplica después en Python:
    R3 exige que la restricción esté en la consulta, y filtrar en memoria dejaría la
    puerta abierta a que un cambio futuro de la función la olvide.
    """

    consulta = select(ApiToken).where(ApiToken.organization_id == organization_id)
    if not include_revoked:
        consulta = consulta.where(ApiToken.revoked_at.is_(None))
    consulta = consulta.order_by(ApiToken.created_at.desc())
    return list((await session.execute(consulta)).scalars().all())


async def revoke_api_token(
    session: AsyncSession,
    organization_id: uuid.UUID,
    token_id: uuid.UUID,
) -> ApiToken:
    """Marca un token como revocado y lo devuelve.

    ## Por qué la fila se conserva

    Revocar tiene que poder demostrarse. Si la fila desapareciera, no habría forma de
    responder meses después a "¿este token estuvo activo el día del incidente?". Es el
    mismo motivo por el que `credit_ledger` y `audit_log` son append-only en R4.

    ## Por qué no se lanza error al revocar dos veces

    Revocar un token ya revocado deja el estado en el que el cliente pidió y no tiene
    efecto colateral. Lanzar un `409` obligaría a cada cliente a distinguir entre "ya
    estaba revocado" y "no existe" — y esa distinción filtra información de otros
    tenants. Idempotente es más honesto aquí.

    El `last_used_at` se conserva, no se limpia: es la evidencia de hasta cuándo sirvió.
    """

    resultado = await session.execute(
        select(ApiToken).where(
            ApiToken.id == token_id,
            # R3: el identificador de la fila y el del tenant van juntos. Buscar solo por
            # `id` y comprobar el tenant después devolvería 404 igualmente, pero dejaría la
            # comprobación en un sitio donde se puede olvidar.
            ApiToken.organization_id == organization_id,
        )
    )
    token = resultado.scalar_one_or_none()
    if token is None:
        raise ApiTokenNotFoundError(f"Token {token_id} no encontrado")

    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(token)
        logger.info(
            "Token de API revocado: organization_id=%s id=%s",
            organization_id,
            token.id,
        )
    return token


async def touch_api_token(session: AsyncSession, token: ApiToken) -> None:
    """Registra que el token se ha usado, sin tocar nada más.

    Se hace en su propia sentencia y sin actualizar `updated_at` a propósito: `ApiToken`
    no tiene `updated_at`, y la única marca que refleja "esto acaba de pasar" es
    `last_used_at`. Mezclarla con `revoked_at` haría imposible responder "¿cuándo se
    dejó de usar?" con una sola lectura.
    """

    token.last_used_at = datetime.now(UTC)
    await session.commit()


def grants(token: ApiToken, required: Scope) -> bool:
    """Indica si el token concede el scope pedido.

    La comparación es de cadena exacta contra la lista persistida. Convertir a `Scope`
    y comparar objetos daría el mismo resultado hoy, pero dejaría abierta la puerta a
    que un valor corrupto en la base se tome como permiso válido; comparar texto exacto
    hace que un valor inesperado no conceda nada.
    """

    return required.value in (token.scopes or [])


def scopes_of(token: ApiToken) -> tuple[Scope, ...]:
    """Los scopes del token que siguen en el catálogo, en orden.

    Filtra antes de convertir en vez de lanzar. Una fila con un scope retirado del
    catálogo —porque se retiró en una versión posterior— no debe romper la
    autenticación de todo el token, solo dejar de conceder ese permiso concreto.
    Romper la autenticación entera convertiría una retirada inofensiva en una caída de
    todos los tokens que la tengan, y esa es una forma cara de equivocarse.
    """

    conocidos = [item for item in (token.scopes or []) if scope_is_known(item)]
    return normalize_scopes(conocidos)
