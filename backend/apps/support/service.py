"""Lógica de negocio de los tickets de soporte.

## Por qué la regla de `URGENT` vive aquí y no en el esquema

El esquema no sabe el plan del tenant. Solo esta capa lo lee, así que es el único sitio
donde la regla puede estar sin duplicarse. La ruta la traduce a `403`.

## Por qué el aislamiento va en el `WHERE` y no después

R3 exige que la restricción esté en la consulta. Filtrar en Python dejaría la puerta abierta
a que un cambio futuro de una función la olvide, y con 46 scopes de API ya se aprendió que
la puerta se abre por un cambio de una línea. Un ticket de otro workspace devuelve `404`
y no `403`: un `403` confirmaría que ese identificador existe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.apps.organizations.models import (
    Organization,
    PlanTierEnum,
    User,
)
from backend.apps.support.models import (
    SupportCategoryEnum,
    SupportTicket,
    TicketMessage,
    TicketPriorityEnum,
    TicketStatusEnum,
)
from backend.apps.support.schemas import SupportSummary

#: Dos alias del mismo modelo `User` para poder distinguir en el `SELECT` quién es el
#: creador y quién el agente sin ambigüedad. Sin los alias, un `LEFT JOIN` a la misma tabla
#: dos veces hace que SQLAlchemy cartele las dos columnas con el mismo nombre y el valor
#: devuelto sea el del último join.
Creador = aliased(User)
Agente = aliased(User)


class TicketRow(NamedTuple):
    """Una fila del listado, con los tres datos ajenos ya resueltos.

    Es un `NamedTuple` y no la entidad de ORM porque el servicio devuelve **después** del
    `commit()`, y con `expire_on_commit=False` los objetos sobreviven pero leerlos desde la
    ruta sigue obligando a pasarlos enteros. Empaquetarlos aquí deja el contrato explícito:
    la ruta recibe campos, no una sesión.
    """

    ticket: SupportTicket
    tenant_name: str
    creator_email: str
    agent_email: str | None
    message_count: int


class TicketMessageRow(NamedTuple):
    """Un mensaje del hilo con el email de su autor."""

    message: TicketMessage
    sender_email: str


class UrgentRequiresEnterpriseError(RuntimeError):
    """Un tenant sin plan Enterprise intentó abrir un ticket `URGENT`.

    Es una excepción de **dominio** y no un `HTTPException` porque la decisión es del
    servicio. La ruta la traduce a `403`.
    """


class NotFoundError(LookupError):
    """El ticket no existe, o no es del tenant que pregunta.

    Un solo tipo de error para los dos casos a propósito: el cliente no puede distinguir
    "no existe" de "es de otro workspace", y esa indistinguibilidad es la garantía de que
    el `404` no confirma la existencia de nada ajeno.
    """


#: Los estados en los que un cliente puede seguir escribiendo. Un ticket resuelto o
#: cerrado es un hilo terminado: responder ahí reabriría una conversación que el soporte
#: dio por buena, y el agente no se entera.
ABIERTO_PARA_CLIENTE: frozenset[TicketStatusEnum] = frozenset(
    {TicketStatusEnum.OPEN, TicketStatusEnum.IN_PROGRESS}
)


def puede_pedir_urgente(plan: PlanTierEnum, is_superuser: bool) -> bool:
    """Si quien pregunta puede abrir un ticket `URGENT`.

    El superusuario puede, y la razón es que **necesita poder probarlo**: sin esa puerta,
    probar el camino de `URGENT` exigiría un tenant Enterprise, y la regla se quedaría sin
    verificar en el entorno de desarrollo, que es donde se escribe el código.
    """

    return plan == PlanTierEnum.ENTERPRISE or is_superuser


def _exigir_prioridad_permitida(
    priority: TicketPriorityEnum, plan: PlanTierEnum, is_superuser: bool
) -> None:
    if priority != TicketPriorityEnum.URGENT:
        return
    if not puede_pedir_urgente(plan, is_superuser):
        raise UrgentRequiresEnterpriseError(
            "Urgent priority requires an Enterprise plan"
        )


async def crear_ticket(
    session: AsyncSession,
    *,
    organization: Organization,
    user: User,
    subject: str,
    category: SupportCategoryEnum,
    priority: TicketPriorityEnum,
    message: str,
) -> SupportTicket:
    """Abre un ticket con su primer mensaje y devuelve el ticket ya confirmado.

    El mensaje inicial va **en la misma transacción** que el ticket. Un ticket sin texto
    existe y el agente lo ve en la cola sin saber qué falla, así que abrir y describir en
    dos peticiones deja esa ventana; y si la segunda falla, queda un ticket vacío que hay
    que cerrar a mano.
    """

    _exigir_prioridad_permitida(priority, organization.plan_tier, user.is_superuser)

    ticket = SupportTicket(
        organization_id=organization.id,
        created_by_user_id=user.id,
        subject=subject,
        category=category,
        priority=priority,
        status=TicketStatusEnum.OPEN,
    )
    session.add(ticket)
    await session.flush()

    session.add(
        TicketMessage(
            ticket_id=ticket.id,
            sender_user_id=user.id,
            is_admin_reply=False,
            content=message,
        )
    )
    await session.commit()
    await session.refresh(ticket)
    return ticket


async def obtener_ticket(
    session: AsyncSession, ticket_id: uuid.UUID, organization_id: uuid.UUID
) -> SupportTicket:
    """Resuelve un ticket **del tenant**, con su `404` si no es suyo.

    El filtro por organización va en el `WHERE` junto al `id`. Filtrar el resultado en
    Python significaría traer el ticket de otro workspace a memoria para decidir que no es
    el del usuario: es el aislamiento resuelto tarde y en el sitio equivocado.
    """

    ticket = (
        await session.execute(
            select(SupportTicket).where(
                SupportTicket.id == ticket_id,
                SupportTicket.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise NotFoundError("Ticket no encontrado")
    return ticket


async def obtener_ticket_global(
    session: AsyncSession, ticket_id: uuid.UUID
) -> SupportTicket:
    """Resuelve un ticket sin filtrar por tenant. Solo para la consola de administración."""

    ticket = (
        await session.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        raise NotFoundError("Ticket no encontrado")
    return ticket


def comprobar_escribible_por_cliente(ticket: SupportTicket) -> None:
    """El cliente solo escribe en tickets abiertos. Un `409` si no.

    Escribir en un ticket resuelto reabre una conversación que el soporte dio por buena, y
    el agente no se entera porque el cambio de estado no ocurre. Un `409` dice exactamente
    eso: el hilo está terminado y hay que abrir otro.
    """

    if ticket.status not in ABIERTO_PARA_CLIENTE:
        raise TicketNotWritableError(
            "El ticket está resuelto o cerrado. Abre uno nuevo para continuar la conversación."
        )


class TicketNotWritableError(RuntimeError):
    """Se intentó escribir en un ticket que ya no admite mensajes del cliente."""


async def anadir_mensaje(
    session: AsyncSession,
    *,
    ticket: SupportTicket,
    sender: User,
    content: str,
    is_admin_reply: bool,
) -> TicketMessage:
    """Añade un mensaje y actualiza `updated_at` del ticket.

    El `updated_at` se toca siempre, incluso cuando el mensaje es del cliente. Es lo que
    ordena la lista por "última actualización", que es lo que el usuario quiere ver: el
    ticket que alguien acaba de escribir, no el más antiguo.
    """

    mensaje = TicketMessage(
        ticket_id=ticket.id,
        sender_user_id=sender.id,
        is_admin_reply=is_admin_reply,
        content=content,
    )
    session.add(mensaje)
    ticket.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(mensaje)
    return mensaje


async def listar_tickets(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None,
    status: TicketStatusEnum | None = None,
    priority: TicketPriorityEnum | None = None,
    search: str | None = None,
    solo_id: uuid.UUID | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[TicketRow], int]:
    """Tickets con su tenant, su creador, su agente y su número de mensajes.

    ## Por qué los tres datos ajenos vienen en la misma consulta

    Tenant, creador y agente son tres `LEFT JOIN` a la misma tabla `users` y `organizations`
    que se resuelven en el motor. Resolverlos en la función de la ruta, con una consulta por
    fila, serían veinticinco idas a la base para pintar una tabla de veinticinco filas —y la
    majority no tiene agente, así que ni siquiera harían falta—. Con el `LEFT JOIN` al
    `users` de agente, el caso mayoritario no cuesta nada: una columna a `NULL`.

    El número de mensajes sale de un `GROUP BY` sobre una subconsulta, no de una cuenta por
    fila: una lista de 25 tickets con 40 mensajes cada uno son mil filas bajadas para pintar
    un contador. Y el hilo se pide solo cuando el usuario abre el ticket, así que la
    conversación entera nunca viaja en el listado.
    """

    mensajes = (
        select(TicketMessage.ticket_id, func.count(TicketMessage.id).label("message_count"))
        .group_by(TicketMessage.ticket_id)
        .subquery()
    )
    consulta = (
        select(
            SupportTicket,
            Organization.name,
            Creador.email,
            Agente.email,
            func.coalesce(mensajes.c.message_count, 0),
        )
        .join(Organization, Organization.id == SupportTicket.organization_id)
        .join(Creador, Creador.id == SupportTicket.created_by_user_id)
        .outerjoin(Agente, Agente.id == SupportTicket.assigned_to_user_id)
        .outerjoin(mensajes, mensajes.c.ticket_id == SupportTicket.id)
    )
    if organization_id is not None:
        consulta = consulta.where(SupportTicket.organization_id == organization_id)
    if solo_id is not None:
        # El detalle se lee con la misma consulta que el listado. `solo_id` y
        # `organization_id` son excluyentes en la práctica —el detalle de un cliente lleva
        # los dos y ambos deben concordar, y el de la consola solo el primero—, así que se
        # aplican como `AND` y una combinación imposible no devuelve nada en vez de
        # devolver el ticket de otro workspace.
        consulta = consulta.where(SupportTicket.id == solo_id)
    if status is not None:
        consulta = consulta.where(SupportTicket.status == status)
    if priority is not None:
        consulta = consulta.where(SupportTicket.priority == priority)
    if search:
        # La almohadilla se muestra en pantalla (`#TK-1005`) pero **no** se guarda: lo que
        # la columna contiene es `TK-1005`. El agente va a copiar lo que ve y pegarlo en el
        # buscador, así que la `#` se quita antes de comparar. Sin esto, escribir el número
        # tal como aparece en la lista —que es lo natural— no devuelve nada, y el buscador
        # parece roto sin estarlo.
        #
        # Se quita **una** `#` inicial y solo esa. `#TK` y `#TK-10` siguen funcionando como
        # búsqueda parcial, que es lo que hace útil un `ilike` con `%` a ambos lados.
        termino = search.strip().removeprefix("#").strip()
        if termino:
            patron = f"%{termino}%"
            consulta = consulta.where(
                SupportTicket.subject.ilike(patron)
                | SupportTicket.ticket_number.ilike(patron)
                | Organization.name.ilike(patron)
        )

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(consulta.order_by(None).subquery())
            )
        ).scalar_one()
    )
    filas = (
        (
            await session.execute(
                consulta.order_by(SupportTicket.updated_at.desc(), SupportTicket.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .all()
    )
    resultado = [
        TicketRow(
            ticket=ticket,
            tenant_name=tenant,
            creator_email=creador,
            agent_email=agente,
            message_count=int(recuento),
        )
        for ticket, tenant, creador, agente, recuento in filas
    ]
    return resultado, total


async def listar_mensajes(
    session: AsyncSession, ticket_id: uuid.UUID
) -> list[TicketMessageRow]:
    """El hilo en orden cronológico, con el email de quien escribió.

    Se ordena por `(created_at, id)`, no solo por `created_at`. Dos mensajes escritos en el
    mismo milisegundo —cosa que pasa cuando el cliente abre el ticket y manda el primer
    mensaje en la misma transacción— saldrían en orden arbitrario, y un hilo de soporte cuyo
    orden de lectura no es determinista no sirve para reconstruir qué se dijo cuándo.
    """

    filas = (
        (
            await session.execute(
                select(TicketMessage, User.email)
                .join(User, User.id == TicketMessage.sender_user_id)
                .where(TicketMessage.ticket_id == ticket_id)
                .order_by(TicketMessage.created_at, TicketMessage.id)
            )
        )
        .all()
    )
    return [
        TicketMessageRow(message=mensaje, sender_email=email)
        for mensaje, email in filas
    ]


async def actualizar_ticket(
    session: AsyncSession,
    *,
    ticket: SupportTicket,
    status: TicketStatusEnum | None = None,
    priority: TicketPriorityEnum | None = None,
    tocar_asignacion: bool = False,
    assigned_to_user_id: uuid.UUID | None = None,
) -> SupportTicket:
    """Aplica los cambios administrativos y refresca `updated_at`.

    La asignación se toca **solo** si vino en el cuerpo. Volver a escribir
    `assigned_to_user_id = None` cuando el cliente no mencionó la asignación vaciaría el
    agente de todos los tickets en cada cambio de prioridad, que es un efecto colateral
    invisible.
    """

    if status is not None:
        ticket.status = status
    if priority is not None:
        ticket.priority = priority
    if tocar_asignacion:
        ticket.assigned_to_user_id = assigned_to_user_id
    ticket.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(ticket)
    return ticket


async def resumir(
    session: AsyncSession, organization: Organization, user: User
) -> SupportSummary:
    """Recuentos para las tarjetas de la vista de soporte del cliente.

    Devuelve el esquema y no un `dict[str, object]`. Un diccionario obliga a la ruta a
    desempaquetarlo con `**`, y los tipos pasan a comprobarse en el punto de uso —donde un
    `object` es asignable a cualquier cosa— en vez de en la firma que los produce. Con el
    esquema, un cambio en los nombres de los campos lo rompe aquí y no en el cliente.
    """

    filas = (
        await session.execute(
            select(SupportTicket.status, func.count(SupportTicket.id))
            .where(SupportTicket.organization_id == organization.id)
            .group_by(SupportTicket.status)
        )
    ).all()
    por_estado = {estado: int(cuenta) for estado, cuenta in filas}

    return SupportSummary(
        open_count=por_estado.get(TicketStatusEnum.OPEN, 0),
        waiting_count=por_estado.get(TicketStatusEnum.IN_PROGRESS, 0),
        # `RESOLVED` y `CLOSED` se cuentan juntos a propósito: para el cliente son lo
        # mismo —"ya está resuelto"— y separarlos le pediría decidir en qué momento un
        # ticket deja de serlo. El agente sí distingue, en la consola.
        resolved_count=por_estado.get(TicketStatusEnum.RESOLVED, 0)
        + por_estado.get(TicketStatusEnum.CLOSED, 0),
        can_request_urgent=puede_pedir_urgente(
            organization.plan_tier, user.is_superuser
        ),
        plan_tier=organization.plan_tier.value,
    )


__all__ = [
    "ABIERTO_PARA_CLIENTE",
    "NotFoundError",
    "TicketMessageRow",
    "TicketNotWritableError",
    "TicketRow",
    "UrgentRequiresEnterpriseError",
    "actualizar_ticket",
    "anadir_mensaje",
    "comprobar_escribible_por_cliente",
    "crear_ticket",
    "listar_mensajes",
    "listar_tickets",
    "obtener_ticket",
    "obtener_ticket_global",
    "puede_pedir_urgente",
    "resumir",
]
