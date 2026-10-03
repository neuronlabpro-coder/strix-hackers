"""Endpoints de soporte para clientes y para la consola de administración.

## Por qué hay dos grupos de rutas y no una con un parámetro de rol

Un endpoint que decide "soy cliente o soy soporte" según quién llama tiene la mitad de sus
ramas probadas en cada modo, y un fallo en la condición deja que un cliente escriba como
soporte. Rutas separadas: cada una tiene su dependencia, sus pruebas y su superficie, y no
se puede llegar a la de administración con el token equivocado —porque exige superusuario,
no porque un `if` decida bien.

## Por qué el cliente nunca manda `is_admin_reply`

No está en ningún esquema de entrada. Se deduce del hecho de haber llegado por la ruta de
administración. Un campo que el cliente pudiera poner a `True` le permitiría disfrazarse
de soporte en un hilo, y ese booleano es lo único que separa las dos voces en pantalla.

## Por qué no hay emisión de eventos aquí

El catálogo de webhooks tiene doce eventos y ninguno es de soporte. Un ticket es una
conversación interna entre un cliente y la plataforma, no un cambio de estado del producto:
no hay suscriptor legítimo de "alguien preguntó algo", y emitirlo obligaría a los tenants a
recibir ruido que no pueden actuar. Cuando exista un evento de ticket, se cablea como los
demás desde `emission.py`, no desde aquí.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.admin.dependencies import SuperuserDependency
from backend.apps.organizations.models import User
from backend.apps.support import service
from backend.apps.support.models import (
    SupportTicket,
    TicketPriorityEnum,
    TicketStatusEnum,
)
from backend.apps.support.schemas import (
    AdminTicketUpdate,
    SupportSummary,
    TicketCreate,
    TicketDetail,
    TicketItem,
    TicketMessageCreate,
    TicketMessageItem,
    TicketPage,
)
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant

SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]

# El router de soporte, y el de la consola de SuperAdmin para las rutas de tickets.
#
# ## Por que estan los dos en este fichero y no en dos
#
# Porque comparten la logica de ticket y los esquemas: partirlo obligaria a duplicar el
# `TicketPage`, el `TicketDetail` y las transiciones de estado, y las dos copias divergirian. Lo
# que se separa es la **puerta**: `/api/v1/support/*` va con sesion de cliente y
# `/api/v1/admin/tickets/*` con `SuperuserDependency`.
#
# ## Por que se declara `admin_router` **una sola vez**, aqui arriba
#
# Porque habia dos `admin_router = APIRouter()`, esta y otra unas doscientas lineas mas abajo, y
# la segunda **pisaba** la primera: todas las decoraciones `@admin_router.*` de tickets estan
# despues de la segunda, asi que hoy las rutas se registran bien y la consola funciona.
#
# Pero el objeto de la primera se descartaba en silencio, y ahi se escondia la bomba: anadir una
# ruta entre las dos, siguiendo la lectura de arriba, la registraria sobre un router que `main.py`
# **nunca importa**, y el endpoint desapareceria sin error ni aviso. Un fallo de disponibilidad que
# solo se manifiesta cuando alguien anade una ruta en el sitio equivocado.
router = APIRouter()
admin_router = APIRouter()


def _a_item(
    fila: service.TicketRow, *, tenant_name: str | None = None
) -> TicketItem:
    """Convierte una fila del listado en el esquema de la tabla.

    `tenant_name` sobrescribe el nombre resuelto por la consulta. El listado del cliente no
    necesita resolverlo —siempre es el suyo— y la vista de soporte lo pinta en su propia
    columna, así que el parámetro existe solo para no repetir la construcción del objeto dos
    veces con la mitad de los campos.
    """

    return TicketItem(
        id=fila.ticket.id,
        ticket_number=fila.ticket.ticket_number,
        subject=fila.ticket.subject,
        category=fila.ticket.category,
        priority=fila.ticket.priority,
        status=fila.ticket.status,
        organization_id=fila.ticket.organization_id,
        organization_name=tenant_name or fila.tenant_name,
        created_by_user_id=fila.ticket.created_by_user_id,
        created_by_email=fila.creator_email,
        assigned_to_user_id=fila.ticket.assigned_to_user_id,
        assigned_to_email=fila.agent_email,
        created_at=fila.ticket.created_at,
        updated_at=fila.ticket.updated_at,
        message_count=fila.message_count,
    )


async def _a_detalle(
    session: AsyncSession, ticket: SupportTicket
) -> TicketDetail:
    """Reconstruye el detalle de un ticket: su fila + su hilo.

    Reutiliza `listar_tickets` con un filtro por `id` en vez de repetir los tres `JOIN` a
    `organizations` y `users`. El filtro es el mismo que la lista, así que la fila sale
    idéntica y queda una sola definición de "cómo se lee un ticket".
    """

    filas, _total = await service.listar_tickets(
        session, organization_id=None, solo_id=ticket.id, limit=1
    )
    if not filas:
        # El ticket se acababa de leer, así que en la práctica no hay carrera. Pero si la
        # fila desapareciera entre las dos consultas, devolver un detalle sin cuerpo
        # sería peor que un `404`: el cliente necesita un ticket o necesita otro ticket.
        raise service.NotFoundError("Ticket no encontrado")
    base = _a_item(filas[0])
    mensajes = await service.listar_mensajes(session, ticket.id)
    return TicketDetail(
        **base.model_dump(),
        messages=[
            TicketMessageItem(
                id=fila.message.id,
                sender_user_id=fila.message.sender_user_id,
                sender_email=fila.sender_email,
                is_admin_reply=fila.message.is_admin_reply,
                content=fila.message.content,
                created_at=fila.message.created_at,
            )
            for fila in mensajes
        ],
    )


@router.get("/api/v1/support/summary", response_model=SupportSummary)
async def read_support_summary(
    tenant: TenantDependency,
    session: SessionDependency,
) -> SupportSummary:
    """Recuentos de tickets y si este tenant puede pedir urgencia.

    El panel lo pide para las tarjetas **y** para decidir si el selector de prioridad
    muestra `URGENT` activo. Que el cliente sepa la regla antes de intentar es cortesía; la
    garantía sigue siendo el `403` del servidor.
    """

    return await service.resumir(session, tenant.organization, tenant.user)


@router.post("/api/v1/support/tickets", response_model=TicketDetail, status_code=201)
async def create_ticket(
    payload: TicketCreate,
    tenant: TenantDependency,
    session: SessionDependency,
) -> TicketDetail:
    """Abre un ticket con su primer mensaje.

    ## Por qué `URGENT` sin Enterprise es `403` y no `422`

    El cliente pidió algo que **no tiene derecho a pedir**, no algo que está mal escrito. Un
    `422` diría "el valor es inválido" y dejaría la duda de si otro valor funcionaría; un
    `403` dice "esto requiere un plan que no tienes", que es la verdad y abre la vía de
    subir de plan.
    """

    try:
        ticket = await service.crear_ticket(
            session,
            organization=tenant.organization,
            user=tenant.user,
            subject=payload.subject,
            category=payload.category,
            priority=payload.priority,
            message=payload.message,
        )
    except service.UrgentRequiresEnterpriseError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(error)
        ) from error

    return await _a_detalle(session, ticket)


@router.get("/api/v1/support/tickets", response_model=TicketPage)
async def list_tickets(
    tenant: TenantDependency,
    session: SessionDependency,
    status_filter: Annotated[
        TicketStatusEnum | None, Query(alias="status")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    query: Annotated[str | None, Query(max_length=256)] = None,
    created_from: date | None = None,
    created_to: date | None = None,
) -> TicketPage:
    """Los tickets del tenant activo, con filtro por estado, texto y rango de alta.

    El tenant sale del contexto, no de un parámetro: un cliente no puede pedir los tickets
    de otro workspace ni por accidente ni queriendo. Y no hay filtro por prioridad aquí a
    propósito —en la vista de cliente cada ticket propio se distingue solo, y un filtro que
    solo puede vaciar la lista no es un filtro—.

    ## Por qué aquí el parámetro se llama `query` y en la consola se llama `search`

    Porque son dos rutas distintas con dos clientes distintos, y el nombre se ha copiado del
    patrón que ya está verificado en `/api/v1/pr-reviews/`, donde el mismo filtro se llama
    `query`. El nombre histórico de la consola —`search`— se conserva: renombrar un parámetro de
    consulta que ya está en uso rompe a su llamador sin decir nada, y el cliente de la consola
    no es este fichero. Queda la divergencia apuntada, no escondida: si algún día se unifica,
    es un cambio con dos archivos de cliente y dos pruebas, no uno.

    ## Por qué el rango va sobre la fecha de alta

    Porque `updated_at` es la columna que ordena la lista y se mueve con cada mensaje: filtrar
    por ella daría un resultado distinto cada vez que alguien conteste. `created_at` no cambia
    nunca. La columna «Actualizado» de la tabla sigue mostrando la última actividad, y el filtro
    se anuncia como rango de alta para que no se confundan. El corte es la medianoche UTC del día
    pedido y el último día entra entero; un rango invertido devuelve la lista vacía, no un `422`.
    Detalle en `service.listar_tickets`.
    """

    filas, total = await service.listar_tickets(
        session,
        organization_id=tenant.organization.id,
        status=status_filter,
        search=query,
        # El nombre del workspace **no** se busca en la vista de cliente: es siempre el mismo,
        # así que igualaría el filtro a «devuélveme todos» en cuanto la palabra buscada saliera
        # en el nombre de la empresa. Ver `service._busqueda_por_texto`.
        incluir_nombre_tenant=False,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )
    return TicketPage(
        items=[_a_item(fila) for fila in filas], total=total, limit=limit, offset=offset
    )


@router.get("/api/v1/support/tickets/{ticket_id}", response_model=TicketDetail)
async def read_ticket(
    ticket_id: UUID,
    tenant: TenantDependency,
    session: SessionDependency,
) -> TicketDetail:
    """El ticket con su hilo completo.

    Un `404` si el ticket es de otro workspace. No un `403`: un `403` confirmaría que ese
    identificador existe, que es justo lo que R3 no permite revelar.
    """

    try:
        ticket = await service.obtener_ticket(
            session, ticket_id, tenant.organization.id
        )
    except service.NotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error

    detalle = await _a_detalle(session, ticket)
    # El nombre del tenant sale del listado sin filtrar, que es correcto pero viene de una
    # consulta global. Se sustituye por el del contexto para que este endpoint no dependa
    # de esa consulta para nada: aquí solo se puede revelar el workspace del que se pidió.
    return detalle.model_copy(update={"organization_name": tenant.organization.name})


@router.post(
    "/api/v1/support/tickets/{ticket_id}/messages",
    response_model=TicketMessageItem,
    status_code=201,
)
async def reply_to_ticket(
    ticket_id: UUID,
    payload: TicketMessageCreate,
    tenant: TenantDependency,
    session: SessionDependency,
) -> TicketMessageItem:
    """Añade un mensaje del cliente al hilo.

    ## Por qué escribir en un ticket resuelto es `409`

    Un ticket resuelto es un hilo que el soporte dio por terminado. Escribir ahí lo reabre
    sin que el agente se entere, y el cliente se queda esperando una respuesta que no va a
    llegar. Un `409` dice que el hilo está cerrado y que hace falta otro.
    """

    try:
        ticket = await service.obtener_ticket(
            session, ticket_id, tenant.organization.id
        )
    except service.NotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error

    try:
        service.comprobar_escribible_por_cliente(ticket)
    except service.TicketNotWritableError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error

    mensaje = await service.anadir_mensaje(
        session,
        ticket=ticket,
        sender=tenant.user,
        content=payload.content,
        is_admin_reply=False,
    )
    return TicketMessageItem(
        id=mensaje.id,
        sender_user_id=mensaje.sender_user_id,
        sender_email=tenant.user.email,
        is_admin_reply=False,
        content=mensaje.content,
        created_at=mensaje.created_at,
    )


# --------------------------------------------------------------------------- #
# Consola de administración
# --------------------------------------------------------------------------- #
#
# Estas rutas **no** aceptan `X-Organization-Id` a propósito, igual que el resto de la
# consola: cruzan tenants, y mandar la cabecera sería enviar un filtro que el servidor
# ignora. La frontera es `is_superuser`, exigida en cada una por `SuperuserDependency`,
# que además entrega el `User` del agente.



@admin_router.get("/api/v1/admin/tickets", response_model=TicketPage)
async def list_all_tickets(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    status_filter: Annotated[
        TicketStatusEnum | None, Query(alias="status")
    ] = None,
    priority: Annotated[TicketPriorityEnum | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> TicketPage:
    """Triaje global con filtros por estado, prioridad y texto libre.

    Se ordena por `updated_at` descendente, no por prioridad: un ticket urgente de hace
    tres semanas no es más urgente que uno normal de hace una hora, y ordenar por prioridad
    escondería los recientes detrás de los que llevan meses sin tocarse. El filtro por
    prioridad sí está —para la cola de urgencias—, pero no es el criterio de orden.
    """

    filas, total = await service.listar_tickets(
        session,
        organization_id=None,
        status=status_filter,
        priority=priority,
        search=search.strip() if search else None,
        limit=limit,
        offset=offset,
    )
    return TicketPage(
        items=[_a_item(fila) for fila in filas], total=total, limit=limit, offset=offset
    )


@admin_router.get("/api/v1/admin/tickets/{ticket_id}", response_model=TicketDetail)
async def read_any_ticket(
    ticket_id: UUID,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> TicketDetail:
    """El ticket de cualquier workspace, con su hilo.

    La consola cruza tenants por definición —es el motivo de que exista—, así que aquí no
    hay filtro por organización. La frontera la pone `is_superuser`, y esa es la frontera
    que R3 permite abrir: un superusuario ve todos los workspaces a propósito y todas sus
    acciones quedan en `audit_log`.
    """

    try:
        ticket = await service.obtener_ticket_global(session, ticket_id)
    except service.NotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    return await _a_detalle(session, ticket)


@admin_router.patch("/api/v1/admin/tickets/{ticket_id}", response_model=TicketDetail)
async def update_ticket(
    ticket_id: UUID,
    payload: AdminTicketUpdate,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> TicketDetail:
    """Cambia estado, prioridad o agente asignado.

    La asignación se toca **solo** si la clave vino en el cuerpo. Sin eso, cada cambio de
    prioridad vaciaría el agente de todos los tickets, porque `None` indistinguía "no lo he
    pensado" de "lo he quitado".
    """

    try:
        ticket = await service.obtener_ticket_global(session, ticket_id)
    except service.NotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error

    if payload.toca_asignacion and payload.assigned_to_user_id is not None:
        await _exigir_asignable(session, payload.assigned_to_user_id)

    ticket = await service.actualizar_ticket(
        session,
        ticket=ticket,
        status=payload.status,
        priority=payload.priority,
        tocar_asignacion=payload.toca_asignacion,
        assigned_to_user_id=payload.assigned_to_user_id,
    )
    return await _a_detalle(session, ticket)


async def _exigir_asignable(
    session: AsyncSession, user_id: UUID
) -> None:
    """Comprueba que a ese usuario se le pueden asignar tickets.

    Asignar a un usuario desactivado deja el ticket en manos de alguien que no puede
    autenticarse para contestarlo, y se queda ahí hasta que alguien se dé cuenta. Un `422` —
    el cuerpo es incorrecto— y no un `404`: lo que falla es que ese usuario no sea
    asignable, no que exista.

    Se exige superusuario activo, no superusuario. Asignar tickets al superusuario es una
    decisión legítima de la consola, y desactivarlo para responder sigue siendo posible
    mientras esté activo: lo que no tiene sentido es dejar trabajo parado a alguien que ya
    no puede entrar.
    """

    asignable = (
        await session.execute(
            select(User.id).where(
                User.id == user_id,
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if asignable is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El agente asignado no existe o está desactivado",
        )


@admin_router.post(
    "/api/v1/admin/tickets/{ticket_id}/messages",
    response_model=TicketMessageItem,
    status_code=201,
)
async def admin_reply(
    ticket_id: UUID,
    payload: TicketMessageCreate,
    agente: SuperuserDependency,
    session: SessionDependency,
) -> TicketMessageItem:
    """Responde como soporte.

    `is_admin_reply` se fija aquí, a `True`, y no se lee del cuerpo: es el hecho de venir
    por esta ruta lo que marca la respuesta como del equipo de soporte.

    A diferencia del cliente, el soporte **sí** puede escribir en un ticket resuelto o
    cerrado. Un cliente que responde a algo ya cerrado reabre una conversación que nadie
    espera; un agente que responde lo que sea —una aclaración, un apunte que se queda en el
    hilo sin tocar la columna de estado— es trabajo normal de quien atiende la cola. Por eso
    esta ruta no llama a `comprobar_escribible_por_cliente`.
    """

    try:
        ticket = await service.obtener_ticket_global(session, ticket_id)
    except service.NotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error

    mensaje = await service.anadir_mensaje(
        session,
        ticket=ticket,
        sender=agente,
        content=payload.content,
        is_admin_reply=True,
    )
    return TicketMessageItem(
        id=mensaje.id,
        sender_user_id=mensaje.sender_user_id,
        sender_email=agente.email,
        is_admin_reply=True,
        content=mensaje.content,
        created_at=mensaje.created_at,
    )


__all__ = [
    "admin_router",
    "router",
]
