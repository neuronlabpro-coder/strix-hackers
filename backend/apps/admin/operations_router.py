"""Consola de operaciones: escaneos, revisiones, contenedores y sondas, de toda la plataforma.

## Por qué existe esto y no es «otra vista del admin»

Porque hay un caso que ninguna otra sección cubre: **un trabajo que se ha quedado pillado**. Un
escaneo lleva cuarenta minutos en `RUNNING`, con su contenedor vivo y su reserva de créditos
ya cobrada. Nadie lo va a cerrar desde el panel del cliente, porque el panel del cliente no
enseña ese run: el run es suyo, pero la decisión de pararlo es de la plataforma.

Y hasta ahora no había forma de hacerlo desde la consola. `abort_pentest` existe, pero está
atada a `tenant.organization.id`: solo se puede abortar un escaneo desde el panel de su propio
cliente. Un operador de plataforma con cinco mil organizaciones delante no tenía ninguna puerta.

## Por qué estas rutas **no** filtran por organización

Porque son de alcance plataforma por definición: el caso que las trae —«este proceso lleva una
hora colgado»— no tiene organización que lo caracterice, y filtrar por ella convertiría la
función en tener que saber primero de quién es el run colgado, que es justo lo que no se sabe.

Y por eso la respuesta de cada lista trae el **nombre** de la organización: el operador no busca
por tenant, mira el problema y necesita saber a quién pertenece.

## Por qué cancelar decide el dinero y el motivo lo pone el operador

Porque un proceso colgado y una cancelación del cliente terminan en el mismo estado, y solo quien
pulsa el botón sabe cuál es cuál. La regla vive en `pentests.abort_reason`: los motivos
`INFRASTRUCTURE_*` devuelven la reserva, los demás cobran. Está en el módulo y no aquí porque es
una regla de negocio con seis valores, no una decisión de una ruta.

## Por qué no hay borrados

Por R4, y por operativa. Un run cancelado se queda con su evidencia, su ledger y su historial: es
lo que permite explicar dentro de seis meses por qué ese cobro fue el que fue. Lo que se puede es
**cancelar**, que marca `ABORTED` y deja la fila. Eliminar un run sería borrar un cobro.

## Por qué el contenedor no tiene entidad propia

Porque no la tiene en el sistema: el contenedor se deriva de forma determinista del `run_id`
(`fenix-strix-{run_id}`) y su referencia vive en `pentest_runs.container_id`. La sección de
contenedores es una **vista sobre los runs**, y por eso no puede tener datos que el run no tenga.

Y por eso arreglar un contenedor que no tiene run es imposible: sin `run_id` no hay nombre
determinista. Lo que sí hay es un run en `RUNNING` con `container_id` puesto, o con
`cleanup_pending`, y esos son los que aparecen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from backend.apps.admin.dependencies import SuperuserDependency
from backend.apps.agents.models import AgentJob, AgentJobStatusEnum
from backend.apps.organizations.models import Organization
from backend.apps.pentests.abort import AbortCleanupPendingError, abortar_run
from backend.apps.pentests.abort_reason import AbortReasonEnum
from backend.apps.pentests.models import PentestRun, ScanStatusEnum
from backend.apps.repositories.models import PRReviewStatusEnum, PullRequestReview
from backend.core.filtros_texto import coincide
from backend.core.middleware import SessionDependency

router = APIRouter(prefix="/api/v1/admin/operations", tags=["admin-operations"])


# --------------------------------------------------------------------------- #
# Esquemas
# --------------------------------------------------------------------------- #


class ScanOperacionItem(BaseModel):
    """Un escaneo, con el nombre de su organización.

    ## Por qué el nombre va dentro del ítem y no en un campo aparte

    Porque la lista **se ordena por el problema**, no por el cliente: el operador mira una lista
    de runs y necesita leer «acme, 40 minutos, RUNNING» de un vistazo. Con el nombre en una
    columna aparte habría que cruzarlo mentalmente con cada fila.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    organizacion: str
    status: ScanStatusEnum
    scan_mode: str
    target_type: str
    target_identifier: str
    container_id: str | None
    cleanup_pending: bool
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    #: Minutos que lleva vivo si sigue en curso, o que estuvo si ya terminó. Es lo que hace
    #: ordenable la lista: un run de cuarenta minutos tiene que salir arriba sin que nadie tenga
    #: que restar dos columnas mentalmente.
    duracion_minutos: Decimal


