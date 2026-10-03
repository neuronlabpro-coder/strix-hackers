"""Renovación del token de acceso de una credencial OAuth de Git.

## El defecto que este fichero existe para no dejar volver

Un token `gho_` de GitHub dura `expires_in: 28800`: ocho horas. `git_credentials` guardaba el
`refresh_token` cifrado desde la fase 3 y **no lo usaba en ningún sitio**, así que toda conexión
OAuth del producto caducaba a las ocho horas y no se recuperaba sola. `GET
/api/v1/repositories/remote?provider=GITHUB` contestaba `410 Gone` con un mensaje de «caducó» que
era cierto y que no arreglaba nada.

Aquí se comprueba lo que hace ese `410` cuando hay un `refresh_token` detrás: se renueva, la ruta
contesta lo de siempre y la credencial vuelve a servir.

## Por qué el caso de concurrencia necesita **dos conexiones de verdad**

Porque el defecto que se corrige no es «no se renueva», es «se renueva dos veces a la vez». Un doble
con un `Lock` de Python pasaría esta prueba sin corregir nada: el mutex que se pondría en el código
sería el mismo que el de la prueba, y los dos caminos no pasarían por él. Aquí hay dos
`AsyncSession` sobre conexiones distintas de PostgreSQL, de modo que el único bloqueo que puede
serializar las dos es el `FOR UPDATE` de la fila.

Y la segunda sesión **carga la credencial antes**, a propósito. Es lo que hace que
`populate_existing=True` sea comprobable: sin él, SQLAlchemy devuelve al segundo concurrente la
entidad que ya tiene en su mapa de identidad, con la caducidad **anterior** a la renovación que
acaba de hacer el primero, y renueva otra vez con el `refresh_token` que el primero acaba de rotar.
Un `Lock` de Python tampoco arregla eso: serializa, pero entrega el valor viejo.

## Por qué el paso de red es un doble inyectado y no la API de verdad

Porque la batería no habla con GitHub ni con GitLab, y porque un refresco que solo se puede
comprobar contra la red real no se puede comprobar en la revisión de código. Lo que se sustituye es
**la red**, no la lógica: `asegurar_credentialo_vigente` se ejecuta entera, con su doble lectura,
su `FOR UPDATE` y su `populate_existing`. El doble cuenta llamadas, que es justo lo que hay que
medir en el caso de concurrencia.

## Por qué estas pruebas escriben en la base de verdad y lo borran

Solo las dos del bloqueo. Necesitan una segunda conexión que pueda bloquear la fila, y un
`savepoint` de la fixture `integration_session` no sirve para eso: sus filas no las ve nadie más
hasta que la transacción exterior se deshace, y al revés. Esas dos escriben con su propia
sesión y borran la organización en un `finally`, porque la base es la misma que usa la
demostración y el resto de la batería. El resto de las pruebas va por la aplicación con la sesión
con `savepoint` y no dejan nada.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.repositories.models import GitCredential, GitProviderEnum
from backend.apps.repositories.oauth import (
    OAuthConfigurationError,
    OAuthProviderSettings,
    OAuthRefreshRejectedError,
    OAuthRefreshUnavailableError,
    OAuthToken,
    refresh_oauth_token,
)
from backend.apps.repositories.services import get_organization_credential
from backend.apps.repositories.token_refresh import (
    EstadoDeRefresh,
    asegurar_credentialo_vigente,
)
from backend.core.crypto import decrypt_secret, encrypt_secret
from backend.core.database import AsyncSessionLocal
from backend.core.security import create_access_token
from backend.main import app

ACCESS_ANTIGUO = "gho_" + "a" * 36
ACCESS_NUEVO = "gho_" + "b" * 36
REFRESH_ANTIGUO = "ghr_" + "c" * 36
REFRESH_NUEVO = "ghr_" + "d" * 36
REFRESH_AJENO = "ghr_" + "e" * 36
CLIENT_SECRET = "secreto-de-prueba-del-client"

CONFIG_GITHUB = OAuthProviderSettings(
    provider=GitProviderEnum.GITHUB,
    client_id="Iv1.github-client",
    client_secret=CLIENT_SECRET,
    authorize_url="https://github.example.com/login/oauth/authorize",
    token_url="https://github.example.com/login/oauth/access_token",
    scopes=("repo",),
)
CONFIG_GITLAB = OAuthProviderSettings(
    provider=GitProviderEnum.GITLAB,
    client_id="gitlab-client",
    client_secret=CLIENT_SECRET,
    authorize_url="https://gitlab.example.com/oauth/authorize",
    token_url="https://gitlab.example.com/oauth/token",
    scopes=("api",),
)


# --------------------------------------------------------------------------- #
# Dobles
# --------------------------------------------------------------------------- #


class ProveedorFalso:
    """El paso de red del refresco, contando llamadas y guardando lo que recibió."""

    def __init__(
        self,
        *,
        access_token: str = ACCESS_NUEVO,
        refresh_token: str | None = REFRESH_NUEVO,
        expires_in: int | None = 28_800,
        error: Exception | None = None,
        retraso: float = 0.0,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_in = expires_in
        self.error = error
        self.retraso = retraso
        self.llamadas = 0
        self.refresh_recibidos: list[str] = []
        self.configs: list[OAuthProviderSettings] = []

    async def __call__(
        self,
        config: OAuthProviderSettings,
        refresh_token: str,
    ) -> OAuthToken:
        self.llamadas += 1
        self.configs.append(config)
        self.refresh_recibidos.append(refresh_token)
        if self.retraso:
            await asyncio.sleep(self.retraso)
        if self.error is not None:
            raise self.error
        return OAuthToken(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_in=self.expires_in,
        )


class ClienteGitCapturador:
    """Sustituto de `GitHubClient`/`GitLabClient` que anota el token con el que se construyó.

    ## Por qué hace falta y no basta con el código de respuesta

    Porque un `200` con el token **viejo** en memoria también es un `200`, y en GitHub sería un
    `401` un segundo después. La renovación tiene que llegar al cliente de gestión, y eso solo se
    ve mirando qué token recibió el constructor.
    """

    instancias: ClassVar[list[ClienteGitCapturador]] = []

    def __init__(self, access_token: str, *_argumentos: Any) -> None:
        self.access_token = access_token
        ClienteGitCapturador.instancias.append(self)

    def list_repositories(self) -> list[dict[str, object]]:
        return [
            {
                "id": 900,
                "name": "renovada",
                "full_name": "acme/renovada",
                "clone_url": "https://github.example.com/acme/renovada.git",
                "default_branch": "main",
                "private": True,
            }
        ]

    def close(self) -> None:
        return None


@contextmanager
def _refresco_con(proveedor: ProveedorFalso):
    """Sustituye **solo** el paso de red dentro del router.

    `asegurar_credentialo_vigente` se sustituye por un envoltorio que le pasa el doble, así que la
    ejecución es la real: doble lectura, `FOR UPDATE`, `populate_existing` y escritura. Lo único
    que cambia es a quién se le pregunta por el token nuevo.
    """

    real = asegurar_credentialo_vigente

    async def _envoltorio(
        session: AsyncSession,
        organization_id: uuid.UUID,
        provider: GitProviderEnum,
        **kwargs: Any,
    ):
        return await real(session, organization_id, provider, refrescador=proveedor, **kwargs)

    ClienteGitCapturador.instancias = []
    with patch(
        "backend.apps.repositories.router.asegurar_credentialo_vigente",
        new=_envoltorio,
    ):
        yield


@contextmanager
def _peticion_de_inventario():
    """Sustituye la clase de cliente Git de la factoría sin tocar nada más de la construcción."""

    ClienteGitCapturador.instancias = []
    with patch(
        "backend.apps.repositories.clients.factory.GitHubClient",
        new=ClienteGitCapturador,
    ):
        yield


# --------------------------------------------------------------------------- #
# Utilidades de datos
# --------------------------------------------------------------------------- #


async def _credencial_en_sesion(
    session: AsyncSession,
    *,
    provider: GitProviderEnum = GitProviderEnum.GITHUB,
    caduca_en: datetime | None,
    refresh: str | None = REFRESH_ANTIGUO,
) -> Organization:
    """Una organización con su administrador y su credencial, en la sesión de la prueba.

    Lo que se escribe aquí queda dentro del `savepoint` de `integration_session` y se deshace al
    terminar la prueba.
    """

    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Refresh {suffix}", slug=f"refresh-{suffix}")
    user = User(
        email=f"refresh-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Refresh Admin",
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
        _fila_de_credencial(organization.id, provider, caduca_en, refresh)
    )
    await session.commit()
    return organization


async def _credencial_confirmada(
    *,
    provider: GitProviderEnum = GitProviderEnum.GITHUB,
    caduca_en: datetime | None,
    refresh: str | None = REFRESH_ANTIGUO,
) -> uuid.UUID:
    """Igual que la anterior, pero **confirmada**, en una conexión propia.

    ## Por qué esta variante existe

    Porque las dos pruebas del bloqueo necesitan que otra sesión —una con su propia conexión— pueda
    leer y bloquear la fila. Una fila escrita dentro del `savepoint` de `integration_session` es
    invisible para el resto del mundo: bloquearla sería bloquear el vacío.

    Por eso el borrado va aparte y en un `finally`. `_borrar_workspace` es la contrapartida
    obligatoria de esta función, y las dos pruebas del bloqueo la llaman siempre.
    """

    suffix = uuid.uuid4().hex
    async with AsyncSessionLocal() as sesion:
        organization = Organization(name=f"Bloqueo {suffix}", slug=f"bloqueo-{suffix}")
        sesion.add(organization)
        await sesion.flush()
        sesion.add(
            _fila_de_credencial(organization.id, provider, caduca_en, refresh)
        )
        await sesion.commit()
        return organization.id


def _fila_de_credencial(
    organization_id: uuid.UUID,
    provider: GitProviderEnum,
    caduca_en: datetime | None,
    refresh: str | None,
) -> GitCredential:
    return GitCredential(
        organization_id=organization_id,
        provider=provider,
        encrypted_access_token=encrypt_secret(
            ACCESS_ANTIGUO,
            organization_id=str(organization_id),
            provider=provider.value,
            field="access",
        ),
        encrypted_refresh_token=(
            encrypt_secret(
                refresh,
                organization_id=str(organization_id),
                provider=provider.value,
                field="refresh",
            )
            if refresh
            else None
        ),
        token_expires_at=caduca_en,
    )


async def _cabeceras(organization: Organization) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token({'sub': str(organization.id)})}",
        "X-Organization-Id": str(organization.id),
    }


async def _inventario(cabeceras: dict[str, str], provider: str = "GITHUB") -> Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as cliente:
        return await cliente.get(
            "/api/v1/repositories/remote",
            params={"provider": provider},
            headers=cabeceras,
        )


async def _credencial_guardada(
    session: AsyncSession,
    organization_id: uuid.UUID,
) -> GitCredential:
    """La fila tal y como está **en la base**, no tal y como la tiene la sesión en caché.

    ## Por qué `populate_existing`

    Porque la fila suele llevar ya un rato en el mapa de identidad de la sesión —la escribió la
    propia prueba— y SQLAlchemy devolvería esa instancia con los valores de entonces. Sin esto, una
    renovación hecha por otra conexión se mediría como si no hubiera pasado. Es el mismo tropiezo
    que documenta `token_refresh.py`, medido desde el otro lado.
    """

    return (
        await session.execute(
            select(GitCredential)
            .where(GitCredential.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _descifrar(credential: GitCredential, campo: str) -> str:
    cifrado = (
        credential.encrypted_refresh_token
        if campo == "refresh"
        else credential.encrypted_access_token
    )
    return decrypt_secret(
        cifrado,
        organization_id=str(credential.organization_id),
        provider=credential.provider.value,
        field=campo,
    )


async def _borrar_workspace(organization_id: uuid.UUID) -> None:
    """Deshace lo que `_credencial_confirmada` confirmó.

    `organization_id` tiene `ON DELETE CASCADE` hacia `git_credentials`, así que una sola sentencia
    deja la base como estaba. Sin esto, una prueba que fallara por lo que fuera dejaría
    organizaciones con nombre `Bloqueo <hex>` en la base compartida con la demostración.
    """

    async with AsyncSessionLocal() as sesion:
        await sesion.execute(delete(Organization).where(Organization.id == organization_id))
        await sesion.commit()


def _texto_del_log(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(registro.getMessage() for registro in caplog.records)


# --------------------------------------------------------------------------- #
# La renovación, de punta a punta por la ruta que devolvía el 410
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.integration
async def test_una_credencial_caducada_se_renueva_y_el_inventario_responde_200(
    integration_session: AsyncSession,
) -> None:
    """El `410` que contestaba esta ruta se ha convertido en un `200`.

    Antes de este arreglo la credencial caducada no tenía salida: `410` y a reconectar. Aquí el
    proveedor devuelve un token nuevo, la base lo guarda y el cliente de gestión se construye con
    **el token nuevo**, que es lo que se comprueba y no solo el código de respuesta: un `200` con el
    token viejo en memoria sería un `200` que falla en GitHub un segundo después.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(hours=2),
    )
    cabeceras = await _cabeceras(organization)
    proveedor = ProveedorFalso()

    with _refresco_con(proveedor), _peticion_de_inventario():
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 200, respuesta.text
    assert proveedor.llamadas == 1
    assert [instancia.access_token for instancia in ClienteGitCapturador.instancias] == [
        ACCESS_NUEVO
    ]
    guardada = await _credencial_guardada(integration_session, organization.id)
    assert _descifrar(guardada, "access") == ACCESS_NUEVO
    assert guardada.token_expires_at is not None
    assert guardada.token_expires_at > datetime.now(UTC)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_la_renovacion_consume_el_refresh_antiguo_y_guarda_el_que_rota_el_proveedor(
    integration_session: AsyncSession,
) -> None:
    """El `refresh_token` es de un solo uso: hay que guardar el que devuelve el proveedor.

    GitLab rota el `refresh_token` en cada refresco y el anterior queda invalidado. Guardar el
    acceso nuevo **sin** guardar el refresh nuevo dejaría la credencial viva una vez más y muerta en
    la segunda, con un fallo que aparecería ocho horas después y sin nada en los logs que lo
    explicara.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(minutes=1),
    )
    cabeceras = await _cabeceras(organization)
    proveedor = ProveedorFalso()

    with _refresco_con(proveedor), _peticion_de_inventario():
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 200
    # Lo que se descifró y se mandó era el `refresh_token` guardado, y no otra cosa.
    assert proveedor.refresh_recibidos == [REFRESH_ANTIGUO]
    guardada = await _credencial_guardada(integration_session, organization.id)
    assert _descifrar(guardada, "refresh") == REFRESH_NUEVO
    assert REFRESH_NUEVO not in guardada.encrypted_refresh_token
    assert REFRESH_ANTIGUO not in guardada.encrypted_refresh_token


@pytest.mark.asyncio
@pytest.mark.integration
async def test_un_token_vigente_no_llama_al_proveedor(
    integration_session: AsyncSession,
) -> None:
    """Con el token en vigor no se gasta ni una llamada.

    El caso contrario importa tanto como el otro: una comprobación que renovara siempre dejaría el
    inventario sin funcionar para nadie y parecería que el conector no existe. Y renovar en cada
    petición mandaría el `client_secret` de la plataforma a GitHub en cada inventario.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) + timedelta(minutes=5),
    )
    cabeceras = await _cabeceras(organization)
    proveedor = ProveedorFalso()

    with _refresco_con(proveedor), _peticion_de_inventario():
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 200
    assert proveedor.llamadas == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_un_token_que_caduca_dentro_del_margen_se_renueva_antes_de_caducar(
    integration_session: AsyncSession,
) -> None:
    """El «antes de que caduque» del encargo, comprobado contra el reloj.

    El token caduca en 30 segundos y el margen configurado es de 60. Esperar a que expirara
    entregaría al cliente de GitHub un token que se caduca a mitad de la llamada, y el síntoma sería
    un `401` del proveedor en una operación que un minuto antes funcionaba.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) + timedelta(seconds=30),
    )
    cabeceras = await _cabeceras(organization)
    proveedor = ProveedorFalso()

    with _refresco_con(proveedor), _peticion_de_inventario():
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 200
    assert proveedor.llamadas == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_un_token_personal_no_se_renueva_nunca_aunque_le_quede_un_refresh_antiguo(
    integration_session: AsyncSession,
) -> None:
    """`token_expires_at IS NULL` es un PAT, y un PAT no se renueva nunca.

    La combinación que se monta aquí es real, no inventada: `connect_personal_token` sustituye el
    token de acceso y pone la fecha a `NULL` **sin borrar** el `encrypted_refresh_token` de una
    conexión OAuth anterior. Si aquí se intentara renovar, se cambiaría el PAT que el usuario acaba
    de pegar por un token OAuth, sin avisar y sin que la fila diga nada.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=None,
        refresh=REFRESH_ANTIGUO,
    )
    cabeceras = await _cabeceras(organization)
    proveedor = ProveedorFalso()

    with _refresco_con(proveedor), _peticion_de_inventario():
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 200
    assert proveedor.llamadas == 0
    guardada = await _credencial_guardada(integration_session, organization.id)
    assert _descifrar(guardada, "access") == ACCESS_ANTIGUO
    assert guardada.token_expires_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_el_refresh_token_rechazado_responde_410_pidiendo_reconectar(
    integration_session: AsyncSession,
) -> None:
    """Si el `refresh_token` también caducó o fue revocado, hay que decirlo.

    Lo que **no** puede ser es un `500` ni un reintento infinito: el primero esconde que el problema
    tiene arreglo y el segundo quema cuota hasta que el proveedor bloquea la aplicación. Y lo que no
    puede ser **nunca** es un `401`, porque en el panel un `401` significa «tu sesión ha caducado» y
    `lib/session-errors.ts` borra la sesión cuando lo ve.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(days=2),
    )
    cabeceras = await _cabeceras(organization)
    proveedor = ProveedorFalso(error=OAuthRefreshRejectedError("refresh revocado"))

    with _refresco_con(proveedor), _peticion_de_inventario():
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 410
    assert respuesta.status_code != 401
    assert "volver a conectarla" in respuesta.json()["detail"]
    # Se intentó una vez y solo una: reintentar aquí es lo que convierte un rechazo en un bloqueo
    # de la aplicación OAuth por parte del proveedor.
    assert proveedor.llamadas == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_un_proveedor_caido_durante_el_refresco_responde_502_y_no_410(
    integration_session: AsyncSession,
) -> None:
    """GitHub caído y credencial caducada son dos hechos, y no pueden salir con el mismo código.

    Si el panel recibiera `410` con GitHub caído pediría reconectar; el usuario haría el viaje
    entero hasta la pantalla de consentimiento de GitHub y GitHub seguiría caído. `502` es lo que
    ya contesta `_translate_client_error` cuando el proveedor no responde, y dos rutas que
    consumen la misma credencial no pueden decir cosas distintas de la misma causa.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(hours=1),
    )
    cabeceras = await _cabeceras(organization)
    proveedor = ProveedorFalso(error=OAuthRefreshUnavailableError("GitHub no respondió"))

    with _refresco_con(proveedor), _peticion_de_inventario():
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 502


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sin_oauth_app_configurada_el_refresco_responde_503(
    integration_session: AsyncSession,
) -> None:
    """Si a la instalación le falta el `client_secret`, reconectar no lo arregla.

    Decirle «vuelve a conectar» al usuario lo devuelve a la pantalla de consentimiento, autoriza de
    nuevo, y la fila vuelve a quedarse sin poder renovarse: un bucle. `503` es lo que ya contesta
    `authorize` para este caso, y por lo menos el ticket dice qué hay que arreglar.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(hours=3),
    )
    cabeceras = await _cabeceras(organization)
    sin_configurar = OAuthProviderSettings(
        provider=GitProviderEnum.GITHUB,
        client_id="",
        client_secret="",
        authorize_url=CONFIG_GITHUB.authorize_url,
        token_url=CONFIG_GITHUB.token_url,
        scopes=("repo",),
    )
    proveedor = ProveedorFalso()

    with (
        _refresco_con(proveedor),
        _peticion_de_inventario(),
        patch(
            "backend.apps.repositories.token_refresh.get_oauth_provider_settings",
            return_value=sin_configurar,
        ),
    ):
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 503
    # Ni se llega a preguntar: no hay con quién preguntar.
    assert proveedor.llamadas == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_el_mensaje_de_reconectar_no_revela_el_refresh_token(
    integration_session: AsyncSession,
) -> None:
    """Ni la respuesta al cliente ni el log pueden llevar el secreto.

    Se fuerza el peor caso: el doble de red propaga un mensaje que **sí** contiene el token, como
    haría un intermediario que lo repitiera. El módulo tiene que seguir sin sacarlo, porque el
    `detail` de un `HTTPException` y el `logger.warning` de un rechazo acaban en la misma consola
    que un `docker logs`.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(days=3),
    )
    cabeceras = await _cabeceras(organization)
    proveedor = ProveedorFalso(
        error=OAuthRefreshRejectedError(f"refresh {REFRESH_ANTIGUO} revocado"),
    )

    with _refresco_con(proveedor), _peticion_de_inventario():
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 410
    assert REFRESH_ANTIGUO not in respuesta.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_la_renovacion_no_escribe_ningun_secreto_en_el_log(
    integration_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """El recorrido entero del refresco, con los cuatro secretos vigilados.

    Se vigilan el token de acceso viejo, el nuevo, el `refresh_token` y el `client_secret` de la
    plataforma, porque los cuatro pasan por este camino y los cuatro valen dinero. Se vigila el
    `caplog` entero y no solo el módulo del refresco: un token filtrado por un `print` o por el
    `repr` de una excepción también cuenta.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(hours=4),
    )
    cabeceras = await _cabeceras(organization)
    proveedor = ProveedorFalso()

    with (
        caplog.at_level(logging.DEBUG),
        _refresco_con(proveedor),
        _peticion_de_inventario(),
    ):
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 200
    texto = _texto_del_log(caplog)
    for secreto in (
        ACCESS_ANTIGUO,
        ACCESS_NUEVO,
        REFRESH_ANTIGUO,
        REFRESH_NUEVO,
        CLIENT_SECRET,
    ):
        assert secreto not in texto
    assert ACCESS_NUEVO not in respuesta.text
    assert "encrypted_access_token" not in respuesta.text
    # Y el log sí dice algo: una renovación sin rastro es un fallo silencioso.
    assert "renovada" in texto


@pytest.mark.asyncio
@pytest.mark.integration
async def test_una_renovacion_sin_expires_in_avisa_en_vez_de_callarse(
    integration_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un proveedor que no dice cuánto vale su token deja la credencial sin fecha.

    Se guarda `None`, que en esta tabla significa «no caduca», así que esa credencial deja de
    renovarse sola. Es un dato que el proveedor no dio y no se puede inventar, pero **sí** se puede
    decir en voz alta: un token que nadie vuelve a mirar es peor que uno que se sabe caducado, y
    esto no puede pasar sin dejar rastro.
    """

    assert integration_session is not None
    organization = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(hours=5),
    )
    cabeceras = await _cabeceras(organization)
    proveedor = ProveedorFalso(expires_in=None)

    with (
        caplog.at_level(logging.WARNING),
        _refresco_con(proveedor),
        _peticion_de_inventario(),
    ):
        respuesta = await _inventario(cabeceras)

    assert respuesta.status_code == 200
    guardada = await _credencial_guardada(integration_session, organization.id)
    assert guardada.token_expires_at is None
    assert any(
        "expires_in" in registro.getMessage() and registro.levelno >= logging.WARNING
        for registro in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_renovar_una_organizacion_no_toca_la_credencial_de_otra(
    integration_session: AsyncSession,
) -> None:
    """R3: cada tenant renueva lo suyo y solo lo suyo.

    La fila de la organización sana no se lee, no se renueva y no se modifica. Un filtro por
    `organization_id` que desapareciera haría que las dos organizaciones compartieran fila, y una
    vería renovar su credencial al ordenar el inventario de la otra.
    """

    assert integration_session is not None
    caducada = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) - timedelta(hours=6),
    )
    sana = await _credencial_en_sesion(
        integration_session,
        caduca_en=datetime.now(UTC) + timedelta(hours=6),
    )
    assert caducada.id != sana.id
    proveedor = ProveedorFalso()

    with _refresco_con(proveedor), _peticion_de_inventario():
        respuesta_sana = await _inventario(await _cabeceras(sana))

    assert respuesta_sana.status_code == 200
    assert proveedor.llamadas == 0

    fila_sana = await _credencial_guardada(integration_session, sana.id)
    assert _descifrar(fila_sana, "access") == ACCESS_ANTIGUO
    fila_caducada = await _credencial_guardada(integration_session, caducada.id)
    assert _descifrar(fila_caducada, "access") == ACCESS_ANTIGUO


