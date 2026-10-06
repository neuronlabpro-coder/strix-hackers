"""Endpoints multi-tenant de inventario, conexión y gestión de repositorios."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.repositories import reviews
from backend.apps.repositories.clients.base import BaseGitClient, GitClientError
from backend.apps.repositories.clients.factory import UnsupportedGitProviderError
from backend.apps.repositories.inventory import (
    NormalizedRepository,
    RemoteRepositoryError,
    normalize_remote_repository,
    webhook_callback_url,
    webhook_subscription_events,
)
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
    generate_webhook_secret,
)
from backend.apps.repositories.reviews import DispatchDependency as ReviewDispatch
from backend.apps.repositories.schemas import (
    PRReviewMetrics,
    PRReviewPage,
    PRReviewResponse,
    RemoteRepositoryPage,
    RemoteRepositoryResponse,
    RepositoryConnectRequest,
    RepositoryConnectResponse,
    RepositoryPage,
    RepositoryResponse,
    RepositoryUpdateRequest,
)
from backend.apps.repositories.services import (
    GitCredentialNotFoundError,
    build_organization_client,
)
from backend.apps.repositories.token_refresh import (
    EstadoDeRefresh,
    asegurar_credentialo_vigente,
)
from backend.core.crypto import CryptoError
from backend.core.database import get_db
from backend.core.filtros_texto import coincide, escape_like
from backend.core.middleware import (
    AdminRequired,
    TenantContext,
    exigir_admin_del_tenant,
    get_current_tenant,
)
from backend.core.rate_limit import enforce_repository_management_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]
ManagementRateLimit = Depends(enforce_repository_management_rate_limit)

_IN_PROGRESS_REVIEW_STATUSES = (PRReviewStatusEnum.QUEUED, PRReviewStatusEnum.SCANNING)
_SUPPORTED_MANAGEMENT_PROVIDERS = frozenset({GitProviderEnum.GITHUB, GitProviderEnum.GITLAB})

#: La dependencia de encolado del análisis de una revisión.
#:
#: Es una dependencia y no una llamada directa a `run_pr_security_pipeline.delay` porque el
#: servicio —`reviews.py`— no depende de FastAPI, y porque así la prueba puede sustituirla sin
#: tocar Redis. Es exactamente el mismo patrón que `DispatchDependency` en `pentests/router.py`.
#:
#: Y se llama `get_dispatch_pr_review` en vez de reusar el `dispatch` de pentests a propósito: las
#: dos tareas se registran con nombres distintos en Celery, con cola y límites de tiempo
#: distintos, y un mismo parámetro que devuelve `task_id` no las hace intercambiables.
def get_dispatch_pr_review() -> ReviewDispatch:
    """Entrega el despachador real de revisiones.

    ## Por qué **no** declara `review_id` como parámetro

    Porque FastAPI resuelve las dependencias como si fueran endpoints, y un parámetro sin
    `Annotated[..., Query()]` se interpreta como un parámetro de consulta. La primera versión de
    esta función era `async def get_dispatch_pr_review(review_id: str) -> str`, y el resultado
    fue que la ruta recibía un `str` —el identificador de la revisión— donde esperaba una
    función, y `lanzar_analisis_de_review` fallaba al llamar con `'str' object is not callable`.

    ## Por qué es una **fábrica** y no la función de encolado directa

    Porque es lo que hace posible sustituirla en las pruebas con `dependency_overrides` sin tocar
    Redis, que es lo mismo que hace `get_dispatch_pentest_run` y por el mismo motivo. Si la ruta
    llamara a `run_pr_security_pipeline.delay(...)` en el cuerpo del manejador, ninguna prueba
    podría ejercitar el camino sin encolar de verdad, y el `503` de cola caída —que es uno de los
    modos de fallo que importan— no se podría provocar.

    ## Por qué el import es **dentro** de la fábrica

    Porque `backend.apps.repositories.tasks` importa el `celery_app`, que **no** registra sus
    tareas hasta que se importa; un import en el nivel superior del router cargaría el broker y
    las tareas del worker en cada proceso de la API, incluido el del servidor MCP. Es el mismo
    motivo por el que `pentests/router.py` importa `execute_pentest_run` en el módulo y este lo
    hace al vuelo: son dos caminos distintos y la decisión se tomó por el coste de arranque, no
    por estética.
    """

    def _dispatch(review_id: str) -> str:
        from backend.apps.repositories.tasks import run_pr_security_pipeline

        return run_pr_security_pipeline.delay(review_id).id  # pyright: ignore[reportFunctionMemberAccess]

    return _dispatch


ReviewDispatchDependency = Annotated[ReviewDispatch, Depends(get_dispatch_pr_review)]


async def _require_admin(tenant: TenantDependency) -> None:
    """Exige `ADMIN` del workspace, o superusuario.

    ## Por qué `async` y por qué `TenantDependency`, y no `TenantContext` a secas

    Porque esta función se usa de las dos formas: en el `dependencies=[...]` de un decorador
    **y** llamada en el cuerpo de otras rutas. Con la anotación suelta, la forma del decorador
    le dice a FastAPI que `TenantContext` es un modelo de respuesta, no un parámetro ya
    resuelto, y la aplicación no arranca:

        `FastAPIError: Invalid args for response field! Hint: check that <class
        'backend.core.middleware.TenantContext'> is a valid Pydantic field type.`

    El alias `Annotated[TenantContext, Depends(get_current_tenant)]` lleva la dependencia
    declarada y las dos formas funcionan.

    ## Por qué se delega en la regla de `core` en vez de repetirla

    Porque antes esta comparaba `tenant.role != RoleEnum.ADMIN` **sin** la excepción del
    superusuario, y las otras cuatro copias del módulo sí la tenían. Cuatro reglas de
    autorización que se distinguen en un `and not` son cuatro reglas, y un superusuario que
    podía administrar un recurso por un router y no por otro es exactamente el tipo de
    diferencia que se descubre cuando un cliente necesita arreglado algo.

    Y por qué el mensaje sigue siendo el de este módulo: es el que lee quien llama, y un
    `403` que dice «repositorios» localiza el problema más rápido que uno que dice «esta
    operación».
    """

    try:
        await exigir_admin_del_tenant(tenant)
    except HTTPException as error:
        raise HTTPException(
            status_code=error.status_code,
            detail="Se requiere permiso de administrador",
        ) from error


def _translate_client_error(error: GitClientError) -> HTTPException:
    """Convierte un error del proveedor en una respuesta con código distinguible.

    ## Por qué el `401` del proveedor contesta `401` y no `502`

    Porque antes contestaba `502` —«Bad Gateway»— y con eso el panel no tenía forma de
    distinguir «tu credencial caducó o el proveedor la rechazó» de «GitHub se ha caído». Son dos
    fallos opuestos: el primero lo arregla quien usa el panel reconectando, el segundo esperando.
    Con un solo código los dos salían en pantalla con el mismo mensaje, que es exactamente el
    diagnóstico que hace inútil un ticket.

    ## Por qué `401` y no `400`

    Porque es la misma traducción que ya está escrita y razonada en `router_auth.py`, en el alta
    de token personal: *«Se traduce a `400` y no a `502` porque la causa es la credencial, no el
    proveedor: un `401` del proveedor no es un problema de la plataforma que se pueda
    reintentar»*. Aquí estaba en `502` y allí en `400`, y dos rutas que consumen la misma
    credencial no pueden contestar con códigos distintos para la misma causa.

    ## Por qué el `403` del proveedor se agrupa con el `401`

    Porque para quien usa el panel es el mismo problema y la misma solución: con lo que tiene no
    puede leer repositorios. Separarlos obligaría a la interfaz a ofrecer una acción que no
    existe para uno de los dos. Lo que sí se distinguen —caducada de rechazada— no llega por
    aquí: se detecta antes, en `_exigir_credencial_vigente`, que ya conoce la fecha de caducidad.

    ## Por qué un `401` aquí es seguro y hay que vigilarlo

    Porque en el panel un `401` significa normalmente «tu sesión ha caducado», y
    `lib/session-errors.ts` borra la sesión cuando lo ve. Hoy ese clasificador **solo** se aplica
    al arranque de la sesión —`loadOrganizations`— y no a las llamadas de cada pantalla, así que
    un `401` del inventario no cierra la sesión de nadie. Si algún día se extiende ese
    clasificador a las peticiones de las pantallas, este `401` empezará a expulsar al usuario al
    login cada vez que su credencial de GitHub caduque, que es justo lo contrario de lo que
    arregla. Por eso el texto del modal y el `401` del panel tienen que seguir tratándose como
    cosas distintas, y por eso el caso de «caducada» se evita con el `410`.
    """
    if error.status_code in {401, 403}:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="La credencial del proveedor fue rechazada o carece de permisos",
        )
    if error.status_code == 404:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El recurso no existe en el proveedor Git",
        )
    if error.status_code == 429:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="El proveedor Git agotó su cuota temporal",
            headers={"Retry-After": str(error.retry_after or 30)},
        )
    if error.status_code is not None and error.status_code >= 500:
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="El proveedor Git no está disponible",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="No se pudo completar la operación con el proveedor Git",
    )


async def _exigir_credencial_vigente(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
) -> None:
    """Deja la credencial en vigor, o responde por qué no se pudo.

    ## Por qué esto hace falta y no lo hacía

    Porque `git_credentials.token_expires_at` se rellenaba en el callback de OAuth y no se leía
    **en ningún sitio**. Una credencial OAuth de GitHub dura ocho horas: al día siguiente el
    proveedor contesta `401`, la ruta lo traducía a `502`, el modal lo pintaba como «este
    proveedor todavía no tiene conector» y el checklist de onboarding seguía marcando «cuenta
    Git conectada» porque contaba filas. Dos pantallas afirmando cosas opuestas sobre la misma
    fila, y ninguna de las dos preguntándose si esa fila seguía sirviendo.

    ## Por qué ahora **renueva** y no solo corta

    Porque antes de este arreglo el `410` era la única salida, y la salida correcta para un token
    OAuth caducado no es «vuelve a conectar»: es «se ha renovado solo». La fila guardaba un
    `refresh_token` cifrado desde la fase 3 que no usaba nadie, de modo que un cliente que conectaba
    su GitHub el lunes perdía la integración el martes, sin explicación y sin manera de arreglarlo
    desde el panel. Para un producto que se vende, eso no es un detalle: es la integración entera
    rota a las ocho horas.

    La caducidad se sigue mirando antes de llamar al proveedor —no se gasta una ida y vuelta para
    que GitHub conteste `401` lo que ya sabemos—, pero cuando hay `refresh_token` lo que se hace
    es renovarlo. El trabajo está en `token_refresh.py`; aquí solo se traduce su veredicto a un
    código de respuesta.

    ## Por qué se corta **antes** de llamar al proveedor cuando no hay salida

    Por dos razones, y las dos son de coste y de honestidad. De coste: una ida y vuelta a GitHub
    para que conteste `401` no compra nada que no sepamos ya. De honestidad: el mensaje «tu
    credencial caducó» solo es posible si la caducidad la dice la base de datos; si se espera al
    `401` del proveedor, la respuesta correcta es «la rechazó», que es un hecho distinto con una
    solución distinta.

    ## Por qué `410` y no `401`

    Porque son dos hechos distintos y el panel necesita separarlos: `401` es «el proveedor no te
    reconoce» y se arregla mirando permisos; `410` es «esto que guardamos se apagó el día X» y se
    arregla reconectando. Con un solo código el panel tendría que elegir uno de los dos textos y
    acertar por azar. `410 Gone` es justo la semántica de HTTP para un recurso que existió y ya
    no, y no lo usa ninguna otra ruta del proyecto.

    ## Por qué no se contesta `401` ni siquiera cuando la renovación ha funcionado

    Porque la renovación **no cambia el estado de la respuesta**: si el token estaba en vigor o se
    ha renovado, la ruta sigue contestando lo que contestaba —`200`— y este punto ni se ve. Lo que
    importa es que el caso que queda, «no se pudo renovar», se contesta con `410` y no con `401`:
    en el panel un `401` significa «tu sesión ha caducado» y `lib/session-errors.ts` borra la
    sesión cuando lo ve. Hoy ese clasificador solo se aplica al arranque de la sesión, pero si
    algún día se extiende a las peticiones de cada pantalla, un `401` por credencial de GitHub
    expulsaría al usuario al login cada vez que su repositorio se re-sincronice. Que la renovación
    haya funcionado no produce ningún estado nuevo; que no haya podido sí, y ese es `410`.

    ## Por qué `token_expires_at IS NULL` no es una credencial caducada

    Porque es lo que deja un token personal: un PAT no lleva fecha de caducidad conocida y
    `connect_personal_token` lo pone a `None` a propósito. Marcarlo como caducado sería inventar
    un dato que no existe, y el panel pediría una reconexión que no arregla nada. Tampoco se
    intenta renovar: un PAT no tiene `refresh_token`, y si lo tuviera —porque sustituyó a una
    conexión OAuth— intentar renovarlo cambiaría el token del usuario sin avisar.

    ## Por qué un proveedor caído no se traduce a `410`

    Porque son al revés. `410` significa «reconecta tu cuenta», y esa es la acción que resuelve el
    caso de verdad. Si GitHub está caído y el panel pide reconectar, el usuario obedece, llega a la
    pantalla de consentimiento de GitHub, y GitHub sigue caído. Además los `401` y `403` del
    proveedor sí se traducen a `502` en `_translate_client_error`, y una renovación que saliera
    `410` porque GitHub tuvo un mal día sería el mismo error de diagnóstico que este arreglo
    corrije, con otro envoltorio.

    ## Por qué «OAuth App no configurada» no se disfraza de `410`

    Porque reconectar no lo arregla: si a esta instalación no le falta `GITHUB_OAUTH_CLIENT_ID` ni
    el secreto, mandar al usuario a la pantalla de consentimiento lo devuelve al mismo sitio. Es el
    mismo criterio que ya usa `authorize` para el mismo caso, y por eso sale `503`.

    ## Por qué aquí ya no se explica el filtro por `organization_id`

    Porque la consulta que exige la caducidad se ha movido a `asegurar_credentialo_vigente`, en
    `token_refresh.py`, y el motivo de R3 —la credencial que hay que renovar es la de **esta**
    organización, y la caducidad de la de otra no dice nada de este tenant— está escrito allí, junto
    a la consulta que lo aplica. Dejarlo aquí sería documentarlo en un sitio donde ya no hay SQL.
    """

    try:
        resultado = await asegurar_credentialo_vigente(session, organization_id, provider)
    except GitCredentialNotFoundError:
        # Sin fila no hay nada que exigir aquí. Que falte la credencial lo dice el `409` que
        # `_open_client` levanta al no conseguir el cliente, que es donde ese caso ya vive.
        return
    if resultado.estado in {EstadoDeRefresh.VIGENTE, EstadoDeRefresh.REFRESCADO}:
        return
    if resultado.estado is EstadoDeRefresh.NO_DISPONIBLE:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="El proveedor Git no está disponible para renovar la credencial",
            headers={"Retry-After": "30"},
        )
    if resultado.estado is EstadoDeRefresh.NO_CONFIGURADO:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Esta instalación no tiene configurada la OAuth App de {provider.value}, "
                "así que no puede renovar la credencial"
            ),
        )
    # `SIN_REFRESH_TOKEN` y `RECHAZADO` son el mismo hecho para quien lo ve: lo que guardamos se
    # apagó y no hay forma de recuperarlo sin que el usuario vuelva a autorizar la aplicación.
    caducado = resultado.caduca_en.isoformat() if resultado.caduca_en else "sin fecha conocida"
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            f"La credencial de {provider.value} de esta organización caducó el {caducado} "
            "y hay que volver a conectarla"
        ),
    )

@asynccontextmanager
async def _open_client(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
) -> AsyncIterator[BaseGitClient]:
    """Abre el cliente del tenant distinguiendo **por qué** no se puede abrir.

    ## Por qué aquí las causas van separadas y en `_try_open_client` no

    Porque `_try_open_client` devuelve `None` para tres cosas que no son la misma, y uno de sus
    dos llamadores —el borrado del webhook al desvincular un repositorio— **quiere** ese
    `None`: si no hay credencial utilizable, avisa por log y sigue con el desvínculo local, que es
    lo que evita dejar un repositorio atado a un webhook que nadie va a borrar.

    Aquí la situación es la contraria: quien llama está inventariando o conectando, y un `None`
    sin explicación se convierte en un «no hay repositorios con esta credencial» que es falso
    cuando lo que hay es una credencial caducada o una clave de cifrado que no abre lo guardado.
    Por eso este camino traduce cada causa a su código, y el otro se los queda todos en `None`.

    ## Por qué `CryptoError` es `500` y no `409`

    Porque la fila **existe**: lo que falla es la clave maestra con la que se cifró, que se rotó
    o no es la de esta instalación. Un `409` —«conecta primero una credencial de GitHub»— sería
    justo lo contrario de la verdad, y además es un bucle: el usuario reconecta, se cifra con la
    misma clave, y vuelve a fallar. Lo que se puede hacer es mirar los logs, y para eso tiene que
    ser un `5xx`.

    ## Por qué `UnsupportedGitProviderError` sale con `501` y no se traga

    Porque con el proveedor ya filtrado por `_SUPPORTED_MANAGEMENT_PROVIDERS` no puede ocurrir;
    pero si mañana se añade un proveedor al enum y se olvida su conector, esta función no debe
    cambiar el síntoma —«no hay credencial»— por el motivo real —«no hay conector»—. El `501` es
    el mismo que ya devuelve la guarda de arriba.

    ## Por qué la renovación va dentro del mismo `try`

    Porque `_exigir_credencial_vigente` no solo mira fechas: también descifra el `refresh_token`
    para renovarlo, y ese descifrado puede fallar con `CryptoError` si la clave maestra con la que
    se cifró no es la de esta instalación. Es el mismo fallo de servidor que ya está traducido a
    `500` un poco más abajo, y por eso tiene que caer en el mismo `except`: fuera de él saldría como
    un `500` de FastAPI sin mensaje, que es justo lo que ese `except` existe para evitar.
    """

    if provider not in _SUPPORTED_MANAGEMENT_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="El proveedor todavía no tiene conector de gestión",
        )
    try:
        # Dentro del `try`, no antes: el motivo está en el docstring de arriba.
        await _exigir_credencial_vigente(session, organization_id, provider)
        client = await build_organization_client(session, organization_id, provider)
    except (GitCredentialNotFoundError, UnsupportedGitProviderError) as error:
        codigo = (
            status.HTTP_501_NOT_IMPLEMENTED
            if isinstance(error, UnsupportedGitProviderError)
            else status.HTTP_409_CONFLICT
        )
        detalle = (
            "El proveedor todavía no tiene conector de gestión"
            if isinstance(error, UnsupportedGitProviderError)
            else f"Conecta primero una credencial de {provider.value} para esta organización"
        )
        raise HTTPException(status_code=codigo, detail=detalle) from error
    except CryptoError as error:
        logger.error(
            "No se pudo descifrar la credencial Git guardada: provider=%s reason=%s",
            provider.value,
            str(error),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo leer la credencial guardada para esta organización",
        ) from error
    try:
        yield client
    finally:
        client.close()


async def _try_open_client(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
) -> BaseGitClient | None:
    """Construye el cliente del tenant o devuelve `None` si no hay credencial utilizable.

    Se mantiene el `None` para las tres causas, y sin distinguirlas, a propósito: el llamador de
    la desvinculación trata «no hay credencial utilizable» como un caso normal y sigue adelante.
    Quien necesita saber el motivo usa `_open_client`.
    """

    try:
        return await build_organization_client(session, organization_id, provider)
    except (GitCredentialNotFoundError, UnsupportedGitProviderError, CryptoError):
        return None


def _normalized_inventory(
    provider: GitProviderEnum,
    raw_repositories: list[dict[str, object]],
) -> list[NormalizedRepository]:
    """Descarta entradas que no podemos materializar de forma segura."""

    normalized: list[NormalizedRepository] = []
    for payload in raw_repositories:
        try:
            normalized.append(normalize_remote_repository(provider, payload))
        except RemoteRepositoryError as error:
            logger.warning(
                "Repositorio remoto descartado del inventario: provider=%s reason=%s",
                provider.value,
                str(error),
            )
    return normalized


def _filtrar_inventario(
    inventory: list[NormalizedRepository], search: str | None
) -> list[NormalizedRepository]:
    """Filtra el inventario por un texto, o lo devuelve entero si no hay texto.

    ## Por qué compara `full_name`, `name` y `default_branch`

    Porque los tres son cosas que el usuario escribe. `acme/api-gateway` se busca por `gateway`,
    y una rama `feature/shy-redesign` se busca por `shy`. Con solo el `full_name`, buscar `shy`
    no encuentra un repositorio cuya rama lo tenga, y el usuario concluye que el repositorio no
    existe.

    ## Por qué ignora mayúsculas pero no acentos

    Porque el nombre de un repositorio en GitHub no distingue mayúsculas de minúsculas, así que
    `API-Gateway` y `api-gateway` son la misma búsqueda y tratarlas distinto sería una sorpresa.
    Con los acentos pasa lo contrario: `diseño` y `diseno` **son** cadenas distintas en un
    nombre de GitHub, y un desplegar un `unicode`-folding introduciría colisiones que no existen
    en el proveedor. Se compara lo que el proveedor considera la misma cadena.
    """
    ## Por qué no hay un atajo para la búsqueda vacía
    #
    # ## Por qué no hay vuelta atrás para la búsqueda vacía
    #
    # Porque no hace falta, y se comprobó. La aguja vacía es subcadena de cualquier cadena, así
    # que `"" in texto` es `True` para todo repositorio y la lista comprehensión devuelve el
    # inventario entero igual que lo haría un `if consulta == "": return inventory`.
    #
    # Se dejó puesto un atajo, y al reintroducir los defectos uno a uno salió el único que ningún
    # test detectaba: quitarlo no cambiaba nada. Eso lo convierte en código muerto, y el código
    # muerto en una función de tres líneas es peor que la línea que ahorra: parece que importa.
    #
    # Y el `strip()` de arriba es lo que de verdad resuelve el caso de los espacios: una búsqueda
    # de «   » llega aquí como aguja vacía, y sin el `strip` sería una búsqueda literal de tres
    # espacios que no encuentra nada. `test_una_busqueda_vacia_o_de_solo_espacios_no_filtra` es
    # el que vigila esa parte.
    consulta = (search or "").strip().lower()
    return [
        repository
        for repository in inventory
        if consulta
        in "\n".join(
            (repository.full_name, repository.name, repository.default_branch)
        ).lower()
    ]


@router.get(
    "/api/v1/repositories/remote",
    response_model=RemoteRepositoryPage,
    dependencies=[ManagementRateLimit, AdminRequired],
)
async def list_remote_repositories(
    tenant: TenantDependency,
    session: SessionDependency,
    provider: Annotated[GitProviderEnum, Query()],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> RemoteRepositoryPage:
    """Lista el inventario accesible con la credencial conectada del tenant.

    ## Por qué hay un `search` aquí y no solo en el cliente

    Porque el inventario **no** se descarga entero: `limit` está topado a 100, así que un
    workspace con 500 repositorios solo deja ver 50 si el buscador filtra en el navegador. El
    síntoma era buscar `shy` y ver un resultado de los doce que hay, sin ninguna pista de que
    faltaran los otros once.

    ## Por qué aquí no cuesta nada y en el cliente sí

    Porque el cliente del proveedor se llama una vez y devuelve **el inventario entero** —eso
    ya lo hacía esta función para el `total`—. El corte por `limit`/`offset` es posterior, así
    que filtrar por `search` es recorrer una lista que ya está en memoria. No hay una segunda
    ida al proveedor y no hay una consulta a la base: el filtro va donde ya estaban los datos.
    """

    connected_result = await session.execute(
        select(Repository.remote_repo_id).where(
            Repository.organization_id == tenant.organization.id,
            Repository.provider == provider,
        )
    )
    connected_ids = {row for row in connected_result.scalars()}
    async with _open_client(session, tenant.organization.id, provider) as client:
        try:
            raw_repositories = await asyncio.to_thread(client.list_repositories)
        except GitClientError as error:
            raise _translate_client_error(error) from error
        normalized = _normalized_inventory(provider, raw_repositories)
    filtrados = _filtrar_inventario(normalized, search)
    window = filtrados[offset : offset + limit]
    return RemoteRepositoryPage(
        items=[
            RemoteRepositoryResponse.from_normalized(
                repository,
                already_connected=repository.remote_repo_id in connected_ids,
            )
            for repository in window
        ],
        # El total es el de la lista **filtrada**, no el del inventario entero. Con lo
        # contrario, buscando `shy` la interfaz respondería «12 de 500» cuando lo que hay son 12
        # de 12, y el número que sirve para saber si falta algo —`total`— mentiría justo cuando
        # el usuario está intentando saber si le falta algo.
        total=len(filtrados),
        limit=limit,
        offset=offset,
        provider=provider,
        busqueda_aplicada=(search or "").strip() or None,
    )


@router.post(
    "/api/v1/repositories/connect",
    response_model=RepositoryConnectResponse,
    dependencies=[ManagementRateLimit],
)
async def connect_repository(
    response: Response,
    tenant: TenantDependency,
    session: SessionDependency,
    payload: RepositoryConnectRequest,
) -> RepositoryConnectResponse:
    """Da de alta el repositorio verificando sus metadatos contra el proveedor."""

    await _require_admin(tenant)
    # La búsqueda va **con** `organization_id` y no contra una tabla entera.
    #
    # ## Por qué esto ya no distingue «existe en otra organización»
    #
    # Antes se buscaba por `(provider, remote_repo_id)` a secas y, si la fila era de otro tenant,
    # se devolvía un `409` con el texto de que ya estaba vinculada a otra organización. Eso era un
    # oráculo: `remote_repo_id` es un identificador público de GitHub, y recorrerlos con un
    # `ADMIN` de la propia organización permitía enumerar **qué repositorios tienen conectados
    # otros clientes de la plataforma** y aprender su `provider`.
    #
    # Y contradecía frontalmente el criterio del proyecto, que usa `404` en vez de `403` para no
    # confirmar la existencia de un recurso ajeno —`_load_tenant_repository`, línea 605, y
    # `list_pr_reviews`, línea 440, lo hacen en este mismo fichero.
    #
    # ## Por qué el `409` tampoco era necesario
    #
    # Porque la unicidad de `repositories` pasó a ser **por organización**
    # (`uq_repositories_org_provider_remote`). Dos clientes conectando el mismo repositorio no es
    # un problema —cada uno tiene su credencial, su espacio de revisión y su cargo— y con la
    # restricción global la organización A reclamaba un repositorio popular y la B no podía
    # conectarlo nunca. Sin esa restricción, no hay conflicto que señalar.
    #
    # ## Por qué se sigue mirando la fila del propio tenant
    #
    # Para que conectar dos veces el mismo repositorio en la misma organización sea un
    # **actualizar**, que es lo que hace el resto de la función, y no un error de unicidad que el
    # cliente no puede distinguir de un fallo suyo.
    existing_result = await session.execute(
        select(Repository).where(
            Repository.organization_id == tenant.organization.id,
            Repository.provider == payload.provider,
            Repository.remote_repo_id == payload.remote_repo_id,
        )
    )
    existing = existing_result.scalar_one_or_none()

    async with _open_client(session, tenant.organization.id, payload.provider) as client:
        try:
            raw_repository = await asyncio.to_thread(
                client.get_repository,
                payload.remote_repo_id,
            )
        except GitClientError as error:
            raise _translate_client_error(error) from error
        try:
            normalized = normalize_remote_repository(payload.provider, raw_repository)
        except RemoteRepositoryError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="El proveedor devolvió metadatos de repositorio no utilizables",
            ) from error

        repository = existing
        created = repository is None
        if repository is None:
            repository = Repository(
                organization_id=tenant.organization.id,
                provider=payload.provider,
                remote_repo_id=normalized.remote_repo_id,
                name=normalized.name,
                full_name=normalized.full_name,
                clone_url=normalized.clone_url,
                default_branch=normalized.default_branch,
                pr_reviews_enabled=payload.pr_reviews_enabled,
                webhook_secret=generate_webhook_secret(),
            )
            session.add(repository)
        else:
            repository.name = normalized.name
            repository.full_name = normalized.full_name
            repository.clone_url = normalized.clone_url
            repository.default_branch = normalized.default_branch
            repository.pr_reviews_enabled = payload.pr_reviews_enabled
            repository.is_active = True
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El repositorio ya está conectado",
            ) from error

        webhook_registered = False
        if repository.webhook_id is None:
            try:
                repository.webhook_id = await asyncio.to_thread(
                    client.create_webhook,
                    normalized.full_name,
                    webhook_callback_url(payload.provider),
                    repository.webhook_secret,
                    webhook_subscription_events(),
                )
                webhook_registered = True
            except (GitClientError, RemoteRepositoryError) as error:
                logger.warning(
                    "No se pudo registrar el webhook: provider=%s repository=%s reason=%s",
                    payload.provider.value,
                    normalized.full_name,
                    str(error),
                )
        else:
            webhook_registered = True
        await session.commit()
        await session.refresh(repository)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return RepositoryConnectResponse(
        repository=RepositoryResponse.from_repository(repository),
        webhook_registered=webhook_registered,
        created=created,
    )


@router.get("/api/v1/repositories/", response_model=RepositoryPage)
async def list_repositories(
    tenant: TenantDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    provider: Annotated[GitProviderEnum | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> RepositoryPage:
    """Lista paginada de repositorios conectados de la organización activa."""

    filters = [Repository.organization_id == tenant.organization.id]
    if provider is not None:
        filters.append(Repository.provider == provider)
    if is_active is not None:
        filters.append(Repository.is_active.is_(is_active))
    total_result = await session.execute(
        select(func.count()).select_from(Repository).where(*filters)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(Repository)
        .where(*filters)
        .order_by(Repository.created_at.desc(), Repository.id)
        .limit(limit)
        .offset(offset)
    )
    repositories = result.scalars().all()
    return RepositoryPage(
        items=[RepositoryResponse.from_repository(repository) for repository in repositories],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v1/repositories/{repository_id}/reviews",
    response_model=PRReviewPage,
)
async def list_repository_reviews(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    review_status: Annotated[PRReviewStatusEnum | None, Query(alias="status")] = None,
    source_branch: Annotated[str | None, Query(max_length=255)] = None,
) -> PRReviewPage:
    """Historial paginado de revisiones de seguridad de un repositorio del tenant.

    El repositorio se carga primero acotado a `organization_id`, de modo que un
    identificador ajeno devuelve `404` sin revelar siquiera si existe.
    """

    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    filters = [
        PullRequestReview.repository_id == repository.id,
        PullRequestReview.organization_id == tenant.organization.id,
    ]
    if review_status is not None:
        filters.append(PullRequestReview.status == review_status)
    if source_branch:
        # `coincide` escapa los comodines de `LIKE`. Aquí el caso real es `_`, porque los
        # nombres de rama lo llevan con frecuencia: sin escape, `?source_branch=fix_web_app`
        # también traería `fix-webXapp`, y `?source_branch=%` traería todas las revisiones del
        # repositorio. Es el fallo más silencioso de un filtro: la tabla sale llena y el
        # operador da por bueno un filtro que no ha filtrado nada.
        filters.append(coincide([PullRequestReview.source_branch], source_branch))

    total_result = await session.execute(
        select(func.count()).select_from(PullRequestReview).where(*filters)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(PullRequestReview)
        .where(*filters)
        .order_by(PullRequestReview.created_at.desc(), PullRequestReview.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return PRReviewPage(
        items=[
            PRReviewResponse.from_review(review, repository_name=repository.full_name)
            for review in result.scalars().all()
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


def _busqueda_por_texto(termino: str) -> ColumnElement[bool]:
    """Condición de la búsqueda por texto del historial de revisiones.

    ## Por qué busca en cuatro columnas y no en una

    Porque «¿se revisó esto?» no tiene un único sitio donde mirar: un nombre de repositorio, un
    título de pull request, el autor y la rama de origen son las cuatro cosas que una persona
    usa para recordar una revisión concreta. Con una sola columna, buscar `auth` devolvería
    página vacía la mitad de las veces y el buscador parecería roto.

    ## Por qué aquí el escape es el caso normal y no el caso límite

    Porque los nombres de repositorio y de rama llevan `_` —`acme/web_app`, `feature/fix_auth`—,
    así que buscar `web_app` sin escapar devolvería también `webXapp`, que el usuario no pidió.

    El escape viene de `core.filtros_texto` y no de una copia local: había una, se comprobó que
    las dos hacían lo mismo en los mismos términos y se consolidó. La prueba que cubre este
    punto es `test_pr_reviews_api.py::test_pr_reviews_search_treats_like_wildcards_as_literals`.

    ## Por qué el número de PR se compara con `==` y no con `LIKE`

    Porque `%42%` también casa con `142` y con `420`, y el número de un pull request es un
    identificador exacto: quien escribe `42` quiere la revisión 42, no las tres. Solo se compara
    cuando el término es puramente numérico, y se acota a nueve dígitos porque la columna es un
    entero de 32 bits y un término de cuarenta dígitos haría fallar la consulta entera en vez de
    devolver cero filas.
    """

    patron = f"%{escape_like(termino.lower())}%"
    alternativas: list[ColumnElement[bool]] = [
        func.lower(PullRequestReview.pr_title).like(patron, escape="\\"),
        func.lower(PullRequestReview.pr_author).like(patron, escape="\\"),
        func.lower(PullRequestReview.source_branch).like(patron, escape="\\"),
        func.lower(Repository.full_name).like(patron, escape="\\"),
    ]
    if termino.isdigit() and len(termino) <= 9:
        alternativas.append(PullRequestReview.pr_number == int(termino))
    return or_(*alternativas)


def _medianoche_utc(dia: date) -> datetime:
    """Medianoche UTC del día pedido.

    ## Por qué en UTC y no en hora local

    Porque `created_at` es `timestamptz` y PostgreSQL compara instantes. Si el corte se calculara
    en hora local, el mismo rango daría resultados distintos según desde qué zona se consulte, y
    «del lunes al martes» dejaría de ser una frase que significa lo mismo para todo el mundo.
    """

    return datetime(dia.year, dia.month, dia.day, tzinfo=UTC)


@router.get("/api/v1/pr-reviews/", response_model=PRReviewPage)
async def list_pr_reviews(
    tenant: TenantDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    review_status: Annotated[PRReviewStatusEnum | None, Query(alias="status")] = None,
    repository_id: UUID | None = None,
    query: Annotated[str | None, Query(max_length=256)] = None,
    created_from: date | None = None,
    created_to: date | None = None,
) -> PRReviewPage:
    """Historial paginado de revisiones de pull request de toda la organización.

    Acepta estado, repositorio, búsqueda por texto y rango de fechas de alta, y devuelve
    `total` con la página, que es lo que necesita la barra de paginación del panel.

    ## Por qué el rango de fechas va sobre `created_at` y no sobre `finished_at`

    Porque `finished_at` es `NULL` mientras la revisión está en cola o escaneando —los estados
    `QUEUED` y `SCANNING`—, así que filtrar por él haría desaparecer de la tabla las revisiones
    en curso en cuanto se tocara cualquiera de las dos fechas. El usuario vería desaparecer sus
    escaneos pendientes por haber pedido un rango, y lo leería como que el motor los perdió.

    Y porque `created_at` es la columna por la que se ordena el listado: el rango recorta filas y
    la paginación las cuenta, así que ambos van sobre la misma fecha y los bordes de página caen
    donde el usuario espera. La columna «Fecha» de la tabla sigue mostrando `finished_at`, que es
    la fecha de finalización del escaneo; el filtro se anuncia como rango de alta para que no se
    confundan.

    ## Por qué `created_to` es **inclusivo**

    Porque «del 1 al 5» son cinco días, no cinco días menos el último. El límite superior es la
    medianoche **del día siguiente**, en UTC, de modo que el último día entra entero. Con
    `<= medianoche_del_día` el día final solo aportaría las revisiones de exactamente las 00:00,
    que es un resultado que nadie quiere y que además depende de la zona horaria de quien
    pregunta.

    Un rango invertido —`created_from` posterior a `created_to`— devuelve la lista vacía y no un
    `422`: las dos condiciones son incompatibles por construcción, así que la respuesta ya es la
    que corresponde, y un error de validación obligaría al panel a manejar un estado que nunca
    se da.

    ## Por qué el `JOIN` es **incondicional**

    Porque `Repository.full_name` se selecciona en todas las filas, y sin el `JOIN` la consulta
    es un **producto cartesiano**: PostgreSQL devuelve cada revisión cruzada con **cada**
    repositorio de la base, y el nombre que sale en la respuesta es el del primero que se
    encuentra, no el de la revisión.

    SQLAlchemy lo avisa (`SAWarning: SELECT statement has a cartesian product between FROM
    element(s) "pull_request_reviews" and "repositories"`), y el aviso es correcto: esto era
    una **fuga entre tenants**. El contenido de la revisión se filtra bien por
    `organization_id`, pero el nombre del repositorio que la acompaña salía de cualquier otro
    tenant de la base. En una herramienta de pentesting, "tu revisión está en
    `competidor/privado`" es exactamente el tipo de dato que no debe aparecer.

    Solo pasaba desapercibido por suerte: el producto cartesiano devuelve las filas en el orden
    que el planificador elija, y cuando el repositorio del propio tenant salía primero el
    resultado era el correcto. Por eso la prueba de aislamiento era **intermitente** —fallaba
    en cuanto la base compartida tenia otro repositorio por delante— y no porque el filtro
    estuviera mal, sino porque el nombre venía de otro sitio.

    ## Por qué el filtro por `organization_id` sigue siendo obligatorio

    R3 no es negociable y esta es una vista global, que es donde más fácil sería colarse una
    fuga si se leyera solo por repositorio.

    El filtro sobre `PullRequestReview.organization_id` basta porque la base **ya** garantiza
    la coincidencia: `fk_pr_reviews_repository_organization` es una clave foránea compuesta
    sobre `(repository_id, organization_id)`, así que PostgreSQL no permite que una revisión
    apunte a un repositorio de otra organización. El filtro sobre `Repository.organization_id`
    que había aquí era redundante, y se quita: mantenía la desconfianza en un sitio donde la
    base ya la aplica, y hacía creer que las dos columnas podían separarse.
    """

    filters = [PullRequestReview.organization_id == tenant.organization.id]
    if review_status is not None:
        filters.append(PullRequestReview.status == review_status)
    if repository_id is not None:
        # Un repositorio de otro tenant no devuelve `403`: no se le dice al llamador si
        # existe o no. La lista simplemente sale vacía, igual que si el filtro fuese suyo
        # y no tuviera revisiones.
        filters.append(PullRequestReview.repository_id == repository_id)
    # `query` llega con los espacios que el usuario dejó en el campo de búsqueda, y un
    # término que solo sean espacios no es una búsqueda: es un filtro vacío. Se descarta
    # para que el botón de limpiar pueda comparar contra el mismo criterio que la consulta.
    if termino := (query or "").strip():
        filters.append(_busqueda_por_texto(termino))
    if created_from is not None:
        filters.append(PullRequestReview.created_at >= _medianoche_utc(created_from))
    if created_to is not None:
        # Inclusivo: el límite superior es la medianoche del día **siguiente**, no la del
        # propio `created_to`. Ver la nota del docstring.
        filters.append(
            PullRequestReview.created_at < _medianoche_utc(created_to) + timedelta(days=1)
        )

    join_condition = Repository.id == PullRequestReview.repository_id
    count_query = (
        select(func.count())
        .select_from(PullRequestReview)
        .join(Repository, join_condition)
        .where(*filters)
    )
    rows_query = select(PullRequestReview, Repository.full_name).join(Repository, join_condition)

    total = int((await session.execute(count_query)).scalar_one())
    result = await session.execute(
        rows_query.where(*filters)
        .order_by(PullRequestReview.created_at.desc(), PullRequestReview.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return PRReviewPage(
        items=[
            PRReviewResponse.from_review(review, repository_name=repository_name)
            for review, repository_name in result.all()
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/pr-reviews/metrics", response_model=PRReviewMetrics)
async def read_pr_review_metrics(
    tenant: TenantDependency,
    session: SessionDependency,
) -> PRReviewMetrics:
    """Indicadores de cabecera de la vista global.

    Se resuelven en una sola consulta agregada en lugar de tres `COUNT` separados: la
    cabecera se pinta en cada carga de la vista y tres viajes a la base por tres
    números que salen de la misma fila es trabajo desperdiciado.
    """

    result = await session.execute(
        select(
            func.count(PullRequestReview.id),
            func.count(PullRequestReview.id).filter(
                PullRequestReview.status == PRReviewStatusEnum.PASSED,
                PullRequestReview.merge_blocked.is_(False),
            ),
            func.count(PullRequestReview.id).filter(PullRequestReview.merge_blocked.is_(True)),
            func.coalesce(func.sum(PullRequestReview.issues_caught_critical), 0),
            func.coalesce(func.sum(PullRequestReview.issues_caught_high), 0),
        ).where(PullRequestReview.organization_id == tenant.organization.id)
    )
    total, clean, blocking, critical, high = result.one()
    return PRReviewMetrics(
        total=int(total),
        clean=int(clean),
        blocking=int(blocking),
        issues_critical=int(critical),
        issues_high=int(high),
    )


@router.post(
    "/api/v1/pr-reviews/{review_id}/analyze",
    response_model=PRReviewResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[AdminRequired],
)
async def analyze_pull_request_review(
    review_id: UUID,
    tenant: TenantDependency,
    session: SessionDependency,
    dispatch: ReviewDispatchDependency,
) -> PRReviewResponse:
    """Pide el análisis de seguridad de una revisión de pull request.

    ## Por qué es `202` y no `201`

    Porque el recurso que se crea —el trabajo encolado— no es la revisión: la revisión **ya
    existía** y solo cambia de estado. Un `201` diría «se creó una revisión nueva», que es
    exactamente lo que no pasó, y un cliente que lo creyera podría insertar la respuesta en su
    lista como una fila más. `202` dice lo que es: la petición se aceptó y el trabajo está en
    curso. La revisión devuelta sale ya en `QUEUED`, así que el panel la pinta como en curso sin
    tener que adivinarlo.

    ## Por qué `AdminRequired` y no un permiso de API token

    Por la misma razón que la sincronización de Supply Chain y que la indexación de manifiestos:
    el pipeline abre un cliente Git con la **credencial del workspace** y lanza un contenedor
    que consume tokens de la plataforma. Quien lo dispara no es un integración de terceros, es la
    persona sentada en el panel. La regla de negocio del cliente no se expone a integraciones.

    ## Por qué devuelve `404` y no `403` cuando no se puede lanzar

    Porque hay dos motivos muy distintos —no existe, está en curso, el repositorio está
    desconectado— y `403` confirmaría que el `review_id` existe en algún sitio, que es lo
    contrario de lo que R3 permite. El motivo concreto va en el registro del servidor; el panel
    recibe «no se puede lanzar» y el usuario ya sabe que tiene que refrescar o esperar.
    """

    try:
        review = await reviews.lanzar_analisis_de_review(
            session,
            organization_id=tenant.organization.id,
            review_id=review_id,
            dispatch=dispatch,
        )
    except reviews.ReviewDispatchError as error:
        # El encolado es infraestructura, no la petición del usuario: la revisión era válida y
        # lo que no pudo atenderla fue la cola. Es un `503` con `Retry-After`, el mismo trato que
        # recibe un pentest que no se pudo encolar, para que el panel sepa que reintentar tiene
        # sentido.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo encolar el análisis de la revisión",
            headers={"Retry-After": "30"},
        ) from error
    except reviews.ReviewNotLaunchableError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="La revisión no existe o no se puede lanzar ahora mismo",
        ) from error

    nombre = await session.execute(
        select(Repository.full_name).where(Repository.id == review.repository_id)
    )
    return PRReviewResponse.from_review(review, repository_name=nombre.scalar_one())


@router.get("/api/v1/repositories/{repository_id}", response_model=RepositoryResponse)
async def get_repository(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
) -> RepositoryResponse:
    """Devuelve un repositorio únicamente si pertenece a la organización activa."""

    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    return RepositoryResponse.from_repository(repository)


@router.patch(
    "/api/v1/repositories/{repository_id}",
    response_model=RepositoryResponse,
    dependencies=[ManagementRateLimit],
)
async def update_repository(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
    payload: RepositoryUpdateRequest,
) -> RepositoryResponse:
    """Actualiza la política de revisiones y la rama por defecto del repositorio."""

    await _require_admin(tenant)
    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    if payload.pr_reviews_enabled is not None:
        repository.pr_reviews_enabled = payload.pr_reviews_enabled
    if payload.default_branch is not None:
        repository.default_branch = payload.default_branch
    if payload.is_active is not None:
        repository.is_active = payload.is_active
    await session.commit()
    await session.refresh(repository)
    return RepositoryResponse.from_repository(repository)


@router.delete(
    "/api/v1/repositories/{repository_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ManagementRateLimit],
)
async def delete_repository(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
) -> Response:
    """Desvincula el repositorio y elimina el webhook en el proveedor si es posible."""

    await _require_admin(tenant)
    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    in_progress_result = await session.execute(
        select(func.count())
        .select_from(PullRequestReview)
        .where(
            PullRequestReview.repository_id == repository.id,
            PullRequestReview.organization_id == tenant.organization.id,
            PullRequestReview.status.in_(_IN_PROGRESS_REVIEW_STATUSES),
        )
    )
    if int(in_progress_result.scalar_one()) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede desvincular un repositorio con revisiones en curso",
        )
    if repository.webhook_id is not None:
        client = await _try_open_client(
            session,
            tenant.organization.id,
            repository.provider,
        )
        if client is None:
            logger.warning(
                "Sin credencial utilizable para eliminar el webhook: provider=%s repository=%s",
                repository.provider.value,
                repository.full_name,
            )
        else:
            try:
                await asyncio.to_thread(
                    client.delete_webhook,
                    repository.full_name,
                    repository.webhook_id,
                )
            except GitClientError as error:
                logger.warning(
                    "No se pudo eliminar el webhook remoto: provider=%s repository=%s reason=%s",
                    repository.provider.value,
                    repository.full_name,
                    str(error),
                )
            finally:
                client.close()
    await session.delete(repository)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _load_tenant_repository(
    session: AsyncSession,
    organization_id: UUID,
    repository_id: UUID,
) -> Repository:
    """Carga un repositorio acotado al tenant; 404 si pertenece a otra organización."""

    result = await session.execute(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.organization_id == organization_id,
        )
    )
    repository = result.scalar_one_or_none()
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repositorio no encontrado",
        )
    return repository
