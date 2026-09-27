"""Pruebas del tipo de token y de la caducidad opcional.

## Qué se comprueba y por qué

Las dos mitades del cambio, por separado y juntas:

- `token_type` **persiste** lo que se pidió y no se deduce. Un token de servicio con un solo
  scope de lectura sigue siendo de servicio, y esa es exactamente la combinación que tiene una
  integración.
- `expires_at` a `None` es un token **sin caducidad**, y no un token caducado. La diferencia se
  ve en `is_expired`, que es donde se nota si se guardó mal.
- El `0` de un selector HTML se traduce a `None` en vez de dar un `422` que no explica nada.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.api_access.models import (
    ApiToken,
    ApiTokenTypeEnum,
)
from backend.apps.api_access.schemas import ApiTokenCreate
from backend.apps.api_access.scopes import Scope
from backend.apps.api_access.service import create_api_token
from backend.core.database import AsyncSessionLocal

pytestmark = pytest.mark.asyncio


async def _tenant(session: AsyncSession) -> uuid.UUID:
    from backend.apps.organizations.models import (
        Membership,
        Organization,
        RoleEnum,
        User,
    )
    from backend.core.security import hash_password

    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"tokens {suffix}", slug=f"tokens-{suffix}"
    )
    user = User(
        email=f"tokens-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Cliente",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return organization.id


# --------------------------------------------------------------------------- #
# El tipo
# --------------------------------------------------------------------------- #


async def test_por_defecto_el_token_es_personal() -> None:
    """Sin decir nada, el token es personal.

    Es lo que se quiere en la mayoría de los casos: un token ligado a la persona se puede
    revocar con criterio al darle de baja, y el que sobrevive a la baja es la excepción
    deliberada.
    """

    payload = ApiTokenCreate(name="Sin tipo", scopes=[Scope.PENTESTS_READ.value])
    assert payload.token_type is ApiTokenTypeEnum.PERSONAL


async def test_un_token_de_servicio_persiste_el_tipo() -> None:
    """`service_key` llega a la fila, y con un solo scope de lectura.

    El caso de un servicio de integración que solo lee sigue siendo un token de servicio: si el
    tipo se dedujera de los scopes, saldría personal y se revocaría con la persona.
    """

    session = AsyncSessionLocal()
    try:
        organization_id = await _tenant(session)
        token, _secreto = await create_api_token(
            session,
            organization_id,
            ApiTokenCreate(
                name="CI de despliegue",
                scopes=[Scope.PENTESTS_READ.value],
                token_type=ApiTokenTypeEnum.SERVICE_KEY,
            ),
        )
        # Se relee de la base y no del objeto: el valor devuelto puede venir de la sesion sin
        # confirmar, y lo que importa es lo que quedo escrito.
        fila = (
            await session.execute(select(ApiToken).where(ApiToken.id == token.id))
        ).scalar_one()
        assert fila.token_type is ApiTokenTypeEnum.SERVICE_KEY
    finally:
        await session.close()


async def test_el_tipo_se_persiste_con_el_valor_en_minuscula() -> None:
    """En la base está `service_key`, no `SERVICE_KEY`.

    El enum de Python declara los miembros en mayúsculas porque es la convención, pero su
    **valor** es minúsculo porque es lo que viaja por la API. Sin `values_callable` en el modelo
    se guardaria el nombre, y la fila seria correcta a la vista del modelo e ilegible para el
    cliente que la lee.
    """

    session = AsyncSessionLocal()
    try:
        organization_id = await _tenant(session)
        token, _secreto = await create_api_token(
            session,
            organization_id,
            ApiTokenCreate(
                name="Comprobacion de valor",
                scopes=[Scope.PENTESTS_READ.value],
                token_type=ApiTokenTypeEnum.SERVICE_KEY,
            ),
        )
        from sqlalchemy import text

        crudo = (
            await session.execute(
                text("SELECT token_type::text FROM api_tokens WHERE id = :id"),
                {"id": token.id},
            )
        ).scalar_one()
        assert crudo == "service_key"
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# La caducidad
# --------------------------------------------------------------------------- #


async def test_un_token_sin_expiracion_no_caduca() -> None:
    """`expires_in_days=None` deja `expires_at` a `None`, y no caduca nunca.

    El `None` se comprueba contra `is_expired` y no solo contra la columna: un token con
    `expires_at` a `None` que `is_expired` diera por caducado sería un token que funciona y
    que el panel marca como muerto, que es peor que un 401 porque no dice por qué.
    """

    session = AsyncSessionLocal()
    try:
        organization_id = await _tenant(session)
        token, _secreto = await create_api_token(
            session,
            organization_id,
            ApiTokenCreate(
                name="Sin caducidad",
                scopes=[Scope.PENTESTS_READ.value],
                expires_in_days=None,
            ),
        )
        fila = (
            await session.execute(select(ApiToken).where(ApiToken.id == token.id))
        ).scalar_one()
        assert fila.expires_at is None
        assert fila.is_expired(datetime.now(UTC)) is False
        # Tampoco dentro de un año, que es cuando "sin caducidad" empezaria a ser una promesa
        # que hay que matizar.
        assert fila.is_expired(datetime.now(UTC) + timedelta(days=3650)) is False
    finally:
        await session.close()


async def test_un_cero_se_traduce_a_sin_expiracion() -> None:
    """El `0` de un selector HTML vale por "sin caducidad".

    Sin el validador, el `ge=1` rechazaba el `0` con un `422` cuyo texto es "input should be
    greater than or equal to 1": un cliente que ha pedido un token sin caducidad recibe un
    error que no menciona la caducidad, y no hay forma de saber que el valor que queria era
    ese.
    """

    payload = ApiTokenCreate(
        name="Desde el selector", scopes=[Scope.PENTESTS_READ.value], expires_in_days=0
    )
    assert payload.expires_in_days is None
    assert payload.resolved_expiration() is None


async def test_una_caducidad_concreta_sigue_siendo_concreta() -> None:
    """El valor por defecto no se movió: 90 días desde hoy."""

    payload = ApiTokenCreate(name="Normal", scopes=[Scope.PENTESTS_READ.value])
    assert payload.expires_in_days == 90
    caducidad = payload.resolved_expiration()
    assert caducidad is not None
    restantes = caducidad - datetime.now(UTC)
    # Margen amplio a proposito: la asercion no debe fallar por milisegundos, que es la
    # forma de que una prueba de tiempo se vuelva flaky sin avisar.
    assert timedelta(days=89) < restantes <= timedelta(days=90)


async def test_una_caducidad_lejana_da_422() -> None:
    """Cinco años no se admiten: el máximo sigue siendo 365 días.

    El techo no es un capricho. «Máximo» significa que el backend sabe rotar dentro de ese
    margen; admitir mas significaria que existe un camino de expiración que nadie ha
    verificado.
    """

    with pytest.raises(ValueError):
        ApiTokenCreate(
            name="Lejano",
            scopes=[Scope.PENTESTS_READ.value],
            expires_in_days=1825,
        )


async def test_un_tipo_inexistente_da_422() -> None:
    """`robot` no es un tipo de token.

    Sin el enum, un `varchar` libre acabaria con `service`, `service-key` y `SERVICIA`
    conviviendo, y la lectura de la fila fallaria al validar contra un conjunto que no
    incluye lo que se escribio.
    """

    with pytest.raises(ValueError):
        ApiTokenCreate.model_validate(
            {
                "name": "Rareza",
                "scopes": [Scope.PENTESTS_READ.value],
                "token_type": "robot",
            }
        )