# --------------------------------------------------------------------------- #
# El bloqueo. Dos conexiones de verdad.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dos_refrescos_simultaneos_llaman_una_vez_al_proveedor() -> None:
    """El caso que un refresco ingenuo se come: dos peticiones, un `refresh_token`.

    Dos sesiones sobre **conexiones distintas**, porque un doble con un mutex pasaría esta prueba
    sin corregir nada. La segunda además carga la credencial antes de empezar, que es lo que
    convierte `populate_existing=True` en algo comprobable en vez de algo declarativo.

    ## Por qué el desfase entre las dos tareas

    Porque si las dos empezaran exactamente a la vez, la que llegue segunda podría leer la fila ya
    renovada en su lectura **sin** bloqueo y no intentaría nada: la prueba pasaría con el código
    roto. Con 50 ms de desfase y una llamada al doble que dura 200 ms, la segunda está bloqueada en
    el `FOR UPDATE` mientras la primera renueva, y entonces sí se mide lo que se quiere medir.
    """

    organization_id = await _credencial_confirmada(
        caduca_en=datetime.now(UTC) - timedelta(hours=7),
    )
    proveedor = ProveedorFalso(retraso=0.2)

    try:
        async with AsyncSessionLocal() as primera, AsyncSessionLocal() as segunda:
            # La segunda carga la entidad **antes**: sin `populate_existing`, SQLAlchemy le
            # devolverá la instancia cacheada con la caducidad anterior a la renovación.
            await get_organization_credential(segunda, organization_id, GitProviderEnum.GITHUB)

            tarea_primera = asyncio.create_task(
                asegurar_credentialo_vigente(
                    primera,
                    organization_id,
                    GitProviderEnum.GITHUB,
                    config=CONFIG_GITHUB,
                    refrescador=proveedor,
                )
            )
            await asyncio.sleep(0.05)
            tarea_segunda = asyncio.create_task(
                asegurar_credentialo_vigente(
                    segunda,
                    organization_id,
                    GitProviderEnum.GITHUB,
                    config=CONFIG_GITHUB,
                    refrescador=proveedor,
                )
            )
            resultados = await asyncio.gather(tarea_primera, tarea_segunda)

        assert proveedor.llamadas == 1
        assert {resultado.estado for resultado in resultados} == {
            EstadoDeRefresh.REFRESCADO,
            EstadoDeRefresh.VIGENTE,
        }
    finally:
        await _borrar_workspace(organization_id)

    async with AsyncSessionLocal() as lector:
        fila = await _credencial_guardada(lector, organization_id)
    assert _descifrar(fila, "access") == ACCESS_NUEVO
    assert _descifrar(fila, "refresh") == REFRESH_NUEVO


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dos_organizaciones_se_renuevan_a_la_vez_sin_bloquearse() -> None:
    """El bloqueo es por fila, no una puerta global.

    El error contrario al de serializar de más es poner una puerta única: el inventario de dos
    clientes distintos se encolaría el uno al otro detrás de la llamada al proveedor del otro. Con
    bloqueo por fila las dos renovar a la vez, y cada una con **su** `refresh_token`, que es lo que
    se comprueba con los dos valores distintos.
    """

    primera_id = await _credencial_confirmada(
        caduca_en=datetime.now(UTC) - timedelta(hours=8),
        refresh=REFRESH_ANTIGUO,
    )
    segunda_id = await _credencial_confirmada(
        caduca_en=datetime.now(UTC) - timedelta(hours=9),
        refresh=REFRESH_AJENO,
    )
    proveedor = ProveedorFalso(retraso=0.2)

    try:
        async with AsyncSessionLocal() as sesion_primera, AsyncSessionLocal() as sesion_segunda:
            resultados = await asyncio.gather(
                asegurar_credentialo_vigente(
                    sesion_primera,
                    primera_id,
                    GitProviderEnum.GITHUB,
                    config=CONFIG_GITHUB,
                    refrescador=proveedor,
                ),
                asegurar_credentialo_vigente(
                    sesion_segunda,
                    segunda_id,
                    GitProviderEnum.GITHUB,
                    config=CONFIG_GITHUB,
                    refrescador=proveedor,
                ),
            )

        assert [resultado.estado for resultado in resultados] == [
            EstadoDeRefresh.REFRESCADO,
            EstadoDeRefresh.REFRESCADO,
        ]
        assert proveedor.llamadas == 2
        assert sorted(proveedor.refresh_recibidos) == sorted(
            [REFRESH_ANTIGUO, REFRESH_AJENO]
        )
    finally:
        await _borrar_workspace(primera_id)
        await _borrar_workspace(segunda_id)


