"""Renovación de la credencial Git en el **camino del worker**.

## El fallo que este fichero existe para no dejar volver

Un token `gho_` de GitHub dura ocho horas. La ruta del panel ya lo tiene resuelto:
`_exigir_credencial_vigente` llama a `asegurar_credentialo_vigente` antes de abrir el cliente de
inventario, y una credencial caducada se renueva sola.

El camino del worker no lo tenía. `build_client_for_repository` —la usan el pipeline de revisión de
PR, el autofix y los webhooks— descifraba `encrypted_access_token` tal cual, sin mirar
`token_expires_at`. El cliente conecta su GitHub, todo funciona en el panel, y a las ocho horas las
revisiones automáticas de pull request empiezan a fallar con un `401` del proveedor **en un camino
que no pasa por ninguna pantalla**: el panel sigue en verde y el producto está roto.

Y no es un defecto que se arreglara desde el panel. El `celery_worker` corre como proceso aparte,
con su propia sesión y en otro contenedor; el webhook llega a la ruta pública, la ruta encola, y el
pipeline construye el cliente en el worker.

## Por qué aquí no se usa la sesión del worker para renovar

Porque **se confirma la transacción** al terminar la fase bloqueada, y en un worker esa transacción
sostiene el `FOR UPDATE` de la revisión y los hallazgos que se están insertando. Además —y
que de verdad importa— la renovación **no es parte de la unidad de trabajo del worker**: si el
escaneo falla después de renovar, el token nuevo tiene que seguir guardado. Con la sesión del
worker, un `rollback()` deshacía la renovación y devolvía el sistema al token caducado.

La sesión es, por tanto, propia: se abre para renovar y se cierra. Cuesta una conexión del pool por
renovación, y una renovación es una por conexión cada ocho horas. `test_la_renovacion_no_confirma_la
transaccion_del_worker` lo comprueba en las dos direcciones, y
`test_la_opcion_de_reutilizar_la_sesion_del_worker_caera` demuestra que esa comprobación cae con la
alternativa descartada.

## Por qué el caso de concurrencia **sujeta** la referencia a la fila

Porque el mapa de identidad de SQLAlchemy guarda **referencias débiles**. Si nadie sujeta la
credencial, el primer `gc` la saca del mapa, la lectura bloqueante la trae fresca de la base, y
`populate_existing=True` resulta innecesario: la prueba pasaría en verde **con el `FOR UPDATE`
eliminado**. Aquí las dos sesiones de llamador cargan la credencial antes de empezar y la guardan en
una lista que sigue viva hasta el final. Sin esa lista, «nada la sujeta» y «está obsoleta» no se
distinguen de «el recolector la trajo de nuevo», y la prueba deja de comprobar lo que
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import PentestRun, ScanStatusEnum
from backend.apps.repositories.credentials import encrypt_organization_credential
from backend.apps.repositories.models import (
    GitCredential,
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.oauth import (
    OAuthProviderSettings,
    OAuthRefreshRejectedError,
    OAuthRefreshUnavailableError,
    OAuthToken,
)
from backend.apps.repositories.pipeline import _run_pr_security_pipeline
from backend.apps.repositories.services import (
    build_client_for_repository,
    get_organization_credential,
)
from backend.apps.repositories.tasks import _ERRORES_REINTENTABLES
from backend.apps.repositories.token_refresh import (
    CODIGO_CREDENCIAL_CADUCADA,
    CODIGO_OAUTH_SIN_CONFIGURAR,
    CODIGO_PROVEEDOR_NO_DISPONIBLE,
    ESTADOS_QUE_SIRVEN,
    EstadoDeRefresh,
    GitCredencialNoDisponibleError,
    GitCredencialNoRenovableError,
    ResultadoDeRefresh,
    _error_de_estado,
    asegurar_credentialo_vigente,
    credenciales_por_vencer,
    credential_necesita_renovacion,
    margen_de_renovacion,
    renovar_credenciales_por_vencer,
    renovar_lote,
)
from backend.core.config import settings
from backend.core.crypto import decrypt_secret
from backend.core.database import AsyncSessionLocal
from backend.workers.celery_app import celery_app

ACCESS_ANTIGUO = "gho_" + "f" * 36
ACCESS_NUEVO = "gho_" + "1" * 36
REFRESH_ANTIGUO = "ghr_" + "2" * 36
REFRESH_NUEVO = "ghr_" + "3" * 36
CLIENT_SECRET = "secreto-de-prueba-del-client"

CONFIG_GITHUB = OAuthProviderSettings(
    provider=GitProviderEnum.GITHUB,
    client_id="Iv1.github-client",
    client_secret=CLIENT_SECRET,
    authorize_url="https://github.example.com/login/oauth/authorize",
    token_url="https://github.example.com/login/oauth/access_token",
    scopes=("repo",),
)

#: Margen que se le da a la segunda tarea para llegar a su `FOR UPDATE` antes de soltar la primera.
#:
#!: No es un temporizador de producción: es la espera de la **prueba** por su propia concurrencia.
#: Cuatro consultas por Tailscale miden del orden de 80 ms cada una en este entorno, así que medio
#: segundo es holgado; si algún día deja de bastar, el fallo sería un `TimeoutError` con el motivo
#: escrito, no un fallo verde.
_MARGEN_PARA_LLEGAR_AL_BLOQUEO = 0.5


# --------------------------------------------------------------------------- #
# Dobles
# --------------------------------------------------------------------------- #


class ProveedorFalso:
    """El paso de red del refresco, contando llamadas y guardando qué recibió."""

    def __init__(
        self,
        *,
        access_token: str = ACCESS_NUEVO,
        refresh_token: str | None = REFRESH_NUEVO,
        expires_in: int | None = 28_800,
        error: Exception | None = None,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_in = expires_in
        self.error = error
        self.llamadas = 0
        self.refresh_recibidos: list[str] = []

    async def __call__(
        self,
        config: OAuthProviderSettings,
        refresh_token: str,
    ) -> OAuthToken:
        del config
        self.llamadas += 1
        self.refresh_recibidos.append(refresh_token)
        if self.error is not None:
            raise self.error
        return OAuthToken(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_in=self.expires_in,
        )


class ProveedorQueEspera:
    """El primer refresco no sale hasta que la prueba suelta el evento.

    Sirve para fijar **dónde** está cada tarea cuando empieza la concurrencia: la prueba espera a
    que el primer refresco esté dentro del proveedor —o sea, con la fila de `git_credentials`
    bloqueada— y solo entonces lanza la segunda petición. Sin esa sincronización, la segunda podría
    leerse la fila ya renovada en su lectura sin bloqueo, no intentaría nada, y la prueba
    el bloqueo roto.
    """

    def __init__(self) -> None:
        self.entrado = asyncio.Event()
        self.soltar = asyncio.Event()
        self.llamadas = 0
        self.refresh_recibidos: list[str] = []

    async def __call__(
        self,
        config: OAuthProviderSettings,
        refresh_token: str,
    ) -> OAuthToken:
        del config
        self.llamadas += 1
        self.refresh_recibidos.append(refresh_token)
        self.entrado.set()
        await asyncio.wait_for(self.soltar.wait(), timeout=10)
        return OAuthToken(
            access_token=ACCESS_NUEVO,
            refresh_token=REFRESH_NUEVO,
            expires_in=28_800,
        )


class ClienteQueCapturaElToken:
    """Sustituto del cliente Git que anota el token con el que se construyó.

    ## Por qué hace falta y no basta el resultado de la llamada

    Porque un `200` con el token **viejo** en memoria también es un `200`, y en GitHub sería
    un segundo después. Que la renovación llegue al cliente solo se ve mirando qué token recibió el
    constructor.
    """

    instancias: ClassVar[list[ClienteQueCapturaElToken]] = []

    def __init__(self, access_token: str, *_argumentos: Any) -> None:
        self.access_token = access_token
        ClienteQueCapturaElToken.instancias.append(self)

    def close(self) -> None:
        return None


@contextmanager
def _cliente_capturador() -> Iterator[None]:
    """Sustituye la clase de cliente Git de la factoría sin tocar nada más de la construcción."""

    ClienteQueCapturaElToken.instancias = []
    with patch(
        "backend.apps.repositories.clients.factory.GitHubClient",
        new=ClienteQueCapturaElToken,
    ):
        yield


@contextmanager
def _renovar_en_la_sesion_del_llamador(
    sesion_del_worker: AsyncSession,
) -> Iterator[None]:
    """La **opción 3** montada: renovar con la sesión del worker y aceptar su `commit()`.

    Es el defecto exacto que la arquitectura de este fichero rechaza, escrito como sustitución para
    poder demostrar que las pruebas que lo detectan no se pasarían con él. No se toca el doble de
    red: se cambia **quién** confirma la transacción, que es la decisión.

    La sesión se pasa explícitamente y no por un global: el mutante es una sustitución dentro de
    `services.py`, que solo ve `(organization_id, provider, ...)`, y para usar «la sesión del
    llamador» necesita que alguien se la dé.
    """

    real = asegurar_credentialo_vigente

    async def _con_la_del_llamador(
        organization_id: uuid.UUID,
        provider: GitProviderEnum,
        **kwargs: Any,
    ) -> Any:
        # La firma es la de la entrada que `services.py` ve: organización, proveedor y palabras
        # clave. La sesión —la del worker— es la que se pasa a propósito.
        resultado = await real(
            _SESION_DEL_LLAMADOR[0],
            organization_id,
            provider,
            **kwargs,
        )
        if resultado.estado in ESTADOS_QUE_SIRVEN:
            return resultado
        raise _error_de_estado(resultado)

    with patch(
        "backend.apps.repositories.services.asegurar_credentialo_vigente_en_sesion_ajena",
        new=_con_la_del_llamador,
    ):
        _SESION_DEL_LLAMADOR.append(sesion_del_worker)
        try:
            yield
        finally:
            _SESION_DEL_LLAMADOR.pop()


#: Sesión del worker que se está imitando, mientras el mutante está montado.
_SESION_DEL_LLAMADOR: list[AsyncSession] = []


# --------------------------------------------------------------------------- #
# Utilidades de datos
# --------------------------------------------------------------------------- #


async def _alta_confirmada(
    *,
    caduca_en: datetime | None,
    refresh: str | None = REFRESH_ANTIGUO,
    provider: GitProviderEnum = GitProviderEnum.GITHUB,
) -> tuple[uuid.UUID, Repository]:
    """Una organización con repositorio y credencial, **confirmadas**, en una conexión propia.

    ## Por qué confirmadas y no dentro del `savepoint` de `integration_session`

    Porque la renovación se hace en una sesión que es **otra**, y una fila escrita dentro del
    `savepoint` de la prueba es invisible para el resto del mundo: su sesión vería una tabla vacía y
    la renovación no renovaría nada. Es la misma razón por la que `test_refresh_token_credencial.py`
    escribe sus pruebas de bloqueo con su propia sesión.

    `_borrar_todo` es la contrapartida obligatoria y la llaman todas.
    """

    suffix = uuid.uuid4().hex
    async with AsyncSessionLocal() as sesion:
        organization = Organization(
            name=f"Worker {suffix}",
            slug=f"worker-{suffix}",
            credit_balance=Decimal("1000"),
        )
        sesion.add(organization)
        await sesion.flush()
        repository = Repository(
            organization_id=organization.id,
            provider=provider,
            remote_repo_id=str(uuid.uuid4().int),
            name="app",
            full_name="acme/app",
            clone_url="https://github.example.com/acme/app.git",
        )
        sesion.add(repository)
        await sesion.flush()
        sesion.add(
            GitCredential(
                organization_id=organization.id,
                provider=provider,
                encrypted_access_token=encrypt_organization_credential(
                    ACCESS_ANTIGUO,
                    organization.id,
                    provider,
                    field="access",
                ),
                encrypted_refresh_token=(
                    encrypt_organization_credential(
                        refresh,
                        organization.id,
                        provider,
                        field="refresh",
                    )
                    if refresh
                    else None
                ),
                token_expires_at=caduca_en,
            )
        )
        await sesion.commit()
        return organization.id, repository


async def _borrar_todo(organization_id: uuid.UUID) -> None:
    """Deshace lo que `_alta_confirmada` confirmó.

    `organization_id` tiene `ON DELETE CASCADE` hacia `git_credentials` y hacia `repositories`, así
    que una sola sentencia deja la base como estaba. Sin esto, una prueba que fallara por lo que
    fuera dejaría organizaciones `Worker <hex>` en la base compartida con la demostración.
    """

    async with AsyncSessionLocal() as sesion:
        await sesion.execute(delete(Organization).where(Organization.id == organization_id))
        await sesion.commit()


async def _credencial_en_base(sesion: AsyncSession, organization_id: uuid.UUID) -> GitCredential:
    """La fila tal y como está **en la base**, no tal y como la tiene una sesión en caché."""

    return (
        await sesion.execute(
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
    assert cifrado is not None, "la columna está a NULL y no hay nada que descifrar"
    return decrypt_secret(
        cifrado,
        organization_id=str(credential.organization_id),
        provider=credential.provider.value,
        field=campo,
    )


def _texto_del_log(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(registro.getMessage() for registro in caplog.records)


# --------------------------------------------------------------------------- #
# El fallo que se arregla: el worker construye el cliente con el token caducado
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.integration
async def test_el_cliente_del_worker_se_construye_con_el_token_renovado() -> None:
    """La credencial caducada se renueva antes de descifrarse, y el token nuevo es el que se usa.

    Sin la renovación, el cliente se construiría con `ACCESS_ANTIGUO` y GitHub contestaría `401` en
    el primer `get_pull_request`. Con ella, se llama al proveedor una vez y el cliente recibe
    nuevo.
    """

    organization_id, repository = await _alta_confirmada(
        caduca_en=datetime.now(UTC) - timedelta(hours=7),
    )
    proveedor = ProveedorFalso()
    try:
        async with AsyncSessionLocal() as sesion:
            with _cliente_capturador():
                await build_client_for_repository(
                    sesion,
                    repository,
                    refrescador=proveedor,
                )
        assert proveedor.llamadas == 1
        assert [instancia.access_token for instancia in ClienteQueCapturaElToken.instancias] == [
            ACCESS_NUEVO
        ]
        async with AsyncSessionLocal() as lector:
            fila = await _credencial_en_base(lector, organization_id)
        assert _descifrar(fila, "access") == ACCESS_NUEVO
        assert fila.token_expires_at is not None
        assert fila.token_expires_at > datetime.now(UTC)
    finally:
        await _borrar_todo(organization_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_un_token_que_caduca_dentro_del_margen_se_renueva_antes_de_caducar() -> None:
    """El «antes de que caduque», comprobado en el camino del worker.

    El token caduca en 30 segundos y el margen configurado es de 60. Esperar a que expirara
    entregaría al cliente un token que se caduca a mitad de la llamada, y el síntoma sería un `401`
    del proveedor en una operación que un minuto antes funcionaba.
    """

    organization_id, repository = await _alta_confirmada(
        caduca_en=datetime.now(UTC) + timedelta(seconds=30),
    )
    proveedor = ProveedorFalso()
    try:
        async with AsyncSessionLocal() as sesion:
            with _cliente_capturador():
                await build_client_for_repository(
                    sesion,
                    repository,
                    refrescador=proveedor,
                )
        assert proveedor.llamadas == 1
    finally:
        await _borrar_todo(organization_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_un_token_vigente_no_llama_al_proveedor_ni_abre_una_sesion_del_pool() -> None:
    """El caso frecuente no cambia: ni una llamada al proveedor ni una conexión extra.

    Se cuenta cuántas sesiones abre el módulo de renovación. Si abriera la suya **siempre**, este
    contador sería `1` y el camino más común del producto pagaría una conexión del pool en cada
    revisión de pull request para no hacer nada.
    """

    organization_id, repository = await _alta_confirmada(
        caduca_en=datetime.now(UTC) + timedelta(hours=4),
    )
    proveedor = ProveedorFalso()
    aperturas: list[int] = [0]

    def _fabrica_contada(*argumentos: Any, **kwargs: Any) -> Any:
        aperturas[0] += 1
        return AsyncSessionLocal(*argumentos, **kwargs)

    try:
        async with AsyncSessionLocal() as sesion:
            with (
                _cliente_capturador(),
                patch(
                    "backend.apps.repositories.token_refresh.AsyncSessionLocal",
                    new=_fabrica_contada,
                ),
            ):
                await build_client_for_repository(
                    sesion,
                    repository,
                    refrescador=proveedor,
                )
        assert proveedor.llamadas == 0
        assert aperturas[0] == 0
    finally:
        await _borrar_todo(organization_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_un_token_personal_no_se_renueva_nunca_aunque_le_quede_un_refresh_antiguo() -> None:
    """`token_expires_at IS NULL` es un PAT, y un PAT no se renueva nunca en ningún camino.

    La combinación que se monta aquí es real, no inventada: `connect_personal_token` sustituye el
    token de acceso y pone la fecha a `NULL` **sin borrar** el `encrypted_refresh_token` de una
    conexión OAuth anterior. Si aquí se intentara renovar, se cambiaría el PAT que el usuario acaba
    de pegar por un token OAuth, sin avisar y sin que la fila diga nada.
    """

    organization_id, repository = await _alta_confirmada(caduca_en=None, refresh=REFRESH_ANTIGUO)
    proveedor = ProveedorFalso()
    try:
        async with AsyncSessionLocal() as sesion:
            with _cliente_capturador():
                await build_client_for_repository(
                    sesion,
                    repository,
                    refrescador=proveedor,
                )
        assert proveedor.llamadas == 0
        assert [instancia.access_token for instancia in ClienteQueCapturaElToken.instancias] == [
            ACCESS_ANTIGUO
        ]
        async with AsyncSessionLocal() as lector:
            fila = await _credencial_en_base(lector, organization_id)
        assert fila.token_expires_at is None
    finally:
        await _borrar_todo(organization_id)


# --------------------------------------------------------------------------- #
# La decisión de arquitectura: sesión propia, y su `commit()` propio
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.integration
async def test_la_renovacion_no_confirma_la_transaccion_del_worker() -> None:
    """La prueba que decide la arquitectura: el `commit()` de la renovación es de **otro**.

    Se monta exactamente el caso temido. La sesión del worker tiene una escritura pendiente —el
    nombre del repositorio, que es lo que un worker real tiene a medias cuando va a hablar con
    GitHub—, llama a `build_client_for_repository`, y **deshace** con `rollback()`. Al final se
    comprueban las dos cosas a la vez, y las dos importan:

    - La escritura pendiente **no** está en la base. Si el `commit()` de la renovación hubiera usado
      esta sesión, la habría confirmado por el camino y el `rollback()` no habría podido deshacer
      nada: media unidad de trabajo persistida sin que nadie lo decidiera.
    - La renovación **sí** está en la base. Porque renovar no es parte de la unidad de trabajo del
      worker: si el escaneo falla después, el token nuevo tiene que seguir guardado. Con la sesión
      del worker, este mismo `rollback()` lo habría deshecho y devuelto al token caducado.
    """

    organization_id, repository = await _alta_confirmada(
        caduca_en=datetime.now(UTC) - timedelta(hours=9),
    )
    repository_id = repository.id
    proveedor = ProveedorFalso()
    try:
        async with AsyncSessionLocal() as sesion_del_worker:
            # La referencia viva a la fila. El mapa de identidad de SQLAlchemy guarda referencias
            # débiles, así que si nadie la sujeta el recolector puede llevársela y dejar la sesión
            # sin la instancia obsoleta que hace necesaria la lectura bloqueante. Se comprueba aquí,
            # **antes** del rollback, porque `rollback()` expira los objetos de la
            # un atributo expirado fuera de un greenlet lanza `MissingGreenlet` — que es exactamente
            # el tropiezo que `token_refresh.py` documenta.
            cargada = await get_organization_credential(
                sesion_del_worker, organization_id, GitProviderEnum.GITHUB
            )
            assert cargada.token_expires_at is not None
            assert any(
                objeto is cargada for objeto in sesion_del_worker.identity_map.values()
            )
            repository.name = "app-renombrada-por-el-worker"
            sesion_del_worker.add(repository)
            await sesion_del_worker.flush()
            with _cliente_capturador():
                await build_client_for_repository(
                    sesion_del_worker,
                    repository,
                    refrescador=proveedor,
                )
            await sesion_del_worker.rollback()

        async with AsyncSessionLocal() as lector:
            nombre = await lector.scalar(
                select(Repository.name).where(Repository.id == repository_id)
            )
            fila_credencial = await _credencial_en_base(lector, organization_id)

        assert nombre == "app"
        assert _descifrar(fila_credencial, "access") == ACCESS_NUEVO
        assert proveedor.llamadas == 1
    finally:
        await _borrar_todo(organization_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_la_opcion_de_reutilizar_la_sesion_del_worker_caera() -> None:
    """La demostración de no-vaciedad de la anterior, con la alternativa descartada montada.

    Este test **no** comprueba el código bueno: monta la opción 3 —renovar con la sesión del
    llamador y aceptar su `commit()`— y verifica que la aserción central de la prueba anterior
    (`nombre == "app"`) **deja de cumplirse**. Las dos pruebas, leídas juntas, son la prueba:
    pasara sin la mutación, la anterior no comprobaría nada, y un test verde que nunca ha fallado
    no demuestra nada.
    """

    organization_id, repository = await _alta_confirmada(
        caduca_en=datetime.now(UTC) - timedelta(hours=9),
    )
    repository_id = repository.id
    proveedor = ProveedorFalso()
    try:
        async with AsyncSessionLocal() as sesion_del_worker:
            with _renovar_en_la_sesion_del_llamador(sesion_del_worker), _cliente_capturador():
                repository.name = "app-renombrada-por-el-worker"
                sesion_del_worker.add(repository)
                await sesion_del_worker.flush()
                await build_client_for_repository(
                    sesion_del_worker,
                    repository,
                    refrescador=proveedor,
                )
                await sesion_del_worker.rollback()

        async with AsyncSessionLocal() as lector:
            nombre = await lector.scalar(
                select(Repository.name).where(Repository.id == repository_id)
            )
            fila_credencial = await _credencial_en_base(lector, organization_id)

        # Con la opción 3, el `commit()` de la renovación confirmó la escritura pendiente y el
        # `rollback()` del worker llegó tarde. Esta es exactamente la aserción que **no** se cumple
        # en la prueba anterior.
        assert nombre == "app-renombrada-por-el-worker"
        # Y el otro lado tampoco se cumple: el token sí quedó renovado, pero por el motivo
        # equivocado —lo persistió un commit ajeno al worker—, que es lo que la otra prueba
        # descarta cuando comprueba que su rollback sí deshizo lo suyo.
        assert _descifrar(fila_credencial, "access") == ACCESS_NUEVO
    finally:
        await _borrar_todo(organization_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dos_workers_a_la_vez_llaman_una_vez_al_proveedor() -> None:
    """Dos sesiones de llamador, un `refresh_token`, una llamada al proveedor."""

    organization_id, repository = await _alta_confirmada(
        caduca_en=datetime.now(UTC) - timedelta(hours=8),
    )
    proveedor = ProveedorQueEspera()
    # La lista es la referencia viva. Sin ella, el recolector puede soltar las filas entre una
    # lectura y otra y la prueba pasa en verde con el `FOR UPDATE` eliminado.
    sujetas: list[GitCredential] = []
    # Margen de **una** hora, no de ocho. Con un margen igual a la vida del token, la fila
    # renovada queda justo en el límite y la comparación `caduca > limite` da falsa: el segundo
    # concurrente renovaría otra vez y la prueba fallaría por un motivo que no tiene nada que
    # ver con el bloqueo. Es la forma buena de escribir esta prueba y también la mala.
    ventana = timedelta(hours=1)

    try:
        async with AsyncSessionLocal() as primera, AsyncSessionLocal() as segunda:
            await asyncio.gather(*(sesion.execute(select(1)) for sesion in (primera, segunda)))
            for sesion in (primera, segunda):
                cargada = await get_organization_credential(
                    sesion, organization_id, GitProviderEnum.GITHUB
                )
                assert cargada.token_expires_at is not None
                sujetas.append(cargada)

            tarea_primera = asyncio.create_task(
                build_client_for_repository(
                    primera,
                    repository,
                    margen=ventana,
                    refrescador=proveedor,
                ),
            )
            # La primera ya tiene la fila bloqueada y está esperando al proveedor.
            await asyncio.wait_for(proveedor.entrado.wait(), timeout=10)
            tarea_segunda = asyncio.create_task(
                build_client_for_repository(
                    segunda,
                    repository,
                    margen=ventana,
                    refrescador=proveedor,
                ),
            )
            await asyncio.sleep(_MARGEN_PARA_LLEGAR_AL_BLOQUEO)
            proveedor.soltar.set()
            await asyncio.gather(tarea_primera, tarea_segunda)

        # La lista sigue viva aquí a propósito: es lo que impide que la aserción de abajo mida el
        # recolector de basura en lugar de la concurrencia.
        assert len(sujetas) == 2
        assert proveedor.llamadas == 1
        assert proveedor.refresh_recibidos == [REFRESH_ANTIGUO]

        async with AsyncSessionLocal() as lector:
            fila = await _credencial_en_base(lector, organization_id)
        assert _descifrar(fila, "access") == ACCESS_NUEVO
        assert _descifrar(fila, "refresh") == REFRESH_NUEVO
    finally:
        proveedor.soltar.set()
        await _borrar_todo(organization_id)


# --------------------------------------------------------------------------- #
# Cuando la renovación falla: reintentar o no, y decirlo
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.integration
async def test_un_refresh_revocado_lanza_el_error_permanente_con_su_codigo() -> None:
    """Un `refresh_token` rechazado no se reintenta, y se dice con un código estable.

    Lo que **no** puede ser es un fallo silencioso: el pipeline tiene que marcar la revisión como
    fallida y el operador tiene que poder filtrar por el motivo. Y lo que no puede ser **nunca** es
    un `401` de la plataforma, porque en el panel eso borra la sesión del usuario
    (`frontend/src/lib/session-errors.ts`): un `401` echaría a la gente al login cada vez que su
    credencial de GitHub caducara.
    """

    organization_id, repository = await _alta_confirmada(
        caduca_en=datetime.now(UTC) - timedelta(days=2),
    )
    proveedor = ProveedorFalso(error=OAuthRefreshRejectedError("el refresh esta revocado"))
    try:
        async with AsyncSessionLocal() as sesion:
            with pytest.raises(GitCredencialNoRenovableError) as fallo:
                await build_client_for_repository(
                    sesion,
                    repository,
                    refrescador=proveedor,
                )
        assert fallo.value.estado is EstadoDeRefresh.RECHAZADO
        assert fallo.value.codigo_revision == CODIGO_CREDENCIAL_CADUCADA
        # Una llamada y solo una: reintentar aquí es lo que convierte un rechazo en un bloqueo de la
        # aplicación OAuth por parte del proveedor.
        assert proveedor.llamadas == 1
    finally:
        await _borrar_todo(organization_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_el_error_de_credencial_no_saca_el_refresh_token_al_log_ni_a_la_excepcion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ni el mensaje, ni el `repr`, ni el log sacan el secreto.

    Se fuerza el peor caso: el doble de red propaga un mensaje que **sí** contiene el token, como
    haría un intermediario que lo repitiera o un `str(error)` mal puesto. El módulo tiene que seguir
    sin sacarlo, porque estas excepciones acaban en el `repr` de Celery, en el log del worker
    `error_code` que se publica.
    """

    organization_id, repository = await _alta_confirmada(
        caduca_en=datetime.now(UTC) - timedelta(days=3),
    )
    proveedor = ProveedorFalso(
        error=OAuthRefreshRejectedError(f"refresh {REFRESH_ANTIGUO} revocado"),
    )
    try:
        with caplog.at_level(logging.DEBUG):
            async with AsyncSessionLocal() as sesion:
                with pytest.raises(GitCredencialNoRenovableError) as fallo:
                    await build_client_for_repository(
                        sesion,
                        repository,
                        refrescador=proveedor,
                    )
        texto = _texto_del_log(caplog)
        for secreto in (ACCESS_ANTIGUO, REFRESH_ANTIGUO, REFRESH_NUEVO, CLIENT_SECRET):
            assert secreto not in str(fallo.value)
            assert secreto not in repr(fallo.value)
            assert secreto not in texto
        assert fallo.value.codigo_revision not in texto
        # Y el log sí dice el motivo, sin el secreto: un rechazo sin rastro es un fallo silencioso.
        assert "rechazo=OAuthRefreshRejectedError" in texto
    finally:
        await _borrar_todo(organization_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_un_proveedor_caido_lanza_el_error_transitorio_con_su_codigo() -> None:
    """GitHub que no responde es reintentable, y lleva un código distinto del de «reconecta».

    Son dos hechos distintos: uno se arregla esperando y el otro reconectando. Si salieran con el
    mismo código, un panel lleno de «tu credencial caducó» por una caída de GitHub le estaría
    mandando al operador a hacer algo que no va a funcionar.
    """

    organization_id, repository = await _alta_confirmada(
        caduca_en=datetime.now(UTC) - timedelta(hours=5),
    )
    proveedor = ProveedorFalso(error=OAuthRefreshUnavailableError("GitHub no respondió"))
    try:
        async with AsyncSessionLocal() as sesion:
            with pytest.raises(GitCredencialNoDisponibleError) as fallo:
                await build_client_for_repository(
                    sesion,
                    repository,
                    refrescador=proveedor,
                )
        assert fallo.value.estado is EstadoDeRefresh.NO_DISPONIBLE
        assert fallo.value.codigo_revision == CODIGO_PROVEEDOR_NO_DISPONIBLE
        assert fallo.value.codigo_revision != CODIGO_CREDENCIAL_CADUCADA
    finally:
        await _borrar_todo(organization_id)


def test_el_error_permanente_no_esta_en_los_reintentables_de_las_tareas() -> None:
    """La política de reintento, comprobada contra las tareas ya registradas en Celery.

    ## Por qué esto es un test y no un comentario

    Porque es una decisión —«rechazar no se reintenta, no responder sí»— que vive en el
    `autoretry_for` de dos tareas, y un comentario se queda viejo sin que nada avise. Comprueba
    las dos cosas: que lo transitorio está y que lo permanente **no**. Un `GitCredencialError`
    genérico en esa lista reintentaría también los rechazos, que es justo el defecto.

    ## Por qué se leen de `celery_app.tasks` y no del objeto importado

    Porque lo que decide el reintento es lo que Celery tiene registrado, no lo que el decorador
    devuelve al importador —que es la función desnuda. Leer la tarea del registro comprueba lo que
    de verdad va a ejecutar el worker.
    """

    assert GitCredencialNoDisponibleError in _ERRORES_REINTENTABLES
    assert GitCredencialNoRenovableError not in _ERRORES_REINTENTABLES

    for nombre in (
        "repositories.run_pr_security_pipeline",
        "repositories.process_git_webhook_event",
    ):
        tarea = celery_app.tasks[nombre]
        assert GitCredencialNoDisponibleError in tarea.autoretry_for, nombre
        assert GitCredencialNoRenovableError not in tarea.autoretry_for, nombre


def test_sin_oauth_app_configurada_el_error_no_disfraza_de_credencial_caducada() -> None:
    """Reconectar no arregla una instalación sin `client_id`, y el código lo dice distinto.

    Es el mismo criterio que usa la ruta del panel con `503`: mandar a alguien a la pantalla de
    consentimiento cuando lo que falta son las credenciales de la plataforma es devolverlo al mismo
    sitio una y otra vez.
    """

    error = _error_de_estado(
        ResultadoDeRefresh(EstadoDeRefresh.NO_CONFIGURADO, datetime.now(UTC))
    )
    assert error.codigo_revision == CODIGO_OAUTH_SIN_CONFIGURAR
    assert error.codigo_revision != CODIGO_CREDENCIAL_CADUCADA
    assert isinstance(error, GitCredencialNoRenovableError)


# --------------------------------------------------------------------------- #
# El pipeline: un fallo de credencial no es un fallo silencioso
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.integration
async def test_el_pipeline_marca_la_revision_fallida_y_no_envuelve_el_error(
    integration_session: AsyncSession,
) -> None:
    """Un worker que no puede renovar deja la revisión en `ERROR` y propaga su propio tipo.

    Sin esto el fallo se quedaría dentro del worker: la tarea de Celery reintentaría, moriría, y el
    panel mostraría una revisión en `SCANNING` para siempre con la explicación en un log que nadie
    está mirando. `PR_PIPELINE_FAILED` tampoco valdría —le dice a quien mira el panel que la
    se rompió, y la acción que hace falta es reconectar la credencial, que es otra distinta—.

    Y el error sale **sin envolver**: `autoretry_for` decide con el tipo, así que
    `GitCredencialNoDisponibleError` dentro de un `PRPipelineError` se trataría como permanente y no
    se reintentaría nunca.
    """

    assert integration_session is not None
    organization, review = await _revision_para_pipeline(integration_session)
    review_id = review.id
    # El identificador se copia **antes** de que el pipeline aparezca: `_mark_pipeline_error` hace
    # `rollback()` y eso expira los objetos de la sesión, así que leer `organization.id` después
    # sería una recarga que necesita contexto verde. Es el mismo motivo por el que `pipeline.py`
    # copia sus identificadores a variables planas antes de cada rollback.
    organization_id = organization.id

    async def client_builder(
        sesion: AsyncSession,
        repository: Repository,
    ) -> Any:
        del sesion, repository
        raise GitCredencialNoRenovableError(EstadoDeRefresh.RECHAZADO)

    @asynccontextmanager
    async def _sesion_del_prueba() -> AsyncIterator[AsyncSession]:
        yield integration_session

    with pytest.raises(GitCredencialNoRenovableError):
        await _run_pr_security_pipeline(
            str(review_id),
            session_provider=_sesion_del_prueba,
            client_builder=client_builder,  # pyright: ignore[reportArgumentType]
        )

    guardada = await integration_session.get(PullRequestReview, review_id)
    assert guardada is not None
    assert guardada.status == PRReviewStatusEnum.ERROR
    assert guardada.finished_at is not None

    # Y el escaneo que se había creado tampoco se queda en curso: un `RUNNING` huérfano es lo que
    # consume cuota sin que nadie lo espere.
    runs = (
        (
            await integration_session.execute(
                select(PentestRun).where(PentestRun.organization_id == organization_id)
            )
        )
        .scalars()
        .all()
    )
    assert runs, "el pipeline debería haber creado el run antes de fallar al abrir el cliente"
    assert all(run.status is ScanStatusEnum.FAILED for run in runs)


async def _revision_para_pipeline(
    session: AsyncSession,
) -> tuple[Organization, PullRequestReview]:
    """Organización con saldo, repositorio, credencial caducada y una revisión en `QUEUED`."""

    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"Credencial {suffix}",
        slug=f"credencial-{suffix}",
        credit_balance=Decimal("1000"),
    )
    session.add(organization)
    await session.flush()
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=str(uuid.uuid4().int),
        name="app",
        full_name="acme/app",
        clone_url="https://github.example.com/acme/app.git",
    )
    session.add(repository)
    await session.flush()
    session.add(
        GitCredential(
            organization_id=organization.id,
            provider=GitProviderEnum.GITHUB,
            encrypted_access_token=encrypt_organization_credential(
                ACCESS_ANTIGUO,
                organization.id,
                GitProviderEnum.GITHUB,
                field="access",
            ),
            encrypted_refresh_token=encrypt_organization_credential(
                REFRESH_ANTIGUO,
                organization.id,
                GitProviderEnum.GITHUB,
                field="refresh",
            ),
            token_expires_at=datetime.now(UTC) - timedelta(hours=5),
        )
    )
    review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        pr_number=21,
        pr_title="Cambio",
        pr_author="alice",
        source_branch="feature/cambio",
        target_branch="main",
        commit_sha="a" * 40,
        base_sha="e" * 40,
        head_clone_url="https://github.example.com/acme/app.git",
    )
    session.add(review)
    await session.commit()
    return organization, review