class ScanOperacionPage(BaseModel):
    items: list[ScanOperacionItem]
    total: int
    limit: int
    offset: int


class CancelarScanRequest(BaseModel):
    """Por qué se cancela. Es lo que decide si se devuelve el dinero."""

    model_config = ConfigDict(extra="forbid")

    motivo: AbortReasonEnum
    #: Explicación en palabras, para el registro. Obligatoria: el enum dice **qué clase** de
    #: motivo es, y no por qué pasó en este caso concreto.
    nota: str = Field(min_length=3, max_length=255)


class CancelarScanResponse(BaseModel):
    """Qué pasó, incluido el dinero.

    ## Por qué el importe devuelto va en la respuesta y no solo en el ledger

    ## Por qué dice «no aplica» en vez de `0`

    Porque el operador necesita **confirmar** lo que pasó antes de cerrar el diálogo. Si la
    respuesta solo dijera «cancelado», no habría forma de saber si se devuelven 10 créditos o
    10.000, y esa es justo la información que hace falta para no comunicar al cliente algo que no
    es cierto.

    Y dice «no aplica» en vez de `0` porque `None` y `0` significan cosas distintas: `0` es
    «se cobró y no había nada que devolver», `None` es «este motivo no devuelve». Confundirlos
    haría que un operador creyera que se ha devuelto algo cuando no.
    """

    id: uuid.UUID
    status: ScanStatusEnum
    motivo: AbortReasonEnum
    tarea_revocada: bool
    limpieza_pendiente: bool
    devuelto_creditos: Decimal | None
    nota: str


class ReviewOperacionItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    organizacion: str
    status: PRReviewStatusEnum
    pr_number: int
    pr_title: str | None
    source_branch: str | None
    target_branch: str | None
    issues_caught_critical: int
    issues_caught_high: int
    merge_blocked: bool
    run_id: uuid.UUID | None
    created_at: datetime
    finished_at: datetime | None


class ReviewOperacionPage(BaseModel):
    items: list[ReviewOperacionItem]
    total: int
    limit: int
    offset: int


class ContenedorItem(BaseModel):
    """Un contenedor vivo, deducido del run que lo creó.

    ## Por qué no tiene `id` propio

    ## Por qué el contenedor se identifica por su `run_id`

    Porque no hay tabla de contenedores: el nombre se deriva del `run_id` y la referencia
    guardada está en `pentest_runs.container_id`. Un `id` inventado aquí sería una cosa que
    existe en la pantalla y no existe en el sistema, y tarde o temprano alguien intentaría usarlo
    contra la API.
    """

    run_id: uuid.UUID
    organization_id: uuid.UUID
    organizacion: str
    container_id: str
    nombre_esperado: str
    status: ScanStatusEnum
    cleanup_pending: bool
    started_at: datetime | None


class ContenedorPage(BaseModel):
    items: list[ContenedorItem]
    total: int
    limit: int
    offset: int


class JobOperacionItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    organizacion: str
    kind: str
    target: str
    status: AgentJobStatusEnum
    agent_id: uuid.UUID | None
    lease_expires_at: datetime | None
    attempt_count: int
    error_message: str | None
    created_at: datetime


class JobOperacionPage(BaseModel):
    items: list[JobOperacionItem]
    total: int
    limit: int
    offset: int


class ReconciliarResponse(BaseModel):
    """Cuántos procesos huérfanos se han limpiado."""

    runs_tocados: int


# --------------------------------------------------------------------------- #
# Utilidades compartidas
# --------------------------------------------------------------------------- #


def _duracion_minutos(run: PentestRun) -> Decimal:
    """Minutos que lleva vivo el run, o que estuvo si ya terminó.

    ## Por qué se calcula aquí y no en la base

    Porque la lista tiene que ordenar **por lo que molesta**, que es «lleva cuarenta minutos
    colgado». Es un cálculo sobre dos columnas y una NOW() que cambia entre filas de la misma
    consulta, así que dejarlo en SQL daría duraciones distintas para runs leídos en el mismo
    instante —irrelevante para ordenar, pero sí para leer.
    """

    fin = run.finished_at or datetime.now(UTC)
    inicio = run.started_at or run.created_at
    return Decimal(str(max((fin - inicio).total_seconds(), 0) / 60)).quantize(Decimal("0.1"))


