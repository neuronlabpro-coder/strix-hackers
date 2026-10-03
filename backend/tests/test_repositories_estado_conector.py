"""Qué contesta el inventario remoto cuando la credencial no sirve.

## El defecto que este fichero existe para no dejar volver

En la cuenta de SuperAdmin, el modal de «Conectar repositorio» decía «Este proveedor todavía no
tiene conector» mientras la lista de repositorios de la misma pantalla tenía tres entradas de
GitHub. El Dashboard, a la vez, decía «Configuración inicial · 3 de 3 completados».

Las tres afirmaciones no pueden ser ciertas a la vez, y el motivo estaba en la base de datos: la
credencial OAuth de GitHub de esa organización tenía `token_expires_at` = 2026-10-01, es decir,
había caducado. GitHub contestaba `401 Bad credentials`, la ruta lo traducía a `502`, y el
frontend trataba todo lo que no fuese `409` como «no hay conector».

Había tres cosas detrás y aquí se cubren las tres:

1. **`token_expires_at` se guardaba y no se leía en ningún sitio.** Una credencial OAuth de
   GitHub dura ocho horas; el panel la guardaba para siempre y la contaba como conectada.
2. **El `401` del proveedor se traducía a `502`.** Con un solo código, «tu credencial caducó» y
   «GitHub se ha caído» eran la misma respuesta.
3. **`CryptoError` se traducía a «no hay credencial».** La fila existe; lo que falla es la clave
   con la que se cifró. Decir «conecta una credencial» ahí es un bucle sin resultado.

## Por qué estas pruebas no se apoyan en los datos de demostración

Porque la base compartida tiene la credencial caducada y los repositorios sembrados, y una prueba
que pasa con ellos no está probando la lógica: está probando el estado de la base. Cada prueba
crea su organización, su credencial y sus fechas, y comprueba el código de respuesta.

## Por qué se comprueba que el proveedor **no** se llama

Porque «dar el motivo sin gastar la llamada» es media de la corrección: el `410` solo es posible
si la caducidad la dice la base de datos, antes de preguntar al proveedor. Si el test solo
mirara el código de respuesta, pasaría igual con la comprobación hecha después.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.repositories.clients.base import GitClientError
from backend.apps.repositories.models import GitCredential, GitProviderEnum
from backend.core.crypto import CryptoError, encrypt_secret
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _workspace(
    session: AsyncSession,
    *,
    provider: GitProviderEnum = GitProviderEnum.GITHUB,
    caduca_en: datetime | None = None,
    token_valido: bool = True,
) -> tuple[Organization, dict[str, str]]:
    """Una organización con su credencial de `provider` y las cabeceras de su sesión.

    `caduca_en` es la fecha que se guarda en `token_expires_at`. `None` es lo que deja un token
    personal de acceso, que no tiene caducidad conocida.
    """

    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Conector {suffix}", slug=f"conector-{suffix}")
    user = User(
        email=f"conector-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Conector Admin",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(
            organization_id=organization.id,
            user_id=user.id,
            role=RoleEnum.ADMIN,
        )
    )
    session.add(
        GitCredential(
            organization_id=organization.id,
            provider=provider,
            encrypted_access_token=encrypt_secret(
                "github-token" if token_valido else "",
                organization_id=str(organization.id),
                provider=provider.value,
            ),
            token_expires_at=caduca_en,
        )
    )
    await session.commit()
    return organization, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


async def _pedir_inventario(
    client: AsyncClient,
    cabeceras: dict[str, str],
    *,
    provider: str = "GITHUB",
) -> Response:
    """La petición de inventario, sin `params` duplicados en cada prueba."""

    return await client.get(
        "/api/v1/repositories/remote",
        params={"provider": provider},
        headers=cabeceras,
    )


@pytest.mark.asyncio
async def test_una_credencial_caducada_no_se_pregunta_al_proveedor(
    integration_session: AsyncSession,
) -> None:
    """`410`, sin gastar la llamada, porque la fecha ya está en la base.

    Este es el test del defecto. Antes esta credencial llegaba al proveedor, GitHub contestaba
    `401`, la ruta lo traducía a `502` y el modal lo leía como «no hay conector».
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(hours=2),
    )
    constructor = AsyncMock(side_effect=AssertionError("no se debe abrir el cliente"))

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=constructor,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            respuesta = await _pedir_inventario(client, cabeceras)

    assert respuesta.status_code == 410
    assert "caduc" in respuesta.json()["detail"].lower()
    constructor.assert_not_awaited()