# --------------------------------------------------------------------------- #
# El endpoint, contra un transporte falso. Sin base de datos.
# --------------------------------------------------------------------------- #


@contextmanager
def _transporte(manejador):
    """Sustituye el `httpx.AsyncClient` que usa `oauth.refresh_oauth_token`.

    Se sustituye el **transporte**, no el cliente: se captura la clase original antes de parchear,
    para que la fábrica no se llame a sí misma, y el resto de argumentos —`timeout`,
    `follow_redirects`— se pasan tal cual. Así la prueba mide la petición que se hace de verdad.
    """

    cliente_real = httpx.AsyncClient

    def _fabrica(**kwargs: Any) -> httpx.AsyncClient:
        return cliente_real(transport=httpx.MockTransport(manejador), **kwargs)

    with patch.object(httpx, "AsyncClient", _fabrica):
        yield


def _cuerpo(peticion: httpx.Request) -> dict[str, str]:
    return {
        clave: valores[0]
        for clave, valores in parse_qs(peticion.content.decode("utf-8")).items()
    }


@pytest.mark.asyncio
async def test_el_refresco_de_github_va_al_endpoint_de_la_configuracion() -> None:
    """La URL del refresco sale de la configuración, no de un literal en el código.

    Es lo que hace que GitLab sea un caso y no una copia: los dos proveedores usan **el mismo**
    endpoint para canjear y para refrescar, así que el mismo `token_url` sirve para los dos. Si
    alguien escribiera `https://github.com/login/oauth/access_token` a mano, esta prueba seguiría
    pasando con GitHub —que es exactamente el mismo valor— y GitLab dejaría de renovar en
    producción sin que nada se diera cuenta.
    """

    peticiones: list[httpx.Request] = []

    async def _manejador(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS_NUEVO,
                "refresh_token": REFRESH_NUEVO,
                "expires_in": 28_800,
                "token_type": "bearer",
            },
        )

    async with _transporte(_manejador):
        token = await refresh_oauth_token(CONFIG_GITHUB, REFRESH_ANTIGUO)

    assert token.access_token == ACCESS_NUEVO
    assert token.refresh_token == REFRESH_NUEVO
    assert token.expires_in == 28_800
    assert len(peticiones) == 1
    peticion = peticiones[0]
    assert peticion.method == "POST"
    assert str(peticion.url) == "https://github.example.com/login/oauth/access_token"
    cuerpo = _cuerpo(peticion)
    assert cuerpo["grant_type"] == "refresh_token"
    assert cuerpo["refresh_token"] == REFRESH_ANTIGUO
    assert cuerpo["client_id"] == CONFIG_GITHUB.client_id
    assert cuerpo["client_secret"] == CONFIG_GITHUB.client_secret
    # Sin `Accept: application/json`, GitHub contesta en `x-www-form-urlencoded` y el `json()` de
    # arriba no habría funcionado nunca en producción.
    assert peticion.headers["accept"] == "application/json"