# --------------------------------------------------------------------------- #
# Escaneos
# --------------------------------------------------------------------------- #


@router.get("/scans", response_model=ScanOperacionPage)
async def listar_scans(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    estado: Annotated[ScanStatusEnum | None, Query()] = None,
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
    solo_colgados: Annotated[bool, Query()] = False,
    busqueda: Annotated[str | None, Query(max_length=120)] = None,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
    desplazamiento: Annotated[int, Query(ge=0)] = 0,
) -> ScanOperacionPage:
    """Escaneos de toda la plataforma, ordenados por cuánto llevan vivos.

    ## Por qué `solo_colgados` existe y no es un filtro de estado

    Porque «los que están mal» no es un estado: es un run `RUNNING` con `started_at` viejo, o un
    `QUEUED` que no se mueve, o un `RUNNING` con `cleanup_pending` de una limpieza anterior que
    falló. Los tres están vivos y ninguno tiene un estado que los agrupe. El parámetro los
    junta, que es la pregunta que el operador se hace al abrir la pantalla.

    Y el umbral son los **300 segundos** que el watchdog ya usa para declarar obsoleto un run, no
    un número inventado: si coincide con el del watchdog, lo que se ve en la pantalla es
    exactamente lo que el watchdog va a hacer, y no una lista de runs que él todavía no ha tocado.

    ## Por qué el desempate va sobre `PentestRun.id`

    Porque `started_at` es **anulable** y `NULL` significa «en cola, sin empezar todavía». Con
    `ORDER BY started_at ASC NULLS FIRST` todos los runs en cola comparten valor de orden —no
    empatan por casualidad: empatan por definición, porque todos están en `NULL`—, así que sin
    un segundo criterio su orden relativo lo decide el planificador. Y esta vista **sí** está
    paginada, así que el reparto de ese empate entre las páginas también lo decide el
    planificador: la página 2 repite filas de la 1 y se come otras, sin que nada lo indique.

    Conviene ser preciso sobre lo que el desempate arregla y lo que no, porque son cosas
    distintas y confundirlas lleva a prometer de más:

    - **`NULLS FIRST` se queda.** Es deliberado: un run en cola lleva esperando desde
      `created_at` y es de lo más urgente que el operador busca, así que va primero. Y no pierde
      ninguna fila: con `limit`/`offset` la fila sale en la página que le toca. Lo que pasa es
      que *cuál* página, sin desempate, no está decidido.
    - **El desempate sí cierra el orden.** `started_at ASC NULLS FIRST, id ASC` es un orden
      **total**: los nulos van delante por la regla del `NULLS FIRST` y, dentro de ese grupo, los
      desempata `id`. No hace falta nada más: no hay que cambiar el `NULLS FIRST` ni quitar los
      nulos de la primera página, porque el problema nunca fue *qué* filas salen, sino que su
      orden dentro de cada página no dependía solo de los valores de las columnas.

    El desempate es sobre la clave primaria de la tabla que se pagina. El `aliased` de
    `Organization` se une por su clave primaria, así que es de uno a uno y no multiplica filas.
    No cambia qué filas se devuelven, solo el orden entre las que ya se devolvían, y no mueve el
    filtro: cuando el operador manda `organization_id`, sigue yendo al `WHERE` (R3).
    """

    organizacion = aliased(Organization)
    base = select(PentestRun, organizacion.name).join(
        organizacion, PentestRun.organization_id == organizacion.id
    )
    conteo = select(func.count()).select_from(PentestRun)

    if estado is not None:
        base = base.where(PentestRun.status == estado)
        conteo = conteo.where(PentestRun.status == estado)
    if organization_id is not None:
        base = base.where(PentestRun.organization_id == organization_id)
        conteo = conteo.where(PentestRun.organization_id == organization_id)
    if busqueda is not None:
        # `coincide` escapa los comodines de `LIKE`. Sin ese escape, `busqueda=%` devuelve
        # todos los escaneos de la plataforma —el comodín va también en los dos extremos del
        # patrón— y `busqueda=web_app` también traería `webXapp`. En la consola es el fallo más
        # molesto de los tres sitios donde se cuela, porque el filtro que ha escrito el operador
        # no ha filtrado nada y no hay forma de que lo note: la tabla sale llena y parece correcta.
        condicion_busqueda = coincide([PentestRun.target_identifier], busqueda)
        base = base.where(condicion_busqueda)
        conteo = conteo.where(condicion_busqueda)
    if solo_colgados:
        # Los mismos estados en curso del watchdog, más la limpieza pendiente de un intento
        # anterior, y con el umbral del watchdog.
        limite_vivo = datetime.now(UTC) - timedelta(seconds=300)
        # `&` y no `and`: el `and` de Python evalua `bool()` sobre el primer operando, y una
        # clausula SQLAlchemy no define su valor booleano, asi que revienta con
        # «Boolean value of this clause is not defined». Es un error que solo aparece cuando
        # alguien pasa por `solo_colgados`, que es justo cuando hace falta.
        en_curso = PentestRun.status.in_([ScanStatusEnum.QUEUED, ScanStatusEnum.RUNNING])
        sin_empezar_o_viejo = (PentestRun.started_at.is_(None)) | (
            PentestRun.started_at <= limite_vivo
        )
        condicion_colgado = (en_curso & sin_empezar_o_viejo) | (
            PentestRun.cleanup_pending.is_(True)
        )
        base = base.where(condicion_colgado)
        conteo = conteo.where(condicion_colgado)

    total = (await session.execute(conteo)).scalar_one()
    filas = (
        await session.execute(
            base.order_by(
                PentestRun.started_at.asc().nullsfirst(),
                PentestRun.id.asc(),
            )
            .limit(limite)
            .offset(desplazamiento)
        )
    ).all()

    return ScanOperacionPage(
        items=[
            ScanOperacionItem(
                id=run.id,
                organization_id=run.organization_id,
                organizacion=nombre,
                status=run.status,
                scan_mode=run.scan_mode.value,
                target_type=run.target_type.value,
                target_identifier=run.target_identifier,
                container_id=run.container_id,
                cleanup_pending=run.cleanup_pending,
                error_message=run.error_message,
                started_at=run.started_at,
                finished_at=run.finished_at,
                created_at=run.created_at,
                duracion_minutos=_duracion_minutos(run),
            )
            for run, nombre in filas
        ],
        total=total,
        limit=limite,
        offset=desplazamiento,
    )


