"""Renovación del token de acceso de una credencial OAuth antes de que caduque.

## El fallo que este módulo existe para no dejar volver

Un token `gho_` de GitHub dura `expires_in: 28800`, ocho horas. `git_credentials` guardaba el
`refresh_token` cifrado desde la fase 3 y **no lo usaba en ningún sitio**: la fila se escribía en
el callback de OAuth y nunca se volvía a leer. El efecto era que toda conexión OAuth del producto
caducaba a las ocho horas y no se recuperaba sola. Un cliente conecta su GitHub el lunes y el
martes ya no tiene integración, sin explicación y sin que nadie pueda arreglarlo desde el panel.

## Por qué el bloqueo es de fila y no un `Lock` de Python

Porque la unidad de ejecución no es el proceso. Uvicorn arranca varios workers, los workers de
Celery son procesos aparte, y el despliegue es un contenedor que se puede replicar. Un
`threading.Lock` o un `asyncio.Lock` solo serializan dentro de **un** proceso: con dos workers, dos
peticiones simultáneas siguen llamando al proveedor a la vez, cada una con el mismo
`refresh_token`. Y eso no es un fallo teórico —GitLab rota el `refresh_token` en cada refresco y el
anterior queda invalidado—, es exactamente el «a veces funciona y a veces no».

El bloqueo tiene que vivir en algo que compartan todos los procesos, y ese algo ya está en el
proyecto: PostgreSQL. `SELECT ... FOR UPDATE` sobre la fila de `git_credentials` es el mismo
mecanismo que usa `billing/service.py` para que dos pentests no gasten el mismo saldo, y
`webhook_events.py` para que dos entregas del mismo evento no se procesen dos veces. Además
`FOR UPDATE` tiene una propiedad que un mutex no tiene y que este caso necesita: **en READ
COMMITTED, el que espera vuelve a leer la fila ya actualizada por el que la tenía bloqueada**. El
segundo refresco ve el `token_expires_at` que el primero acaba de escribir y no llama al proveedor.

## Por qué la primera lectura va sin bloqueo

Porque el caso normal es que el token siga vivo, y en ese caso no hay nada que serializar. Tomar
el bloqueo de fila en cada petición de inventario dejaría todas las peticiones de una organización
encoladas detrás de la misma fila, y la última esperaría a la llamada a GitHub de la primera. Es el
patrón de doble comprobación: se lee sin bloquear para descartar el caso frecuente, y solo se
bloquea cuando de verdad puede haber un refresco.

## Por qué `populate_existing=True` no es opcional

Porque SQLAlchemy no sobrescribe los atributos de una instancia que ya está en el mapa de
identidad. La fila puede llevar ya un rato en la sesión —cualquier cosa que haya leído la credencial
antes la dejó ahí— y entonces la lectura bloqueante devolvería **la misma instancia con los valores
viejos**: el segundo concurrente creería que el token sigue caducado y refrescaría con el
`refresh_token` que el primero acaba de rotar. `supply_chain/service.py` documenta el mismo motivo
para el mismo parámetro.

## Por qué esto es especialmente traicionero aquí

Porque el mapa de identidad de SQLAlchemy guarda **referencias débiles**. Si la sesión no tiene
nadie sujetando la fila, el primer `gc` la saca del mapa y la lectura siguiente la trae fresca de la
base: el parámetro no hace falta. En cuanto alguien la sujeta —la ruta que lo llama, una vista que
la consultó antes, cualquier cosa— la fila se queda con su valor viejo y el parámetro sí hace
falta. Es decir: el defecto aparece y desaparece según cuándo pase el recolector, que es la peor
forma posible de un defecto. Por eso se pone siempre y no «cuando haga falta».

## Por qué `refrescador` es inyectable

Porque un refresco que solo se puede probar contra la red real no se puede probar: la batería del
proyecto no habla con GitHub ni con GitLab. Inyectar la función **no** cambia lo que se hace en
producción —el valor por defecto es `refresh_oauth_token`— y permite comprobar el caso de
concurrencia con dos conexiones reales de PostgreSQL y un proveedor falso que cuenta llamadas.

## Por qué esta función **confirma** la transacción de la sesión del llamador

Porque el bloqueo de fila se mantiene hasta que termina la transacción, y sin cerrarla el
`FOR UPDATE` seguiría sujetando la fila durante la llamada al proveedor y durante el resto de la
petición. Se confirma al salir de la fase bloqueada, tanto si se ha escrito algo como si no,
porque `expire_on_commit=False` deja el mapa de identidad intacto y un `commit()` sin cambios no
persiste nada.

Eso convierte en **precondición** que el llamador no tenga nada pendiente de escribir cuando llama
aquí. Un `rollback()` sería la alternativa, y sería peor: `rollback()` expira todos los objetos de
la sesión, y `connect_repository` lee `repository.webhook_id` **después** de `_open_client`; un
atributo expirado fuera de un greenlet lanza `MissingGreenlet`.

## Por qué esa precondición no se pide a los workers

Porque en el worker **no se llama a esta función directamente**.
`asegurar_credentialo_vigente_en_sesion_ajena` abre una sesión que es suya, la renueva y la cierra.
La precondición se cumple por construcción en vez de por convención.

Y la convención es justo lo que no se puede aceptar. «El llamador no tiene nada pendiente» depende
del orden de unas líneas que se mueven cada vez que se toca un worker, y un `commit()` inesperado
en mitad de la inserción de hallazgos —o del asiento en el `credit_ledger`, que R4 vuelve
append-only— persiste media unidad de trabajo sin que nadie lo haya decidido. Que hoy tres de los
cuatro llamadores de `build_client_for_repository` la cumplan no la convierte en invariante: la
convierte en una portada.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.repositories.credentials import (
    GitCredentialNotFoundError,
    encrypt_organization_credential,
)
from backend.apps.repositories.models import GitCredential, GitProviderEnum
from backend.apps.repositories.oauth import (
    OAuthConfigurationError,
    OAuthProviderSettings,
    OAuthRefreshError,
    OAuthRefreshUnavailableError,
    OAuthToken,
    get_oauth_provider_settings,
    refresh_oauth_token,
)
from backend.core.config import settings
from backend.core.crypto import CryptoError, decrypt_secret
from backend.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

#: Firma del paso de red, sustituible en las pruebas. El valor por defecto es el de producción.
RefrescadorOAuth = Callable[[OAuthProviderSettings, str], Awaitable[OAuthToken]]


class EstadoDeRefresh(StrEnum):
    """Qué ha pasado al intentar dejar la credencial en vigor."""

    #: No había nada que hacer: no tiene caducidad, o le queda más margen del configurado.
    VIGENTE = "vigente"
    #: Se ha renovado el token de acceso y se ha guardado con su nueva fecha.
    REFRESCADO = "refrescado"
    #: La credencial caducó y no hay `refresh_token` con el que renovarla.
    SIN_RENOVACION = "sin_refresh_token"
    #: El proveedor rechazó el `refresh_token`. Solo queda volver a conectar.
    RECHAZADO = "rechazado"
    #: Esta instalación no tiene `client_id`/`client_secret` del proveedor. Reconectar no arregla
    #: nada, y decirlo como si arreglara sería mandar al usuario a repetir un alta que va a fallar.
    NO_CONFIGURADO = "no_configurado"
    #: El proveedor no respondió. Es transitorio y la respuesta es `502`, no «reconecta».
    NO_DISPONIBLE = "no_disponible"


@dataclass(frozen=True, slots=True)
class ResultadoDeRefresh:
    """El veredicto y la fecha que lo explica, para que el mensaje pueda citarla."""

    estado: EstadoDeRefresh
    caduca_en: datetime | None = None


#: Estados en los que la credencial queda servible para hablar con el proveedor.
ESTADOS_QUE_SIRVEN: frozenset[EstadoDeRefresh] = frozenset(
    {EstadoDeRefresh.VIGENTE, EstadoDeRefresh.REFRESCADO}
)


# --------------------------------------------------------------------------- #
# Los errores que un worker tiene que poder distinguir
# --------------------------------------------------------------------------- #

#: `error_code` que viaja en el evento `pr_review.failed` y que el panel puede filtrar.
#:
#: Es un código y no un texto porque el texto hay que leerlo uno a uno y el código permite ver «esta
#: organización lleva veinte revisiones rechazadas por credencial caducada» de un vistazo, que es la
#: pregunta que se hace alguien con un panel lleno de errores. Es el mismo criterio que ya usa
#: `INSUFFICIENT_CREDITS` en `pipeline.py`.
CODIGO_CREDENCIAL_CADUCADA = "GIT_CREDENTIAL_RECONNECT_REQUIRED"
CODIGO_OAUTH_SIN_CONFIGURAR = "GIT_OAUTH_NOT_CONFIGURED"
CODIGO_PROVEEDOR_NO_DISPONIBLE = "GIT_CREDENTIAL_PROVIDER_UNAVAILABLE"

_CODIGO_POR_ESTADO: dict[EstadoDeRefresh, str] = {
    EstadoDeRefresh.RECHAZADO: CODIGO_CREDENCIAL_CADUCADA,
    EstadoDeRefresh.SIN_RENOVACION: CODIGO_CREDENCIAL_CADUCADA,
    EstadoDeRefresh.NO_CONFIGURADO: CODIGO_OAUTH_SIN_CONFIGURAR,
    EstadoDeRefresh.NO_DISPONIBLE: CODIGO_PROVEEDOR_NO_DISPONIBLE,
}

#: Por qué reintentar y por qué no, en una frase por estado. Sin esta tabla el mensaje sería un
#: literal por clase de error y el día que se añadiese un estado habría que acordarse de añadirlo.
_MOTIVO_POR_ESTADO: dict[EstadoDeRefresh, str] = {
    EstadoDeRefresh.RECHAZADO: "el proveedor rechazó el refresh token guardado",
    EstadoDeRefresh.SIN_RENOVACION: (
        "la credencial caducó y no tiene refresh token con el que renovarla"
    ),
    EstadoDeRefresh.NO_CONFIGURADO: (
        "esta instalación no tiene configurada la OAuth App del proveedor"
    ),
    EstadoDeRefresh.NO_DISPONIBLE: "el proveedor no respondió al intento de refresco",
}


class GitCredencialError(RuntimeError):
    """La credencial del tenant no está en vigor y no se ha podido dejar en vigor.

    ## Por qué una excepción y no un código HTTP

    Porque estos errores no ocurren en una petición: ocurren en un worker, dentro de un pipeline o
    de una tarea de Celery, y no hay respuesta que traducir. El panel nunca los ve; los ve quien
    lee el estado de la revisión y el registro del worker, y para eso necesitan **distinguir el
    caso transitorio del permanente** —uno se reintenta con backoff y el otro no, porque
    reintentar un `refresh_token` revocado es lo que hace que un proveedor bloquee la aplicación
    OAuth— y necesitan decir cuál de los dos es sin traducir a ojo.

    ## Por qué el mensaje no lleva nada del proveedor

    Porque en este marco el `refresh_token` está descifrado, y porque estas excepciones acaban en el
    `repr` de Celery, en el log del worker y en el `error_code` que se publica. El texto es de la
    tabla de arriba, escrito aquí, y no el `str(error)` de nadie.
    """

    def __init__(self, estado: EstadoDeRefresh, caduca_en: datetime | None = None) -> None:
        super().__init__(
            f"La credencial de Git no está en vigor: {_MOTIVO_POR_ESTADO[estado]} "
            f"(estado={estado.value})"
        )
        self.estado = estado
        self.caduca_en = caduca_en

    @property
    def codigo_revision(self) -> str:
        """El `error_code` estable que viaja en el evento de revisión fallida."""

        return _CODIGO_POR_ESTADO[self.estado]


class GitCredencialNoRenovableError(GitCredencialError):
    """No hay nada que reintentar: reconectar es la única salida.

    ## Por qué reintentar esto sería un defecto y no una prudencia

    Porque el `refresh_token` está revocado o no existe, y el estado no va a cambiar por insistir.
    Cada reintento es una petición más a la API de tokens con las credenciales de la plataforma
    incluida, y un proveedor que ve eso puede bloquear la OAuth App para todos los tenants. Es el
    mismo motivo por el que
    `test_el_refresh_token_rechazado_responde_410_pidiendo_reconectar` comprueba que en la ruta
    del panel se llama al proveedor **una vez y solo una**.
    """


class GitCredencialNoDisponibleError(GitCredencialError):
    """El proveedor no respondió. Es transitorio y el reintento con backoff es lo correcto.

    ## Por qué esto no es el mismo error que el de «no disponible» de la API de Git

    Porque es un fallo de la **ida a renovar**, no del trabajo que se iba a hacer. `GitServerError`
    ya cubre el `5xx` del API de Git; este cubre el `5xx` o el tiempo agotado del endpoint de token.
    Con los dos en `autoretry_for`, un GitHub degradado reintenta las dos cosas por separado y cada
    una con su propio motivo en el log.
    """


def _error_de_estado(
    resultado: ResultadoDeRefresh,
) -> GitCredencialError:
    """El error tipado que corresponde a un veredicto que no deja la credencial servible."""

    clase: type[GitCredencialError] = (
        GitCredencialNoDisponibleError
        if resultado.estado is EstadoDeRefresh.NO_DISPONIBLE
        else GitCredencialNoRenovableError
    )
    return clase(resultado.estado, resultado.caduca_en)


def margen_de_renovacion(margen: timedelta | None = None) -> timedelta:
    """El margen que se aplica, venga del llamador o de la configuración.

    ## Por qué una función y no `margen or timedelta(...)`

    Porque `timedelta(0)` —que es un valor legítimo: renovar justo al expirar— es *falsy*, y
    `margen or configurado` lo descartaría en silencio por el camino del panel. Un valor que el
    operador puede poner a propósito no puede ser indistinguible de «no me han pasado nada».
    """

    if margen is not None:
        return margen
    return timedelta(seconds=settings.git_token_refresh_margin_seconds)


def credential_necesita_renovacion(
    caduca_en: datetime | None,
    *,
    momento: datetime | None = None,
    margen: timedelta | None = None,
) -> bool:
    """¿Está esta credencial dentro —o más allá— de la ventana en la que hay que renovarla?

    ## Por qué existe, y por qué devuelve un booleano y no el resultado

    Porque la ventana es la **misma regla** en dos sitios, y si cada uno la escribiera habría dos
    respuestas a «¿esto está para renovar?» que pueden divergir sin que ninguna prueba se entere.
    Quien llama a esta función no quiere renovar todavía: quiere saber si tiene que preguntar. La
    decisión la sigue tomando `asegurar_credentialo_vigente`, que es quien la va a cumplir.

    ## Por qué se puede leer una copia que no sea la de la base

    Porque equivocarse **solo puede costar una vuelta de más**, nunca una de menos: si la copia en
    memoria dice que hay que renovar y la base ya estaba renovada, `asegurar_credentialo_vigente`
    lee la fila —desde la base— antes de decidir y no llama al proveedor. Lo que no puede pasar es
    lo contrario, y por eso esta función solo se usa para descartar trabajo.

    ## Por qué `None` es «no»

    Porque `token_expires_at IS NULL` es lo que deja `connect_personal_token`: un token personal de
    acceso no lleva fecha de caducidad conocida. No hay nada que renovar, y no se intenta — un PAT
    guardado junto al `refresh_token` de una conexión OAuth anterior es una combinación real, y
    «renovarlo» cambiaría el token del usuario por un token OAuth sin avisar.
    """

    if caduca_en is None:
        return False
    return caduca_en <= (momento or datetime.now(UTC)) + margen_de_renovacion(margen)


async def asegurar_credentialo_vigente(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
    *,
    ahora: datetime | None = None,
    margen: timedelta | None = None,
    config: OAuthProviderSettings | None = None,
    refrescador: RefrescadorOAuth | None = None,
) -> ResultadoDeRefresh:
    """Deja la credencial del tenant con un token de acceso en vigor, o explica por qué no pudo.

    **Precondición:** la sesión del llamador no tiene nada pendiente de escribir. La función
    confirma la transacción al terminar la fase bloqueada, y un `commit()` con cambios a medias
    persistiría media petición.

    ## Por qué `token_expires_at IS NULL` no entra nunca por aquí

    Porque en esta tabla `NULL` es lo que deja `connect_personal_token`: un token personal de acceso
    no lleva fecha de caducidad conocida. No hay nada que renovar, y **no se intenta**: un PAT
    guardado junto a un `refresh_token` de una conexión OAuth anterior es una combinación real
    —`connect_personal_token` sustituye el token de acceso y pone la fecha a `NULL` sin borrar el
    `refresh_token`—, y si se intentara refrescar se cambiaría el PAT del usuario por un token OAuth
    sin avisar.

    ## Por qué el margen configurable existe

    Porque `token_expires_at` es un instante y una petición dura un rato. Renovar justo al expirar
    significa entregar al cliente GitHub un token que se caduca a mitad de la llamada, y el síntoma
    es un `401` del proveedor en una operación que hace un minuto funcionaba. El margen viene de la
    configuración —R1— y no de un literal aquí.

    ## Por qué el filtro es por `organization_id` primero

    Por R3, y por el mismo motivo que en el resto del módulo: la credencial que hay que renovar es
    la de esta organización, y `ix_git_credentials_org_provider` es único sobre esas dos columnas.
    """
    momento = ahora or datetime.now(UTC)
    limite = momento + margen_de_renovacion(margen)

    # Primera lectura, sin bloqueo: descarta el caso frecuente y no sujeta la fila.
    prelectura = await session.execute(
        select(
            GitCredential.token_expires_at,
            GitCredential.encrypted_refresh_token.is_not(None),
        ).where(
            GitCredential.organization_id == organization_id,
            GitCredential.provider == provider,
        )
    )
    fila = prelectura.one_or_none()
    if fila is None:
        raise GitCredentialNotFoundError(
            "La organización no tiene una credencial para el proveedor"
        )
    caduca = fila[0]
    if caduca is None or caduca > limite:
        return ResultadoDeRefresh(EstadoDeRefresh.VIGENTE, caduca)
    if not fila[1]:
        # Caducada y sin `refresh_token`: no hay nada que hacer ni a quién esperar. Ni se bloquea
        # ni se llama al proveedor; la respuesta es «vuelve a conectar».
        return ResultadoDeRefresh(EstadoDeRefresh.SIN_RENOVACION, caduca)

    # Segunda lectura, bloqueando la fila. Aquí se serializan los refrescos concurrentes.
    bloqueada = await session.execute(
        select(GitCredential)
        .where(
            GitCredential.organization_id == organization_id,
            GitCredential.provider == provider,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    credential = bloqueada.scalar_one_or_none()
    if credential is None:
        # La fila no puede desaparecer en medio de su propia petición: la borra un
        # `ON DELETE CASCADE` de la organización. Se trata como ausencia, y el `409` lo levanta
        # `_open_client` cuando no consigue construir el cliente.
        await session.commit()
        raise GitCredentialNotFoundError(
            "La organización no tiene una credencial para el proveedor"
        )

    # Bajo el bloqueo se decide de nuevo: si otro proceso ya renovó mientras se esperaba, su
    # `token_expires_at` es el que se lee aquí y no hay nada que renovar.
    caduca = credential.token_expires_at
    if caduca is not None and caduca > limite:
        await session.commit()
        return ResultadoDeRefresh(EstadoDeRefresh.VIGENTE, caduca)
    if credential.encrypted_refresh_token is None:
        await session.commit()
        return ResultadoDeRefresh(EstadoDeRefresh.SIN_RENOVACION, caduca)

    resultado = await _refrescar_bajo_bloqueo(
        credential,
        config if config is not None else get_oauth_provider_settings(provider),
        refrescador if refrescador is not None else refresh_oauth_token,
        momento,
    )
    # Se cierra la transacción pase lo que pase. En el camino de éxito es lo que persiste el token
    # nuevo y lo que suelta la fila; en los demás es lo que suelta la fila sin escribir nada.
    await session.commit()
    return resultado


# --------------------------------------------------------------------------- #
# La entrada del worker: sesión propia, y por tanto `commit()` propio
# --------------------------------------------------------------------------- #


#: Fábrica de la sesión ajena, sustituible en las pruebas.
#:
#: Es un parámetro y no un `patch` global porque el módulo se importa en procesos donde el
#: parcheo global no existe —un worker de Celery— y porque una prueba que sustituye una cosa
#: concreta se puede volver a sustituir por otra sin dejar residuos.
SesionAjena = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@asynccontextmanager
async def _sesion_propia() -> AsyncIterator[AsyncSession]:
    """Una sesión del pool del módulo, abierta solo para una renovación.

    ## Por qué el pool del módulo y no uno nuevo por llamada

    Porque `_default_session_provider` —el que usan los workers— crea un `AsyncEngine` nuevo en cada
    tarea y lo destruye al terminarla. Abrir otro aquí significaría crear y destruir un tercer
    motor dentro de la misma tarea, y la conexión por Tailscale de este entorno cuesta del orden de
    150 ms.
    El pool de `backend.core.database` ya existe en el proceso, con `pool_pre_ping` y reciclado, y
    esto no es una petición de usuario a la que haya que aíslar: es una sesión de corta vida que
    solo renueva una fila.
    """

    async with AsyncSessionLocal() as sesion:
        yield sesion


async def asegurar_credentialo_vigente_en_sesion_ajena(
    organization_id: UUID,
    provider: GitProviderEnum,
    *,
    sesion_ajena: SesionAjena = _sesion_propia,
    margen: timedelta | None = None,
    config: OAuthProviderSettings | None = None,
    refrescador: RefrescadorOAuth | None = None,
) -> ResultadoDeRefresh:
    """Renueva en una sesión que **no** es la del llamador, o lanza el error tipado del motivo.

    ## Por qué la sesión es otra y no la del llamador

    Porque `asegurar_credentialo_vigente` confirma la transacción al terminar la fase bloqueada, y
    en un worker esa transacción no es del worker: es la que sostiene el `FOR UPDATE` de la revisión
    —en `pipeline.py`, `with_for_update()` sobre `pull_request_reviews` y `repositories`—, o el
    `savepoint` de los hallazgos que se están insertando. Un `commit()` en medio de eso persiste
    media unidad de trabajo: hallazgos cobrados y cobrados dos veces, o una revisión en `SCANNING`
    con la mitad de los hallazgos ya guardados. No es un riesgo teórico, es la razón por la que
    `_claim_review` y `_persist_findings` son tan deliberados con sus commits.

    Y hay un segundo motivo, más importante que el de la atomicidad: **la renovación no es parte de
    la unidad de trabajo del worker**. Si el pipeline falla después de renovar, el token nuevo tiene
    que seguir guardado. Con la sesión del worker, un `rollback()` por el fallo del escaneo
    deshacía la renovación y devolvía al sistema al token caducado, y el siguiente worker volvía a
    intentar renovarlo con un `refresh_token` que GitLab ya había rotado.

    ## Por qué sale una excepción y no un `ResultadoDeRefresh`

    Porque quien llama aquí es un worker y **no hay a quién responderle**. Un veredicto que nadie
    mira es un fallo silencioso, que es la clase de defecto más cara que tiene este proyecto. Al
    salir como excepción, el `autoretry_for` de la tarea decide —reintentar o no— según el tipo, y
    `pipeline.py` marca la revisión como fallida con un `error_code` estable antes de propagarla.
    La ruta del panel sigue usando `asegurar_credentialo_vigente` directamente, donde el veredicto
    sí se traduce a un código HTTP.
    """

    async with sesion_ajena() as sesion:
        resultado = await asegurar_credentialo_vigente(
            sesion,
            organization_id,
            provider,
            margen=margen,
            config=config,
            refrescador=refrescador,
        )
    if resultado.estado in ESTADOS_QUE_SIRVEN:
        return resultado
    raise _error_de_estado(resultado)


# --------------------------------------------------------------------------- #
# El barrido por adelantado
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ResultadoDelBarrido:
    """Cuántas se renovaron, cuántas ya estaban vivas y cuántas no se pudieron dejar vivas."""

    renovate: int
    fallidas: int
    sin_refresh_token: int
    candidatas: int


async def credenciales_por_vencer(
    session: AsyncSession,
    *,
    ventana: timedelta | None = None,
    momento: datetime | None = None,
    limite: int | None = None,
) -> list[tuple[UUID, GitProviderEnum]]:
    """Las credenciales OAuth que hay que renovar antes de que caduquen.

    ## Por qué el filtro excluye las que no tienen `refresh_token`

    Porque una credencial sin `refresh_token` no se puede renovar por definición, y meterla en la
    lista solo conseguiría que cada barrido produjese el mismo `SIN_RENOVACION` y el mismo log de
    error para siempre. Si esa credencial está caducada, el que tiene que enterarse es el operador
    por el panel —que es donde `410` lo dice—, no un `beat` que no puede hacer nada.

    Y `token_expires_at IS NOT NULL` deja fuera los tokens personales de acceso, que es lo que esta
    columna significa cuando está a `NULL`.

    ## Por qué el `LIMIT`

    Porque el barrido corre en un `beat` que comparte worker con los escaneos, y una lista sin tope
    con muchos tenants se lleva el tiempo y el ancho de banda del proceso entero. Con el tope, un
    barrido que no llega a todo es un barrido que se repite en el siguiente ciclo: no se pierde
    trabajo, y el ordering por `token_expires_at` hace que el que más urge sea el primero.
    """

    ahora = momento or datetime.now(UTC)
    ventana_real = (
        ventana
        if ventana is not None
        else timedelta(seconds=settings.git_token_proactive_refresh_window_seconds)
    )
    tope = (
        limite
        if limite is not None
        else settings.git_token_proactive_refresh_batch_size
    )
    resultado = await session.execute(
        select(GitCredential.organization_id, GitCredential.provider)
        .where(
            GitCredential.token_expires_at.is_not(None),
            GitCredential.encrypted_refresh_token.is_not(None),
            GitCredential.token_expires_at <= ahora + ventana_real,
        )
        .order_by(GitCredential.token_expires_at)
        .limit(tope)
    )
    return [(fila[0], fila[1]) for fila in resultado.all()]


async def renovar_credenciales_por_vencer(
    *,
    sesion_ajena: SesionAjena = _sesion_propia,
    ventana: timedelta | None = None,
    margen: timedelta | None = None,
    ahora: datetime | None = None,
    limite: int | None = None,
    refrescador: RefrescadorOAuth | None = None,
) -> ResultadoDelBarrido:
    """Consulta las candidatas y renueva el lote. Es lo que llama Celery Beat.

    ## Por qué esto no sustituye a `asegurar_credentialo_vigente_en_sesion_ajena`

    Porque solo funciona mientras este proceso esté sano. Es una medida de comodidad y no de
    corrección: quita la renovación del camino crítico del webhook —que no tiene nada de usuario
    alrededor— y la adelanta a antes de que nadie la necesite, pero un `beat` caído, una ventana más
    corta que el intervalo, o un proveedor caído más rato que la ventana dejan la credencial
    caducada igual. Por eso el camino del worker **también** renueva, y no delega aquí. Las dos
    piezas juntas cubren más que cada una por separado; con solo una de las dos habría un camino
    entero sin cubrir, que es peor que no tener arreglo.

    ## Por qué son dos funciones y no una

    Porque la parte que **escribe** no se puede comprobar sin tocar filas de otros tenants: la
    consulta es global por naturaleza —«qué credenciales de esta instalación van a caducar» no
    admite un filtro de una organización— y la base de las pruebas es la misma que usa la
    demostración. Separando el listado del lote, la consulta se puede comprobar en modo lectura
    sobre lo que haya, y la renovación se puede comprobar sobre una lista que la prueba ha elegido.
    Una sola función con las dos cosas mezcladas solo se podría probar de una manera: letting que
    el doble de red escriba sobre credenciales que no son de la prueba.
    """

    ventana_real = (
        ventana
        if ventana is not None
        else timedelta(seconds=settings.git_token_proactive_refresh_window_seconds)
    )
    # La sesión de lectura es una más: el listado no escribe, y no tiene por qué ir con la sesión
    # de la primera renovación.
    async with sesion_ajena() as lector:
        candidatas = await credenciales_por_vencer(
            lector,
            ventana=ventana_real,
            momento=ahora,
            limite=limite,
        )
    return await renovar_lote(
        candidatas,
        sesion_ajena=sesion_ajena,
        margen=margen if margen is not None else ventana_real,
        refrescador=refrescador,
    )


async def renovar_lote(
    candidatas: list[tuple[UUID, GitProviderEnum]],
    *,
    sesion_ajena: SesionAjena = _sesion_propia,
    margen: timedelta | None = None,
    refrescador: RefrescadorOAuth | None = None,
) -> ResultadoDelBarrido:
    """Renueva una lista de candidatas, una a una y sin encadenar fallos.

    ## Por qué cada credencial va en su propia sesión

    Porque `asegurar_credentialo_vigente` toma un `FOR UPDATE` sobre la fila y mantiene el bloqueo
    durante la llamada al proveedor. Todas en una sola transacción serían N bloqueos —y N llamadas a
    GitHub— vivos a la vez durante la más lenta de todas, y una sola excepción dejaría el lote
    entero sin confirmar. Una por una: un fallo es el de esa fila y el resto sigue.

    ## Por qué un fallo no se propaga

    Porque este barrido corre periódico y no tiene a nadie esperando. Propagar dejaría las demás
    credenciales sin renovar y el `error_code` de la tarea en `FAILURE` en cada ciclo. Lo que no
    puede es **callarse**: cada fallo se registra con su organización, su proveedor y su estado, y
    el resumen dice cuántas fallaron. Un barrido que falla se ve en el log; uno que se come los
    fallos, no.

    ## Por qué el `margen` no lo decide el llamador por su cuenta

    Porque el listado y la renovación tienen que usar **la misma medida**. Si el listado dice «esta
    credencial está dentro de la ventana» y la renovación decide con el margen de 60 s de la ruta
    HTTP, todas las candidatas volverían `VIGENTE` sin llamar al proveedor y el barrido sería un
    no-op silencioso. Quien llama puede pasar un margen distinto —el barrido periódico pasa la
    ventana entera—, pero tiene que decidirlo a propósito.
    """

    renovate = 0
    fallidas = 0
    sin_refresh_token = 0
    for organization_id, provider in candidatas:
        try:
            resultado = await asegurar_credentialo_vigente_en_sesion_ajena(
                organization_id,
                provider,
                sesion_ajena=sesion_ajena,
                margen=margen,
                refrescador=refrescador,
            )
        except GitCredencialNoRenovableError as error:
            fallidas += 1
            logger.error(
                "No se pudo renovar por adelantado la credencial: organization=%s provider=%s "
                "estado=%s. Queda caducada hasta que el cliente vuelva a conectarla.",
                organization_id,
                provider.value,
                error.estado.value,
            )
            continue
        except (GitCredencialNoDisponibleError, CryptoError) as error:
            fallidas += 1
            logger.error(
                "No se pudo renovar por adelantado la credencial por un fallo recuperable: "
                "organization=%s provider=%s motivo=%s",
                organization_id,
                provider.value,
                type(error).__name__,
            )
            continue
        except GitCredentialNotFoundError:
            # La fila se borró entre el listado y el intento: es un `ON DELETE CASCADE` de una
            # organización que alguien está borrando. No es un fallo de renovación y no se cuenta.
            logger.info(
                "La credencial desapareció antes de renovarla por adelantado: organization=%s "
                "provider=%s",
                organization_id,
                provider.value,
            )
            continue
        if resultado.estado is EstadoDeRefresh.REFRESCADO:
            renovate += 1
        elif resultado.estado is EstadoDeRefresh.SIN_RENOVACION:
            sin_refresh_token += 1

    resumen = ResultadoDelBarrido(
        renovate=renovate,
        fallidas=fallidas,
        sin_refresh_token=sin_refresh_token,
        candidatas=len(candidatas),
    )
    if fallidas:
        logger.error(
            "El barrido de renovación por adelantado dejó %d de %d credenciales sin renovar",
            fallidas,
            len(candidatas),
        )
    elif candidatas:
        logger.info(
            "Barrido de renovación por adelantado: %d de %d renovadas, ninguna fallida",
            renovate,
            len(candidatas),
        )
    return resumen


async def _refrescar_bajo_bloqueo(
    credential: GitCredential,
    config: OAuthProviderSettings,
    refrescador: RefrescadorOAuth,
    momento: datetime,
) -> ResultadoDeRefresh:
    """Canjea el `refresh_token`, lo cifra y lo guarda. Se llama con la fila ya bloqueada.

    ## Por qué los fallos del proveedor no salen como excepciones

    Porque quien necesita distinguirlos es el router, que es donde se traduce a un código HTTP, y
    esa traducción depende de lo que va a ver el panel: `410` para «reconecta», `502` para «GitHub
    está caído», `503` para «esta instalación no tiene OAuth App de GitHub». Una excepción que se
    encadena con `from` acaba en la respuesta con un `500` genérico, que es justo lo que hay que
    evitar. `CryptoError` **sí** se propaga, porque ese caso ya está traducido a `500` en
    `_open_client` y es un fallo de servidor, no del proveedor.

    ## Por qué solo se registra el estado y el nombre de la clase del error

    Porque el `refresh_token` está descifrado en este marco. Los mensajes que llegan aquí ya vienen
    saneados por `oauth.py` —sin token y sin `error_description`—, y aun así se registra solo el
    nombre de la clase del error de `httpx`: es lo que distingue «se cayó la red» de «el TLS no
    valió», y no contiene ni la petición ni el cuerpo.
    """
    organization_id = credential.organization_id
    provider = credential.provider
    try:
        refresh_token = decrypt_secret(
            credential.encrypted_refresh_token or "",
            organization_id=str(organization_id),
            provider=provider.value,
            field="refresh",
        )
        token = await refrescador(config, refresh_token)
    except CryptoError as error:
        logger.error(
            "No se pudo descifrar el refresh token guardado: organization=%s provider=%s "
            "motivo=%s",
            organization_id,
            provider.value,
            type(error).__name__,
        )
        raise
    except OAuthConfigurationError as error:
        logger.error(
            "La OAuth App del proveedor no está configurada: organization=%s provider=%s "
            "motivo=%s",
            organization_id,
            provider.value,
            type(error).__name__,
        )
        return ResultadoDeRefresh(EstadoDeRefresh.NO_CONFIGURADO, credential.token_expires_at)
    except OAuthRefreshError as error:
        no_disponible = isinstance(error, OAuthRefreshUnavailableError)
        logger.warning(
            "El proveedor no devolvió un token renovado: organization=%s provider=%s "
            "rechazo=%s",
            organization_id,
            provider.value,
            type(error).__name__,
        )
        return ResultadoDeRefresh(
            EstadoDeRefresh.NO_DISPONIBLE if no_disponible else EstadoDeRefresh.RECHAZADO,
            credential.token_expires_at,
        )

    # A partir de aquí ya no hay excepciones: el token del proveedor está en la mano y hay que
    # dejarlo escrito. Lo que viene es cifrado y actualización de columnas.
    credential.encrypted_access_token = encrypt_organization_credential(
        token.access_token,
        organization_id,
        provider,
        field="access",
    )
    if token.refresh_token:
        credential.encrypted_refresh_token = encrypt_organization_credential(
            token.refresh_token,
            organization_id,
            provider,
            field="refresh",
        )
    # Sin `expires_in` no se puede saber hasta cuándo vale. Se guarda `None` —que en esta tabla
    # significa «no caduca»— y se avisa: el síntoma es que esa credencial deja de renovarse sola,
    # y un token que nadie vuelve a mirar es peor que uno que se sabe caducado.
    credential.token_expires_at = (
        momento + timedelta(seconds=token.expires_in)
        if token.expires_in is not None
        else None
    )
    nueva_caducidad = credential.token_expires_at
    if nueva_caducidad is None:
        # Sin `expires_in` no se puede saber hasta cuándo vale. Guardar `None` es lo único que se
        # puede hacer, y en esta tabla `None` significa «no caduca», así que el síntoma es que esa
        # credencial deja de renovarse sola. Se dice en voz alta: un token que nadie vuelve a mirar
        # es peor que uno que se sabe caducado, y esto no es un fallo de la petición.
        logger.warning(
            "El proveedor no devolvió expires_in al renovar la credencial: organization=%s "
            "provider=%s. Se queda sin fecha de caducidad y no se renovará sola.",
            organization_id,
            provider.value,
        )
    else:
        logger.info(
            "Credencial Git renovada: organization=%s provider=%s caduca_en=%s",
            organization_id,
            provider.value,
            nueva_caducidad.isoformat(),
        )
    return ResultadoDeRefresh(EstadoDeRefresh.REFRESCADO, credential.token_expires_at)