@pytest.mark.asyncio
async def test_el_refresco_de_gitlab_va_a_su_propio_endpoint() -> None:
    """GitLab usa `POST /oauth/token` con `grant_type=refresh_token`, que es lo mismo que su canje.

    Se comprueba aparte porque la prueba de GitHub no la cubre: son dos dominios distintos, y un
    `token_url` escrito a mano para GitHub rompería GitLab sin tocar GitHub.
    """

    peticiones: list[httpx.Request] = []

    async def _manejador(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
        return httpx.Response(
            200,
            json={
                "access_token": "glpat-nuevo",
                "refresh_token": "gl-refresh-nuevo",
                "expires_in": 7200,
                "token_type": "Bearer",
            },
        )

    async with _transporte(_manejador):
        token = await refresh_oauth_token(CONFIG_GITLAB, "gl-refresh-antiguo")

    assert token.access_token == "glpat-nuevo"
    assert token.refresh_token == "gl-refresh-nuevo"
    assert token.expires_in == 7200
    assert str(peticiones[0].url) == "https://gitlab.example.com/oauth/token"
    assert _cuerpo(peticiones[0])["grant_type"] == "refresh_token"


@pytest.mark.asyncio
async def test_un_200_con_error_de_github_es_un_rechazo_y_no_un_token() -> None:
    """GitHub contesta `200` con `{"error": ...}` cuando el `refresh_token` ya no vale.

    Con `raise_for_status()` —que es lo que hace el canje de código— esto no se detectaría: el
    cuerpo se leería como un token válido y la credencial se guardaría rota. Es el motivo por el que
    este módulo no llama a `raise_for_status`.
    """

    async def _manejador(_peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": "bad_refresh_token",
                "error_description": f"The refresh token {REFRESH_ANTIGUO} is invalid",
            },
        )

    async with _transporte(_manejador):
        with pytest.raises(OAuthRefreshRejectedError) as fallo:
            await refresh_oauth_token(CONFIG_GITHUB, REFRESH_ANTIGUO)

    # El mensaje dice **por qué** lo rechazó el proveedor, y no repite ni el token ni la
    # descripción —que es el campo donde un intermediario devolvería el valor enviado.
    assert "bad_refresh_token" in str(fallo.value)
    assert REFRESH_ANTIGUO not in str(fallo.value)
    assert "is invalid" not in str(fallo.value)