@router.post("/scans/{run_id}/cancel", response_model=CancelarScanResponse)
async def cancelar_scan(
    run_id: uuid.UUID,
    payload: CancelarScanRequest,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> CancelarScanResponse:
    """Cancela un escaneo de cualquier organización y devuelve el dinero si el motivo lo obliga.

    ## Por qué el motivo va en el cuerpo y no se deduce

    Porque un run colgado y una cancelación del cliente se ven **exactamente igual**: los dos
    están en `RUNNING`. Si el motivo se dedujera del estado o del tiempo transcurrido, un cliente
    que cancela a los cinco minutos y un proceso que lleva cuarenta colgado acabarían con el
    mismo trato, y uno de los dos está mal. Quien pulsa el botón sabe cuál es cuál.

    ## Por qué el importe devuelto sale en la respuesta

    Para que el operador pueda confirmar lo que pasó antes de cerrar el diálogo, y para que el
    panel pueda comunicarlo al cliente sin inventar nada. `None` significa «este motivo no
    devuelve» y es un valor distinto de `0`.
    """

    run = (
        await session.execute(
            select(PentestRun).where(PentestRun.id == run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Escaneo no encontrado"
        )
    if run.status == ScanStatusEnum.ABORTED:
        # Idempotencia: el segundo intento devuelve lo mismo que el primero en vez de fallar,
        # porque un doble clic en «cancelar» no es un error del operador sino un doble clic.
        return CancelarScanResponse(
            id=run.id,
            status=run.status,
            motivo=payload.motivo,
            tarea_revocada=True,
            limpieza_pendiente=run.cleanup_pending,
            devuelto_creditos=None,
            nota="el escaneo ya estaba cancelado; no se ha vuelto a tocar",
        )
    if run.status not in {ScanStatusEnum.QUEUED, ScanStatusEnum.RUNNING}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El escaneo ya está en un estado terminal ({run.status.value})",
        )

    try:
        resultado = await abortar_run(session, run, motivo=payload.motivo)
    except AbortCleanupPendingError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "El escaneo se ha cancelado, pero su contenedor no se ha podido detener. "
                "Queda una limpieza pendiente que el watchdog recogerá."
            ),
            headers={"Retry-After": "30"},
        ) from None

    return CancelarScanResponse(
        id=resultado.run.id,
        status=resultado.run.status,
        motivo=resultado.motivo,
        tarea_revocada=resultado.tarea_revocada,
        limpieza_pendiente=resultado.run.cleanup_pending,
        devuelto_creditos=resultado.devuelto,
        nota=payload.nota,
    )