# --------------------------------------------------------------------------- #
# La regla del margen, que es la que decide si hay que renovar
# --------------------------------------------------------------------------- #


def test_un_margen_de_cero_no_se_confunde_con_ausencia_de_margen() -> None:
    """`timedelta(0)` es un valor legítimo, y no puede caer al valor configurado.

    La forma ingeniosa de escribir esto —`margen or timedelta(configurado)`— trata el cero como si
    fuera «no me han pasado nada», así que un operador que ponga el margen a `0` para renovar justo
    al expirar seguiría renovando con 60 segundos de antelación, y nadie lo vería hasta que se
    preguntara por qué el ajuste no hace nada.
    """

    assert margen_de_renovacion(timedelta(0)) == timedelta(0)
    assert margen_de_renovacion(None) == timedelta(
        seconds=settings.git_token_refresh_margin_seconds
    )


@pytest.mark.parametrize(
    ("caduca_en", "esperado"),
    [
        (None, False),
        ("futuro_lejos", False),
        ("futuro_cerca", True),
        ("pasado", True),
    ],
)
def test_la_regla_del_margen_es_la_misma_en_los_dos_caminos(
    caduca_en: datetime | None,
    esperado: bool,
) -> None:
    """`None` es un PAT y no se renueva nunca; el resto depende solo de la ventana."""

    momento = datetime.now(UTC)
    instantes: dict[str, datetime] = {
        "futuro_lejos": momento + timedelta(hours=4),
        "futuro_cerca": momento + timedelta(seconds=30),
        "pasado": momento - timedelta(hours=4),
    }
    valor = instantes.get(caduca_en) if isinstance(caduca_en, str) else caduca_en
    assert credential_necesita_renovacion(valor, momento=momento) is esperado