@pytest.mark.asyncio
async def test_un_5xx_al_refrescar_es_transitorio_y_no_un_rechazo() -> None:
    """GitHub caído no es «tu refresh_token está revocado».

    Confundir los dos convertiría una caída del proveedor en una instrucción de reconectar que no
    arregla nada y borra una integración que estaba bien.
    """

    async def _manejador(_peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    async with _transporte(_manejador):
        with pytest.raises(OAuthRefreshUnavailableError):
            await refresh_oauth_token(CONFIG_GITHUB, REFRESH_ANTIGUO)


@pytest.mark.asyncio
async def test_un_endpoint_sin_https_no_se_llega_a_enviar_el_refresh_token() -> None:
    """Sin HTTPS no se envía ni el `refresh_token` ni el `client_secret` de la plataforma.

    `validate_oauth_provider_settings` se llama antes de abrir el cliente, y por eso una URL mal
    puesta en la configuración no convierte la plataforma en algo que manda el secreto del cliente a
    un sitio que se lo queda.
    """

    sin_https = OAuthProviderSettings(
        provider=GitProviderEnum.GITHUB,
        client_id="Iv1.github-client",
        client_secret=CLIENT_SECRET,
        authorize_url="http://github.example.com/login/oauth/authorize",
        token_url="http://github.example.com/login/oauth/access_token",
        scopes=("repo",),
    )
    enviados: list[httpx.Request] = []

    async def _manejador(peticion: httpx.Request) -> httpx.Response:
        enviados.append(peticion)
        return httpx.Response(200, json={"access_token": ACCESS_NUEVO})

    async with _transporte(_manejador):
        with pytest.raises(OAuthConfigurationError):
            await refresh_oauth_token(sin_https, REFRESH_ANTIGUO)

    assert enviados == []
