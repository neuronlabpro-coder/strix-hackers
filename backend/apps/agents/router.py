"""Los dos lados de la cola de escaneos, en un solo router.

## Por qué viven juntos

Porque hablan de la misma cola y el mismo par de credenciales, y separarlos en dos ficheros no
habria añadido ninguna garantia: lo que impide que un token de agente llegue a un endpoint de
panel es que `get_current_agent` resuelve a `ScannerAgent` y **no puede producir un
`TenantContext`**, y eso es una propiedad del tipo de la funcion de autenticacion, no del router
en el que viva.

## Por que el lado del cliente no lleva control de rol

Porque el token de agente no tiene rol: no es una sesion de navegador, no pertenece a un
`Member`, y no hay `Membership` que mirar. El control de rol aplica al **lado del cliente**, que
es donde hay un `tenant` y por tanto un rol.

## Lo que se exigia y no se exigia

Tres controles faltaban aqui y los tres existen ya en el resto del producto: el rol, el limite de
tasa y el cobro. Se anaden en las cuatro rutas de escritura —alta, baja, encolar y reponer— y
**no** en las de lectura, porque leer los escaneos es de cualquier miembro y quien paga tiene
que ver lo que ha pedido.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from backend.apps.agents import service
from backend.apps.agents.auth import AgentDependency
from backend.apps.agents.models import AgentJob, AgentJobKindEnum, AgentJobStatusEnum, ScannerAgent
from backend.apps.agents.schemas import (
    AgentCreate,
    AgentEnrolled,
    AgentHeartbeat,
    AgentItem,
    AgentJobClaimed,
    AgentJobDetail,
    AgentJobItem,
    AgentJobPage,
    AgentJobReport,
    AgentJobRequest,
    AgentPage,
    AgentSummary,
    SistemaObjetivoEnum,
)
from backend.apps.agents.service import (
    AgentNotFoundError,
    JobNotClaimableError,
    entidad_inexistente,
    preservar_evidencia,
)
from backend.apps.billing.service import InsufficientCreditsError
from backend.apps.organizations.models import RoleEnum
from backend.core.middleware import SessionDependency, TenantContext, get_current_tenant
from backend.core.rate_limit import (
    enforce_agent_enrollment_rate_limit,
    enforce_scan_rate_limit,
)

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]


async def _exigir_admin(tenant: TenantDependency) -> None:
    """Falla cerrado si quien llama no es `ADMIN` del workspace.

    ## Por que hace falta aqui y no lo habia

    Porque este modulo era el unico camino de escritura del producto sin capa de autorizacion de
    funcion: el aislamiento por tenant estaba impecable —el `organization_id` va dentro del
    `WHERE`—, pero **cualquier miembro** podia dar de alta un agente y encolar escaneos.

    Y el alta de un agente no es una operacion cualquiera: emite una credencial de 32 bytes que da
    acceso a la red interna del cliente, y esa credencial no la puede reemitir quien no sea el
    cliente. Emitirla, revocarla y reponer la cola son las tres acciones que `repositories`,
    `knowledge` y `supply_chain` ya exigen a un `ADMIN`; aqui faltaban las tres.

    ## Por que solo en las de escribir

    Porque **leer** los escaneos y el estado de los agentes es de cualquier miembro, y no hay
    razon para quitarlo: quien paga necesita ver lo que ha pedido. El limite de tasa cubre el
    abuso de la lectura sin quitar el acceso legitimo.

    ## Por que un `403` y no un `404`

    Porque aqui no hay nada que no exista: la ruta existe, la accion esta clara y el rol es del
    propio llamante, no de un recurso ajeno. Ocultar la existencia de la operacion seria
    theatrical —el `403` dice exactamente que falta— y el proyecto reserva el `404` para no
    confirmar que un recurso de otro tenant existe.

    ## Por que `async` y por que `TenantDependency` y no `TenantContext`

    Porque es una **sub-dependencia**, y FastAPI la reanaliza: al declarar el parametro con la
    clase suelta en vez de con el alias, `TenantContext` —un dataclass congelado con `slots`— se
    interpreta como un tipo de campo de respuesta y la aplicacion **no arranca**. Es el mismo
    motivo por el que `enforce_pentest_rate_limit` declara `tenant: TenantDependency` y no
    `tenant: TenantContext`, y no es una rareza de FastAPI sino su forma de resolver anotaciones
    de dependencias anidadas.

    Se escribe `async` por la misma razon que las demas sub-dependencias del proyecto: la firma
    tiene que ser un inspectable con los parametros que FastAPI debe resolver, y la sincronia
    no aporta nada a una comprobacion de rol que solo lee.
    """

    if tenant.role is not RoleEnum.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador del workspace",
        )


#: Los tres controles que este modulo no tenia, declarados una vez y usados por firma.
#:
#: ## Por que en la **firma** y no en `dependencies=[...]`
#:
#: Porque las dos formas son equivalentes para FastAPI, y la de la firma tiene una propiedad que
#: la otra no: al leer la ruta se ve que exige admin. Con `dependencies=[Depends(...)]` la
#: comprobacion vive en el decorador, una linea por encima de la firma, y en un router con veinte
#: rutas es exactamente el sitio donde se olvida una.
#:
#: Y `RepositoryManagementRateLimit` del modulo de repositorios ya usa este patron, asi que no es
#: una convencion nueva: es la que ya habia en la mitad buena del proyecto.
AdminRequired = Annotated[None, Depends(_exigir_admin)]
ScanRateLimit = Annotated[None, Depends(enforce_scan_rate_limit)]
EnrollmentRateLimit = Annotated[None, Depends(enforce_agent_enrollment_rate_limit)]


PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


# --------------------------------------------------------------------------- #
# Lado del cliente
# --------------------------------------------------------------------------- #


@router.post("", response_model=AgentEnrolled, status_code=status.HTTP_201_CREATED)
async def inscribir(
    payload: AgentCreate,
    tenant: TenantDependency,
    session: SessionDependency,
    _admin: AdminRequired,
    _limite: EnrollmentRateLimit,
) -> AgentEnrolled:
    """Da de alta un agente y devuelve su token **una sola vez**.

    El token viene en el cuerpo y no hay ningún endpoint que lo vuelva a entregar. Por eso la
    respuesta se llama `AgentEnrolled` y no `AgentItem`: quien la lee tiene que entender que está
    viendo algo que no se puede recuperar, y un nombre que no lo dijera invitaría a suponer lo
    contrario.
    """

    agente, token = await service.inscribir_agente(session, tenant.organization.id, payload)
    return AgentEnrolled(
        id=agente.id,
        name=agente.name,
        token=token,
        token_prefix=agente.token_prefix,
        status=agente.status,
        enrolled_at=agente.enrolled_at,
        agent_version=agente.agent_version,
        platform_hint=agente.platform_hint,
        sistema_objetivo=SistemaObjetivoEnum(agente.sistema_objetivo),
    )


@router.get("", response_model=AgentPage)
async def listar(
    tenant: TenantDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgentPage:
    """Los agentes del tenant, vivos y dados de baja.

    Los dados de baja se listan y no se ocultan: la pregunta de un operador que ve un escaneo
    parado es si el agente que lo hacía sigue dado de alta, y esconder los revocados obliga a
    adivinar por la ausencia. El token no se devuelve nunca; solo su prefijo visible, que es lo
    justo para distinguir dos agentes en la lista.

    ## Por qué el desempate por `id`

    Porque `enrolled_at` es `server_default=func.now()` y no es único: dos agentes dados de
    alta en la misma transacción comparten marca. Sin un segundo criterio, el orden dentro de
    ese grupo lo decide el planificador, y como esta lista **sí** está paginada, la página 2
    puede repetir filas de la página 1 y perder otras sin que nada lo indique.

    El desempate va sobre `ScannerAgent.id`, la clave primaria de la tabla que se pagina: la
    consulta no tiene `JOIN`, así que `id` identifica cada fila de la salida sin ambigüedad.
    No cambia qué filas se devuelven, solo su orden, y el filtro por organización sigue siendo
    el primer `WHERE` (R3).
    """

    total = int(
        (
            await session.execute(
                select(func.count(ScannerAgent.id)).where(
                    ScannerAgent.organization_id == tenant.organization.id
                )
            )
        ).scalar_one()
    )
    agentes = (
        (
            await session.execute(
                select(ScannerAgent)
                .where(ScannerAgent.organization_id == tenant.organization.id)
                .order_by(ScannerAgent.enrolled_at.desc(), ScannerAgent.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return AgentPage(
        items=[AgentItem.model_validate(agente) for agente in agentes],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/{agent_id}/revoke", response_model=AgentItem)
async def revocar(
    agent_id: uuid.UUID,
    tenant: TenantDependency,
    session: SessionDependency,
    _admin: AdminRequired,
    motivo: Annotated[str | None, Query(max_length=255)] = None,
) -> AgentItem:
    """Da de baja un agente. No borra su historial ni los trabajos que hizo.

    El `404` y no el `403` por la razón de siempre: un `403` confirmaría que ese identificador
    existe en alguna parte, que es justo lo que R3 no deja revelar.
    """

    try:
        agente = await service.revocar_agente(
            session, tenant.organization.id, agent_id, motivo
        )
    except AgentNotFoundError as error:
        raise entidad_inexistente() from error
    return AgentItem.model_validate(agente)


# --------------------------------------------------------------------------- #
# Lado del cliente: los trabajos
# --------------------------------------------------------------------------- #


@router.post("/jobs", response_model=AgentJobItem, status_code=status.HTTP_202_ACCEPTED)
async def encolar(
    payload: AgentJobRequest,
    tenant: TenantDependency,
    session: SessionDependency,
    _admin: AdminRequired,
    _limite: ScanRateLimit,
) -> AgentJobItem:
    """Encola un escaneo. `202` y no `201` porque todavía no ha pasado nada.

    El trabajo no lo ejecuta este proceso: lo recoge un agente de la red del cliente, que puede
    no existir todavía. Un `201` afirmaría que el escaneo está en marcha, y lo único cierto es que
    está en una cola. La respuesta lleva `status: QUEUED` para que quien la lea vea la
    diferencia.
    """

    # El `402` y no la excepción cruda.
    #
    # ## Por qué hace falta, y qué pasaba antes
    #
    # Porque `apply_credit_delta` lanza `InsufficientCreditsError`, y sin este `except` esa
    # excepción sale del handler tal cual: un `500` con una traza en el log. Y un `500` por saldo
    # insuficiente dice tres cosas falsas a la vez —que el servidor falló, que probablemente sea
    # temporal, y que no hay nada que hacer—, cuando lo cierto es que hay una acción concreta:
    # recargar.
    #
    # El mensaje lleva el coste y el saldo disponible porque es lo que necesita alguien que está
    # decidiendo si recarga o cambia de plan. Es el mismo texto que usa `pentests`, y se escribe
    # dos veces porque los dos routers no comparten capa de errores: un `handler` global para esto
    # sería más arquitectónico y más difícil de leer que el `except` que lo resuelve.
    from backend.apps.commercial.service import feature_enabled

    feature_key = (
        "container_scanning"
        if payload.kind is AgentJobKindEnum.CONTAINER_SCAN
        else "internal_network_scanning"
    )
    if not await feature_enabled(session, tenant.organization.id, feature_key):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Feature no habilitada")
    try:
        trabajo = await service.encolar_trabajo(
            session, tenant.organization.id, tenant.user.id, payload
        )
    except InsufficientCreditsError as error:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Saldo insuficiente: el escaneo cuesta {error.required} créditos y el saldo "
                f"disponible es {error.available}. Recarga en Facturación."
            ),
        ) from error
    return _a_item(trabajo, {})


@router.get("/jobs", response_model=AgentJobPage)
async def listar_trabajos(
    tenant: TenantDependency,
    session: SessionDependency,
    estado: Annotated[AgentJobStatusEnum | None, Query()] = None,
    tipo: Annotated[AgentJobKindEnum | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgentJobPage:
    """Los escaneos del tenant, con su estado y si su evidencia sigue intacta.

    `evidence_intact` se recalcula **al leer**. Es lo que hace que la huella sirva de algo: una
    firma que solo se escribe y nunca se comprueba no protege nada, y aquí se comprueba en cada
    lectura para que un resultado que cambió por debajo se vea en la lista y no en un
    justificante.
    """

    trabajos, total = await service.listar_trabajos(
        session,
        tenant.organization.id,
        estado=estado,
        tipo=tipo,
        limit=limit,
        offset=offset,
    )
    nombres = await service.nombre_de_agentes(
        session, {t.agent_id for t in trabajos if t.agent_id is not None}
    )
    return AgentJobPage(
        items=[_a_item(trabajo, nombres) for trabajo in trabajos],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}", response_model=AgentJobDetail)
async def ver_trabajo(
    job_id: uuid.UUID,
    tenant: TenantDependency,
    session: SessionDependency,
) -> AgentJobDetail:
    """Un escaneo con su resultado completo."""

    trabajo = await service.cargar_trabajo(session, tenant.organization.id, job_id)
    if trabajo is None:
        raise entidad_inexistente()
    nombres = await service.nombre_de_agentes(
        session, {trabajo.agent_id} if trabajo.agent_id else set()
    )
    return _a_detalle(trabajo, nombres)


@router.get("/summary", response_model=AgentSummary)
async def resumen(
    tenant: TenantDependency,
    session: SessionDependency,
) -> AgentSummary:
    """Los números de la cabecera, que alimentan los KPI y los gráficos.

    Van en un endpoint y no se calculan en el navegador porque el origen de los datos de
    contenedor y de red es un `JSONB` que **escribió un agente de otro despliegue**, y cuenta
    eso el servidor y no cada una de las dos pantallas por su lado.
    """

    return await service.resumen_agente(session, tenant.organization.id)


@router.get("/summary/networks", response_model=AgentSummary)
async def resumen_de_redes(
    tenant: TenantDependency,
    session: SessionDependency,
) -> AgentSummary:
    """El resumen, acotado a los escaneos de red.

    ## Por qué una ruta aparte y no un parámetro

    Porque `/containers` y `/networks` son dos pantallas con dos tablas distintas, y filtrar en
    el cliente significaría traer **todos** los resultados de los dos tipos para pintar la mitad.
    Con un escaneo de contenedor de 400 paquetes, la respuesta del resumen de redes sería cinco
    veces más grande de lo necesario para dibujar un gráfico de puertos.

    Se reparte el calculo en el servidor en vez de en el cliente por la misma razon por la que el
    filtro de organizacion va en el `WHERE`: lo que se filtra antes de leer, no se trae.
    """

    return await service.resumen_agente(session, tenant.organization.id, solo_redes=True)


@router.post("/jobs/requeue-expired", response_model=dict[str, int])
async def reponer(
    tenant: TenantDependency,
    session: SessionDependency,
    _admin: AdminRequired,
) -> dict[str, int]:
    """Devuelve a la cola los escaneos cuyo agente dejó el trabajo a medias.

    Está expuesto como endpoint y no solo como tarea periódica porque el caso que lo hace
    necesario **no** se resuelve esperando: un agente apagado no avisa, así que su trabajo caduca
    sin que nadie lo sepa hasta que el cliente pregunta por qué su escaneo lleva horas en curso.
    Con este endpoint la pantalla lo puede arreglar sin esperar al temporizador, y el temporizador
    lo hace igualmente para cuando nadie está mirando.
    """

    return {"requeued": await service.reponer_alquileres(session, tenant.organization.id)}


# --------------------------------------------------------------------------- #
# Lado del agente
#
# Van **debajo** de los del cliente a propósito, no por gusto. FastAPI resuelve las rutas en el
# orden en que se declaran, y `/jobs/requeue-expired` se parece a `/jobs/{job_id}`. Si el de
# detalle se declarara antes, `requeue-expired` se interpretaría como un identificador y la
# operación de reponer trabajos sería inalcanzable. Por eso el de reponer va **antes** que el
# detalle, y hay una prueba que lo comprueba.
# --------------------------------------------------------------------------- #


@router.post("/heartbeat", response_model=AgentItem)
async def latido(
    payload: AgentHeartbeat,
    agente: AgentDependency,
    session: SessionDependency,
) -> AgentItem:
    """El agente dice que sigue vivo."""

    return AgentItem.model_validate(
        await service.registrar_latido(
            session, agente, payload.agent_version, payload.platform_hint
        )
    )


@router.post("/jobs/claim", response_model=AgentJobClaimed | None)
async def reclamar(
    agente: AgentDependency,
    session: SessionDependency,
) -> AgentJobClaimed | None:
    """Toma el trabajo más antiguo que esté pendiente, o devuelve `null`.

    `null` **no** es un error: es la respuesta de "no hay nada que hacer", y es la que el agente
    recibe la mayor parte de las veces. Un `204` obligaría a distinguir en el cliente entre "no
    hay trabajo" y "petición incorrecta", y un `404` sugeriría que el endpoint no existe, que es
    justo lo que no es.
    """

    reclamado = await service.reclamar_trabajo(session, agente)
    if reclamado is None:
        return None
    trabajo, _ = reclamado
    return AgentJobClaimed(
        id=trabajo.id,
        kind=trabajo.kind,
        target=trabajo.target,
        ports=list(service.PUERTOS_POR_DEFECTO),
        lease_expires_at=trabajo.lease_expires_at,
    )


@router.post("/jobs/{job_id}/start", response_model=AgentJobDetail)
async def empezar(
    job_id: uuid.UUID,
    agente: AgentDependency,
    session: SessionDependency,
) -> AgentJobDetail:
    """El agente empieza a trabajar de verdad."""

    try:
        trabajo = await service.marcar_en_curso(session, agente, job_id)
    except JobNotClaimableError as error:
        raise entidad_inexistente() from error
    return _a_detalle(trabajo, {agente.id: agente.name})


@router.post("/jobs/{job_id}/report", response_model=AgentJobDetail)
async def entregar(
    job_id: uuid.UUID,
    reporte: AgentJobReport,
    agente: AgentDependency,
    session: SessionDependency,
) -> AgentJobDetail:
    """Cierra el trabajo con su resultado o con su motivo de fallo.

    El resultado se guarda con su huella SHA-256 y a partir de ese momento es inmutable: un
    `trigger` de PostgreSQL rechaza el `UPDATE` y el `DELETE` de un trabajo ya terminado. Un
    escaneo registra lo que había cuando se hizo, y poder reescribirlo después convierte la
    evidencia en una afirmación.
    """

    try:
        trabajo = await service.entregar_trabajo(session, agente, job_id, reporte)
    except JobNotClaimableError as error:
        raise entidad_inexistente() from error
    return _a_detalle(trabajo, {agente.id: agente.name})


# --------------------------------------------------------------------------- #
# Traducción a los esquemas de salida
# --------------------------------------------------------------------------- #


def _a_item(trabajo: AgentJob, nombres: dict[uuid.UUID, str]) -> AgentJobItem:
    """Un trabajo sin su resultado, para la lista.

    La lista **no** lleva el `result` a propósito: un escaneo de red de un `/24` puede traer
    cientos de hosts y la lista se convertiría en una descarga de varios megabytes para mostrar
    una fila. El resultado se pide en el detalle.
    """

    return AgentJobItem(
        id=trabajo.id,
        kind=trabajo.kind,
        target=trabajo.target,
        status=trabajo.status,
        priority_order=trabajo.priority_order,
        agent_id=trabajo.agent_id,
        agent_name=nombres.get(trabajo.agent_id) if trabajo.agent_id else None,
        requested_by=trabajo.requested_by,
        attempt_count=trabajo.attempt_count,
        claimed_at=trabajo.claimed_at,
        completed_at=trabajo.completed_at,
        error_message=trabajo.error_message,
        evidence_intact=preservar_evidencia(trabajo),
        created_at=trabajo.created_at,
    )


def _a_detalle(trabajo: AgentJob, nombres: dict[uuid.UUID, str]) -> AgentJobDetail:
    return AgentJobDetail(**_a_item(trabajo, nombres).model_dump(), result=trabajo.result)