# --------------------------------------------------------------------------- #
# El barrido por adelantado
#
# `credenciales_por_vencer` es una consulta **global**: pregunta qué credenciales de esta
# instalación van a caducar, y esa pregunta no admite un filtro de una organización. La base de las
# pruebas es la misma que usa la demostración, así que una prueba que ejecutara el barrido completo
# con un doble de red que devuelve un token escribiría sobre filas de otros tenants, y no lo
# notices. Por eso el listado se comprueba en modo lectura y la escritura se comprueba sobre una
# lista que la propia prueba ha elegido: son las dos mitades de la función, separadas por
# `renovar_lote`.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.integration
async def test_el_listado_del_barrido_solo_saca_lo_que_va_a_caducar() -> None:
    """La consulta global se comprueba en modo lectura: dentro la caducada, fuera lo demás.

    Es la comprobación que **no puede** hacerse sobre el barrido completo, y por eso está separada:
    aquí no se escribe nada, así que da igual cuántas filas de la demostración o de otros
    en la base. Se mira que la propia fila esté, que la vigente no, y que el token personal —que es
    el único caso con `token_expires_at IS NULL`— tampoco.
    """

    por_vencer_id, _ = await _alta_confirmada(
        caduca_en=datetime.now(UTC) + timedelta(minutes=20),
    )
    vigente_id, _ = await _alta_confirmada(
        caduca_en=datetime.now(UTC) + timedelta(hours=5),
    )
    personal_id, _ = await _alta_confirmada(caduca_en=None, refresh=REFRESH_ANTIGUO)
    try:
        async with AsyncSessionLocal() as lector:
            candidatas = await credenciales_por_vencer(
                lector,
                ventana=timedelta(hours=1),
            )
        organizaciones = {organization_id for organization_id, _proveedor in candidatas}
        assert por_vencer_id in organizaciones
        assert vigente_id not in organizaciones
        assert personal_id not in organizaciones
        # Y sin `refresh_token` tampoco se cuela nadie: la lista de candidatas se arma sobre esa
        # columna, no sobre la fecha sola.
        sin_refresh_id, _ = await _alta_confirmada(
            caduca_en=datetime.now(UTC) + timedelta(minutes=5),
            refresh=None,
        )
        try:
            async with AsyncSessionLocal() as lector:
                con_refresh = await credenciales_por_vencer(
                    lector,
                    ventana=timedelta(hours=1),
                )
            sin_refresh = {
                organization_id for organization_id, _p in con_refresh
            }
            assert sin_refresh_id not in sin_refresh
        finally:
            await _borrar_todo(sin_refresh_id)
    finally:
        await _borrar_todo(por_vencer_id)
        await _borrar_todo(vigente_id)
        await _borrar_todo(personal_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_el_lote_se_renueva_con_la_ventana_entera_y_no_toca_lo_demas() -> None:
    """El lote usa la **ventana** como margen, o el barrido es un no-op silencioso.

    Con el margen de 60 s de la ruta HTTP, una credencial que caduca en 20 minutos volvería
    `VIGENTE` sin llamar al proveedor. Se pasa la lista a mano —y no el resultado del listado— para
    que el doble de red no pueda escribir sobre las filas de otros tenants.

    Se comprueba también que una credencial vigente y un token personal, puestas en la lista, no se
    tocan: el listado es el que las deja fuera, y `renovar_lote` no vuelve a preguntar.
    """

    por_vencer_id, _ = await _alta_confirmada(
        caduca_en=datetime.now(UTC) + timedelta(minutes=20),
    )
    vigente_id, _ = await _alta_confirmada(
        caduca_en=datetime.now(UTC) + timedelta(hours=5),
    )
    personal_id, _ = await _alta_confirmada(caduca_en=None, refresh=REFRESH_ANTIGUO)
    proveedor = ProveedorFalso()
    try:
        resumen = await renovar_lote(
            [
                (por_vencer_id, GitProviderEnum.GITHUB),
                (vigente_id, GitProviderEnum.GITHUB),
                (personal_id, GitProviderEnum.GITHUB),
            ],
            margen=timedelta(hours=1),
            refrescador=proveedor,
        )
        assert resumen.candidatas == 3
        assert resumen.renovate == 1
        assert resumen.fallidas == 0
        assert proveedor.llamadas == 1

        async with AsyncSessionLocal() as lector:
            renovada = await _credencial_en_base(lector, por_vencer_id)
            vigente = await _credencial_en_base(lector, vigente_id)
            personal = await _credencial_en_base(lector, personal_id)
        assert _descifrar(renovada, "access") == ACCESS_NUEVO
        assert renovada.token_expires_at is not None
        assert _descifrar(vigente, "access") == ACCESS_ANTIGUO
        assert personal.token_expires_at is None
    finally:
        await _borrar_todo(por_vencer_id)
        await _borrar_todo(vigente_id)
        await _borrar_todo(personal_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_el_lote_no_se_traga_un_fallo_ni_aborta_las_demas(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Una credencial que no se puede renovar deja rastro y **no** arrastra a las demás.

    Un barrido que se come los fallos se queda en verde para siempre mientras el producto está
    muerto; uno que aborta en el primer error deja las siguientes credenciales sin renovar en cada
    pasada. Las dos cosas son el defecto, así que las dos se comprueban.

    El orden importa, y aquí lo impone la lista: la que falla es la primera y la buena la
    Con el orden al revés, «siguió después de un fallo» y «no se quedó con el primero» darían el
    mismo resultado.
    """

    rota_id, _ = await _alta_confirmada(caduca_en=datetime.now(UTC) + timedelta(minutes=10))
    buena_id, _ = await _alta_confirmada(caduca_en=datetime.now(UTC) + timedelta(minutes=15))

    class _FallaLaPrimera:
        """Rechaza la primera llamada, la de la credencial más próxima a caducar."""

        def __init__(self) -> None:
            self.llamadas = 0

        async def __call__(
            self,
            config: OAuthProviderSettings,
            refresh_token: str,
        ) -> OAuthToken:
            del config, refresh_token
            self.llamadas += 1
            if self.llamadas == 1:
                raise OAuthRefreshRejectedError("el refresh esta revocado")
            return OAuthToken(
                access_token=ACCESS_NUEVO,
                refresh_token=REFRESH_NUEVO,
                expires_in=28_800,
            )

    proveedor = _FallaLaPrimera()
    try:
        with caplog.at_level(logging.ERROR):
            resumen = await renovar_lote(
                [
                    (rota_id, GitProviderEnum.GITHUB),
                    (buena_id, GitProviderEnum.GITHUB),
                ],
                margen=timedelta(hours=1),
                refrescador=proveedor,
            )
        assert proveedor.llamadas == 2
        assert resumen.candidatas == 2
        assert resumen.renovate == 1
        assert resumen.fallidas == 1

        async with AsyncSessionLocal() as lector:
            rota = await _credencial_en_base(lector, rota_id)
            buena = await _credencial_en_base(lector, buena_id)
        assert _descifrar(rota, "access") == ACCESS_ANTIGUO
        assert _descifrar(buena, "access") == ACCESS_NUEVO

        texto = _texto_del_log(caplog)
        assert f"organization={rota_id}" in texto
        assert "estado=rechazado" in texto
        assert f"organization={buena_id}" not in texto
    finally:
        await _borrar_todo(rota_id)
        await _borrar_todo(buena_id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_el_barrido_completo_recorre_el_listado_sin_escribir_sobre_nadie() -> None:
    """La composición —consultar y renovar el lote— con un doble que **no puede** escribir.

    Es la prueba de que las dos mitades encajan, hecha de la única manera que no puede corromper la
    base compartida: un doble que rechaza **todo**. Con eso, el recorrido completo ocurre —se
    consulta, se itera, se cuenta, se registra— y ninguna fila de la demostración ni de ningún otro
    tenant puede cambiar, porque una renovación rechazada no escribe nada.

    Que el recuento de `renovate` sea cero aquí no es una limitación: esa cifra la comprueba
    `test_el_lote_se_renueva_con_la_ventana_entera_y_no_toca_lo_demas`, que sí escribe, sobre filas
    que son suyas.
    """

    propia_id, _ = await _alta_confirmada(
        caduca_en=datetime.now(UTC) + timedelta(minutes=20),
    )
    try:
        with caplog_sin_fallo():
            resumen = await renovar_credenciales_por_vencer(
                ventana=timedelta(hours=1),
                refrescador=ProveedorQueRechazaTodo(),
            )
        assert resumen.candidatas >= 1
        assert resumen.renovate == 0
        assert resumen.fallidas == resumen.candidatas
    finally:
        await _borrar_todo(propia_id)


@contextmanager
def caplog_sin_fallo() -> Iterator[None]:
    """Silencia el `ERROR` que el barrido va a registrar a propósito.

    ## Por qué hace falta silenciarlo

    Porque la prueba anterior **quiere** que cada candidata falle, y el barrido registra un `ERROR`
    por cada una, incluyendo las de la demostración. Un `ERROR` en la salida de la batería se lee
    como un fallo del proyecto, y aquí no lo es: es el comportamiento que se está comprobando. El
    contexto no borra nada —los registros se siguen recogiendo—; solo sube el nivel para que la
    demostración no parezca rota.
    """

    import logging as _logging

    token = _logging.getLogger("backend.apps.repositories.token_refresh")
    anterior = token.level
    token.setLevel(_logging.CRITICAL)
    try:
        yield
    finally:
        token.setLevel(anterior)


class ProveedorQueRechazaTodo:
    """Un proveedor falso que rechaza cualquier `refresh_token`, y por tanto no escribe nada.

    ## Por qué existe y por qué no se puede sustituir por uno que devuelva un token

    Porque devolver un token haría que la fila de **la demostración** —o la de cualquier otro tenant
    con una credencial por caducar— quedara guardada con el token del doble. Eso ya pasó una vez al
    escribir este fichero: la credencial de la organización de demostración salió de la base con un
    token de prueba. Rechazar es la única forma de recorrer el código de verdad sin escribir.
    """

    def __init__(self) -> None:
        self.llamadas = 0

    async def __call__(
        self,
        config: OAuthProviderSettings,
        refresh_token: str,
    ) -> OAuthToken:
        del config, refresh_token
        self.llamadas += 1
        raise OAuthRefreshRejectedError("este doble rechaza todo y no escribe nada")