class LimpiarContenedorResponse(BaseModel):
    """Qué se ha limpiado de un run, y qué queda vivo.

    ## Por qué el estado del run **no** cambia

    Porque limpiar un contenedor no es cancelar el trabajo. El run puede llevar días en `FAILED`
    o `COMPLETED` —con sus hallazgos, su informe y su evidencia ya emitidos— y lo que sigue vivo
    es el contenedor, la red y el directorio de trabajo. Tocar el estado del run sería reescribir
    un resultado que ya se le entregó al cliente.

    Es la diferencia entre «el análisis se acabó» y «el análisis se acabó y además lo limpié»:
    cosas distintas, y mezclarlas produce un run que dice que terminó y cuyo contenedor sigue
    gastando CPU.
    """

    id: uuid.UUID
    contenedor: str
    #: `True` si el contenedor **no** estaba, que es el caso normal cuando se limpia después de
    #: un fallo: el runner ya lo había parado y lo que quedaba era la red o el directorio.
    contenedor_ya_no_existia: bool
    limpieza_pendiente: bool
    nota: str


def _es_no_encontrado(error: Exception) -> bool:
    """Si el fallo de Docker es «ese contenedor no existe», y no otra cosa.

    ## Por qué se mira la clase y no el texto

    Porque el texto del mensaje **cambia entre versiones de la librería**, y comprobar una cadena
    es la forma más fácil de que un día deje de coincidir y la limpieza pase a fallar sin que nadie
    entienda por qué. `docker.errors.NotFound` es una clase: si la librería la renombra, este
    módulo deja de importar y lo dice el linter, que es donde debe decirlo.
    """

    try:
        from docker.errors import NotFound
    except ImportError:  # pragma: no cover - solo si `docker` no está instalado
        return "No such container" in str(error)
    return isinstance(error, NotFound)


def _motivo_del_fallo(error: Exception) -> str:
    """Por qué no se pudo limpiar, en palabras que le sirven a un operador.

    ## Por qué se distingue «Docker no está» del resto

    ## Por qué Docker no está es un caso aparte

    Porque son dos problema distintos con dos arreglo distintos. Si Docker está parado, el arreglo
    es «arranca Docker Desktop», y ningún reintento del watchdog lo arregla. Si lo que falla es el
    permiso o el contenedor, el arreglo es otro, y la acción sí es reintentar.

    Sin esta distinción el operador lee «no se pudo detener el contenedor» y no sabe si reiniciar
    un servicio o escalar el problema, que es justo la decisión que tiene que tomar.
    """

    if "Docker" in type(error).__name__ or "Docker" in str(error):
        return "el servicio de Docker no está disponible"
    return type(error).__name__