@pytest.mark.asyncio
async def test_una_credencial_vigente_se_pregunta_al_proveedor(
    integration_session: AsyncSession,
) -> None:
    """La caducidad no corta lo que todavía está dentro de fecha.

    El caso contrario importa tanto como el otro: una comprobación que cortara siempre dejaría el
    inventario sin funcionar para todo el mundo y parecería que el conector no existe.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace(
        integration_session,
        caduca_en=datetime.now(UTC) + timedelta(minutes=5),
    )

    class _Cliente:
        def list_repositories(self) -> list[dict[str, object]]:
            return [
                {
                    "id": 501,
                    "name": "vigente",
                    "full_name": "acme/vigente",
                    "clone_url": "https://github.com/acme/vigente.git",
                    "default_branch": "main",
                    "private": False,
                }
            ]

        def close(self) -> None:
            return None

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=AsyncMock(return_value=_Cliente()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            respuesta = await _pedir_inventario(client, cabeceras)

    assert respuesta.status_code == 200
    assert respuesta.json()["items"][0]["full_name"] == "acme/vigente"


@pytest.mark.asyncio
async def test_un_token_personal_sin_caducidad_se_pregunta_al_proveedor(
    integration_session: AsyncSession,
) -> None:
    """`token_expires_at IS NULL` no es «caducado»: es lo que deja un PAT.

    Confundir los dos pediría reconectar credenciales que no tienen nada que caducar, que es un
    bucle distinto del anterior y igual de inútil.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace(integration_session, caduca_en=None)

    class _Cliente:
        def list_repositories(self) -> list[dict[str, object]]:
            return [
                {
                    "id": 502,
                    "name": "pat",
                    "full_name": "acme/pat",
                    "clone_url": "https://github.com/acme/pat.git",
                    "default_branch": "main",
                    "private": True,
                }
            ]

        def close(self) -> None:
            return None

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=AsyncMock(return_value=_Cliente()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            respuesta = await _pedir_inventario(client, cabeceras)

    assert respuesta.status_code == 200


@pytest.mark.asyncio
async def test_una_credencial_rechazada_por_el_proveedor_responde_401(
    integration_session: AsyncSession,
) -> None:
    """El `401` del proveedor sale como `401`, no como `502`.

    Con un `502` el panel no distinguía «tu credencial la rechazó el proveedor» de «GitHub está
    caído», y las dos salían con el mismo texto. Y lo que contestaba era `502` mientras el alta de
    token personal, que es la otra mitad de la misma credencial, contestaba `400` por el mismo
    `401`: dos rutas, una causa, dos códigos.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace(integration_session)

    class _ClienteRechazado:
        def list_repositories(self) -> list[dict[str, object]]:
            raise GitClientError("El proveedor Git rechazó la operación", status_code=401)

        def close(self) -> None:
            return None

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=AsyncMock(return_value=_ClienteRechazado()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            respuesta = await _pedir_inventario(client, cabeceras)

    assert respuesta.status_code == 401


@pytest.mark.asyncio
async def test_un_proveedor_caido_responde_502_y_no_401(
    integration_session: AsyncSession,
) -> None:
    """El otro lado de la corrección: un `5xx` del proveedor sigue siendo `502`.

    Sin esta prueba, cambiar `401`→`401` en la traducción sería una manera válida de arreglarlo
    todo, y el panel acabaría diciendo «tu credencial está mal» cuando lo que pasa es que GitHub
    no responde.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace(integration_session)

    class _ClienteCaido:
        def list_repositories(self) -> list[dict[str, object]]:
            raise GitClientError("El proveedor Git devolvió un error temporal", status_code=503)

        def close(self) -> None:
            return None

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=AsyncMock(return_value=_ClienteCaido()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            respuesta = await _pedir_inventario(client, cabeceras)

    assert respuesta.status_code == 502


@pytest.mark.asyncio
async def test_una_credencial_que_no_se_puede_descifrar_responde_500(
    integration_session: AsyncSession,
) -> None:
    """`CryptoError` no es «no hay credencial».

    Antes las tres excepciones caían en el mismo `except` y devolvían `None`, que la ruta traducía
    a `409`: «conecta primero una credencial de GitHub». La fila estaba ahí; lo que no abría era la
    clave con la que se cifró. La respuesta al usuario era reconectar, y reconectar volvía a cifrar
    con la misma clave.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace(integration_session)

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=AsyncMock(side_effect=CryptoError("No se pudo autenticar el secreto cifrado")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            respuesta = await _pedir_inventario(client, cabeceras)

    assert respuesta.status_code == 500
    assert respuesta.status_code != 409


@pytest.mark.asyncio
async def test_sin_credencial_sigue_siendo_409(
    integration_session: AsyncSession,
) -> None:
    """La caducidad no ha cambiado el caso de «no hay ninguna credencial»."""

    assert integration_session is not None
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Sin nada {suffix}", slug=f"sin-nada-{suffix}")
    user = User(
        email=f"sin-nada-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Sin Nada",
        email_verified=True,
    )
    integration_session.add_all([organization, user])
    await integration_session.flush()
    integration_session.add(
        Membership(
            organization_id=organization.id,
            user_id=user.id,
            role=RoleEnum.ADMIN,
        )
    )
    await integration_session.commit()
    cabeceras = {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        respuesta = await _pedir_inventario(client, cabeceras)

    assert respuesta.status_code == 409


@pytest.mark.asyncio
async def test_la_caducidad_de_otra_organizacion_no_afecta(
    integration_session: AsyncSession,
) -> None:
    """La caducidad se mira en la fila del tenant, no en la tabla.

    R3. `git_credentials` es una fila por organización y proveedor, y el índice
    `ix_git_credentials_org_provider` es único sobre esas dos columnas. Si el filtro de
    `organization_id` desapareciera, una credencial caducada de **otra** empresa cortaría el
    inventario de esta, y el `410` le diría al usuario que su credencial caducó cuando la suya
    está perfecta.
    """

    assert integration_session is not None
    ajena, cabeceras_ajena = await _workspace(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(days=30),
    )
    propia, cabeceras = await _workspace(integration_session)
    assert ajena.id != propia.id

    class _Cliente:
        def list_repositories(self) -> list[dict[str, object]]:
            return [
                {
                    "id": 601,
                    "name": "sano",
                    "full_name": "acme/sano",
                    "clone_url": "https://github.com/acme/sano.git",
                    "default_branch": "main",
                    "private": False,
                }
            ]

        def close(self) -> None:
            return None

    with patch(
        "backend.apps.repositories.router.build_organization_client",
        new=AsyncMock(return_value=_Cliente()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            sana = await _pedir_inventario(client, cabeceras)
            caducada = await _pedir_inventario(client, cabeceras_ajena)

    assert sana.status_code == 200
    assert caducada.status_code == 410


@pytest.mark.asyncio
async def test_sin_conector_sigue_siendo_501(
    integration_session: AsyncSession,
) -> None:
    """El `501` es el único código que significa «no hay conector».

    Se comprueba para que el arreglo del `401` no se haya hecho tragándose el `501`: si dos
    causas distintas de fallo salieran con el mismo código, el panel volvería a tener el problema
    que se arregla, repartido de otra manera.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace(integration_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        respuesta = await _pedir_inventario(client, cabeceras, provider="BITBUCKET")

    assert respuesta.status_code == 501