@router.post("/scans/{run_id}/cleanup", response_model=LimpiarContenedorResponse)
async def limpiar_contenedor(
    run_id: uuid.UUID,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> LimpiarContenedorResponse:
    """Mata el contenedor, su red y su directorio de un run, y despeja la limpieza pendiente.

    ## Por qué esta ruta y no «cancelar» para este caso

    Porque un run `FAILED` con `cleanup_pending=True` **ya terminó**, y cancelar un run terminado
    es un `409` correcto: no hay nada que detener. Lo que queda vivo son los recursos, y esta ruta
    los quita. Son dos problemas distintos y el operador los ve distintos en la pantalla: uno se
    cancela, otro se limpia.

    ## Por qué limpiar algo que ya no está no es un error

    Porque **es** el objetivo cumplido, y Docker lanza `NotFound` en ese caso. Si eso fuera un
    `500`, el operador vería un error justo cuando la operación había funcionado, y aprendería a
    no fiarse de la pantalla. El detalle se distingue con `contenedor_ya_no_existia`, que sí se
    devuelve, y que el panel pinta como «ya estaba limpio» y no como fallo.

    Un fallo que **no** es «no existe» —Docker caído, permiso denegado— deja
    `cleanup_pending` intacto y responde `503`, para que el watchdog lo reintente y no se pierda
    la constancia de que había algo pendiente.
    """

    from backend.workers.runner import docker_client as modulo_docker
    from backend.workers.runner.sandbox import StrixSandboxManager

    run = (
        await session.execute(
            select(PentestRun).where(PentestRun.id == run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Escaneo no encontrado"
        )

    nombre = StrixSandboxManager.container_name_for_run(str(run.id))

    # El cliente se crea **dentro** del `try`, no antes.
    #
    # ## Por qué está dentro y no fuera
    #
    # Porque `create_docker_client()` habla con el demonio de Docker, y si el demonio no está
    # —Docker Desktop parado, que es el estado normal de un portátil en desarrollo— lanza
    # `DockerException` ahí mismo. Con la creación fuera del `try`, esa excepción escapaba como un
    # `500` de servidor: el operador veía «error interno» sin ninguna pista de que lo quefallaba
    # era Docker, cuando lo único que necesitaba saber era «arranca Docker».
    ya_no_existia = False
    # Se inicializa a `None` y no se lee hasta después del `try`. Las dos funciones de limpieza
    # aceptan `client=None` y crean el suyo, así que el caso «el cliente no llegó a existir» es
    # un valor legítimo y no un `NameError` esperando a ocurrir.
    cliente = None
    try:
        cliente = modulo_docker.create_docker_client()
        StrixSandboxManager.kill_container(
            nombre, expected_run_id=str(run.id), client=cliente
        )
    except Exception as error:
        if _es_no_encontrado(error):
            ya_no_existia = True
        else:
            # El contenedor sigue vivo, no se pudo tocar, o Docker no está disponible. En los
            # tres casos la marca de limpieza pendiente **no se toca**: es lo que permite al
            # watchdog —y a un operador— volver a intentarlo más tarde.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"No se pudo detener el contenedor {nombre}: {_motivo_del_fallo(error)}. "
                    "Queda una limpieza pendiente."
                ),
                headers={"Retry-After": "30"},
            ) from error

    # La red y el directorio se limpian siempre: no tienen un «no existe» que distinguir, y
    # dejarlos es lo que hace que `/tmp/fenix_workspaces` crezca sin límite.
    StrixSandboxManager.remove_network_for_run(str(run.id), client=cliente)
    StrixSandboxManager.purge_workspace(str(run.id))

    run.cleanup_pending = False
    await session.commit()
    return LimpiarContenedorResponse(
        id=run.id,
        contenedor=nombre,
        contenedor_ya_no_existia=ya_no_existia,
        limpieza_pendiente=False,
        nota=(
            "El contenedor ya no estaba; se han limpiado la red y el directorio."
            if ya_no_existia
            else "Contenedor, red y directorio eliminados."
        ),
    )


# --------------------------------------------------------------------------- #
# Contenedores
# --------------------------------------------------------------------------- #


@router.get("/containers", response_model=ContenedorPage)
async def listar_contenedores(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
    desplazamiento: Annotated[int, Query(ge=0)] = 0,
) -> ContenedorPage:
    """Contenedores vivos o con limpieza pendiente, deducidos de los runs que los crearon.

    ## Por qué la lista se limita a `RUNNING` y `QUEUED`

    Porque un contenedor de un run terminado **no debería existir**. Si existe, es que una
    limpieza falló, y ese run tiene `cleanup_pending=True`, que es justo el segundo caso de la
    consulta. Mostrar los contenedores de runs ya cerrados sería mostrar trabajo ya terminado,
    que es ruido en una pantalla cuyo propósito es encontrar lo que está ocupando recursos.

    ## Por qué el desempate va sobre `PentestRun.id`

    Por lo mismo que en los escaneos, y aquí el caso es todavía más limpio porque **esta lista
    solo contiene runs en curso**: `QUEUED` y `RUNNING`. Un run `QUEUED` tiene `started_at` a
    `NULL` por definición —todavía no ha empezado—, así que el grupo de nulos no es una
    coincidencia de la base de datos: es una propiedad de la lista. Y como `NULLS FIRST` los
    pone delante, ese grupo es la primera página casi siempre.

    Con el `ORDER BY` solo, el orden dentro del grupo de nulos lo decide el planificador, y como
    la vista está paginada la página 2 puede repetir filas de la 1 y perder otras. El desempate
    sobre la clave primaria cierra el orden sin cambiar el `NULLS FIRST` y sin tocar el `WHERE`.

    Esta vista **no** filtra por organización, y es deliberado: un contenedor vivo sin saber de
    quién es es exactamente el caso que el operador tiene delante, y filtrar por tenant lo
    escondería detrás de un filtro que no tiene por qué conocer.
    """

    from backend.workers.runner.sandbox import StrixSandboxManager

    organizacion = aliased(Organization)
    total = await session.execute(
        select(func.count()).select_from(PentestRun).where(
            PentestRun.status.in_([ScanStatusEnum.QUEUED, ScanStatusEnum.RUNNING])
        )
    )
    filas = (
        await session.execute(
            select(PentestRun, organizacion.name)
            .join(organizacion, PentestRun.organization_id == organizacion.id)
            .where(PentestRun.status.in_([ScanStatusEnum.QUEUED, ScanStatusEnum.RUNNING]))
            .order_by(
                PentestRun.started_at.asc().nullsfirst(),
                PentestRun.id.asc(),
            )
            .limit(limite)
            .offset(desplazamiento)
        )
    ).all()

    return ContenedorPage(
        items=[
            ContenedorItem(
                run_id=run.id,
                organization_id=run.organization_id,
                organizacion=nombre,
                container_id=run.container_id or StrixSandboxManager.container_name_for_run(
                    str(run.id)
                ),
                nombre_esperado=StrixSandboxManager.container_name_for_run(str(run.id)),
                status=run.status,
                cleanup_pending=run.cleanup_pending,
                started_at=run.started_at,
            )
            for run, nombre in filas
        ],
        total=total.scalar_one(),
        limit=limite,
        offset=desplazamiento,
    )


# --------------------------------------------------------------------------- #
# Revisiones de pull request
# --------------------------------------------------------------------------- #


@router.get("/reviews", response_model=ReviewOperacionPage)
async def listar_reviews(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    estado: Annotated[PRReviewStatusEnum | None, Query()] = None,
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
    desplazamiento: Annotated[int, Query(ge=0)] = 0,
) -> ReviewOperacionPage:
    """Revisiones de PR de toda la plataforma.

    ## Por qué no se puede cancelar todavía desde aquí

    Porque una revisión **no tiene contenedor propio**: el `container_id` no se persiste para
    revisiones, se deduce del `run_id` que creó, y el identificador de la tarea de Celery vive en
    ese run. Cancelar una revisión es, exactamente, abortar su run —y la ruta de aborto ya está
    en `/scans/{id}/cancel`—, pero la revisión hay que moverla a `ERROR` en la misma operación.

    Esa parte está escrita y verificada en `pentests.abort`, porque `_mark_linked_review_error`
    ya existe y `abortar_run` la llama. Lo que falta es el endpoint propio de revisión, que
    resuelve `review → run → abortar` y solo se diferencia en buscar por `review_id`.

    ## Por qué el desempate va sobre `PullRequestReview.id`

    Porque `created_at` no es único, y esta vista **sí** está paginada: dos revisiones
    recientes pueden compartir marca —`created_at` es `now()` de servidor, y dos revisiones
    que entran en la misma transacción lo hacen— y entonces la página 2 repite filas de la 1
    y se come otras. El operador pasa de una a otra y ve duplicados y huecos sin que nada lo
    indique.

    El desempate es sobre la clave primaria de la tabla que se pagina. El `aliased` de
    `Organization` se une por su clave primaria, o sea que es de uno a uno y no multiplica
    filas: `PullRequestReview.id` sigue identificando cada fila de la salida sin ambigüedad.

    El filtro por `organization_id` no se ha movido: cuando el operador lo manda, sigue yendo
    al `WHERE`; cuando no lo manda, esta vista es deliberadamente global, como las demás de la
    consola.
    """

    organizacion = aliased(Organization)
    base = select(PullRequestReview, organizacion.name).join(
        organizacion, PullRequestReview.organization_id == organizacion.id
    )
    conteo = select(func.count()).select_from(PullRequestReview)

    if estado is not None:
        base = base.where(PullRequestReview.status == estado)
        conteo = conteo.where(PullRequestReview.status == estado)
    if organization_id is not None:
        base = base.where(PullRequestReview.organization_id == organization_id)
        conteo = conteo.where(PullRequestReview.organization_id == organization_id)

    total = (await session.execute(conteo)).scalar_one()
    filas = (
        await session.execute(
            base.order_by(PullRequestReview.created_at.desc(), PullRequestReview.id.desc())
            .limit(limite)
            .offset(desplazamiento)
        )
    ).all()

    return ReviewOperacionPage(
        items=[
            ReviewOperacionItem(
                id=revision.id,
                organization_id=revision.organization_id,
                organizacion=nombre,
                status=revision.status,
                pr_number=revision.pr_number,
                pr_title=revision.pr_title,
                source_branch=revision.source_branch,
                target_branch=revision.target_branch,
                issues_caught_critical=revision.issues_caught_critical,
                issues_caught_high=revision.issues_caught_high,
                merge_blocked=revision.merge_blocked,
                run_id=revision.run_id,
                created_at=revision.created_at,
                finished_at=revision.finished_at,
            )
            for revision, nombre in filas
        ],
        total=total,
        limit=limite,
        offset=desplazamiento,
    )


# --------------------------------------------------------------------------- #
# Sondas y trabajos de agente
# --------------------------------------------------------------------------- #


@router.get("/jobs", response_model=JobOperacionPage)
async def listar_jobs(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    estado: Annotated[AgentJobStatusEnum | None, Query()] = None,
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
    desplazamiento: Annotated[int, Query(ge=0)] = 0,
) -> JobOperacionPage:
    """Trabajos de las sondas, de toda la plataforma.

    ## Por qué los trabajos en curso son los que se atascan

    Porque un trabajo `QUEUED` esperando turno es normal, y uno `RUNNING` con un `lease_expires_at`
    pasado es una sonda que se quedó sin conexión a mitad. El alquiler es lo que permite
    distinguir los dos: `reponer_alquileres` devuelve a la cola los que caducaron, y esa función
    ya existe y es la vía de desatascarlos sin tocar nada más.

    ## Por qué el desempate va sobre `AgentJob.id`

    Por la misma razón que en las revisiones de esta consola, y aquí es todavía más grave:
    los trabajos **nacen a ráfaga**. Un webhook que llega con cinco eventos encola cinco
    trabajos, y como `created_at` es `now()` de servidor, esos cinco comparten marca. Con
    `ORDER BY created_at DESC` y nada más, el reparto entre las páginas lo decide el
    planificador, y el operador ve la misma cola con filas duplicadas y huecos según por
    dónde pase.

    El desempate es sobre `AgentJob.id`, la clave primaria de la tabla que se pagina. El
    `aliased` de `Organization` se une por su clave primaria, así que no multiplica filas.
    """

    organizacion = aliased(Organization)
    base = select(AgentJob, organizacion.name).join(
        organizacion, AgentJob.organization_id == organizacion.id
    )
    conteo = select(func.count()).select_from(AgentJob)

    if estado is not None:
        base = base.where(AgentJob.status == estado)
        conteo = conteo.where(AgentJob.status == estado)
    if organization_id is not None:
        base = base.where(AgentJob.organization_id == organization_id)
        conteo = conteo.where(AgentJob.organization_id == organization_id)

    total = (await session.execute(conteo)).scalar_one()
    filas = (
        await session.execute(
            base.order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
            .limit(limite)
            .offset(desplazamiento)
        )
    ).all()

    return JobOperacionPage(
        items=[
            JobOperacionItem(
                id=job.id,
                organization_id=job.organization_id,
                organizacion=nombre,
                kind=job.kind.value,
                target=job.target,
                status=job.status,
                agent_id=job.agent_id,
                lease_expires_at=job.lease_expires_at,
                attempt_count=job.attempt_count,
                error_message=job.error_message,
                created_at=job.created_at,
            )
            for job, nombre in filas
        ],
        total=total,
        limit=limite,
        offset=desplazamiento,
    )
