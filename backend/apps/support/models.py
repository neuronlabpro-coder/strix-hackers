"""Modelos del sistema nativo de tickets de soporte.

## Por qué no hay `ON DELETE CASCADE` en `organization_id`

Un ticket es el registro de un problema que un cliente registró y que soporte resolvió.
Si el workspace se borra, el ticket **no se puede borrar con él**: la conversación es la
evidencia de lo que se le dijo a ese cliente, y en una disputa de facturación o de
responsabilidad sobre un hallazgo, es lo que demuestra que se le avisó.

`RESTRICT` hace que el borrado del workspace **falle** en vez de arrastrar los tickets. Es
deliberado: la vía correcta es dar de baja el workspace —el borrado lógico que ya existe—,
que conserva los tickets. La alternativa, `CASCADE`, los borraría en silencio.

## Por qué `ticket_number` y no el UUID

El UUID sirve para las claves, pero nadie puede leerlo en voz alta por teléfono. El número
`#TK-1001` es lo que el cliente dice al soporte y lo que el agente busca.

Se numera con una **secuencia de PostgreSQL** y no con `MAX(id) + 1`: con el `MAX` dos
altas simultáneas obtienen el mismo número, y el índice único que haría falta para
corregirlo solo convierte el conflicto en un error en vez de un ticket lost. La secuencia
es atómica y no necesita que nadie la bloquee.

## Por qué `assigned_to_user_id` no lleva `RESTRICT` a `users`

Es `SET NULL`. Un agente que deja la empresa no puede dejar tickets asignados a un usuario
inexistente, y su baja **no** debe fallar por tener tickets abiertos. Perder la asignación
es el comportamiento correcto: el ticket vuelve a la cola, que es donde tiene que estar.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.database import Base


class SupportCategoryEnum(StrEnum):
    """Motivo de contacto. Determina quién lo atiende, no cambia el flujo."""

    TECHNICAL = "TECHNICAL"
    BILLING = "BILLING"
    VULNERABILITY_REVIEW = "VULNERABILITY_REVIEW"
    FEATURE_REQUEST = "FEATURE_REQUEST"


class TicketPriorityEnum(StrEnum):
    """Urgencia de la petición.

    `URGENT` está **reservado a Enterprise**: es la única forma de que un cliente sin plan
    paguen por soporte prioritario, y por eso la regla se comprueba en el servidor. El
    panel desactiva el selector por el mismo motivo, pero esa es cortesía: la garantía es
    que el `403` salga de aquí.
    """

    LOW = "LOW"
    NORMAL = "NORMAL"
    URGENT = "URGENT"


class TicketStatusEnum(StrEnum):
    """Ciclo de vida del ticket.

    El recorrido es `OPEN -> IN_PROGRESS -> RESOLVED -> CLOSED`, y `CLOSED` es terminal.
    `RESOLVED` y `CLOSED` se distinguen porque un ticket resuelto que el cliente no
    acepta se reabre —y eso solo puede saberse si los dos estados son diferentes—.
    """

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class SupportTicket(Base):
    """Una conversación de soporte entre un cliente y la plataforma."""

    __tablename__ = "support_tickets"
    __table_args__ = (
        # La lista de un tenant es siempre "los míos, más recientes primero". El índice
        # compuesto cubre ese acceso sin.order_by: PostgreSQL puede leerlo en orden y no
        # tiene que ordenar en memoria un tickets que ya le interesa ordenados.
        Index(
            "ix_support_tickets_tenant_created",
            "organization_id",
            "created_at",
        ),
        # La cola de soporte: "urgentes y abiertos primero", que es la consulta que hace el
        # agente al abrir la vista de triaje.
        Index(
            "ix_support_tickets_queue",
            "status",
            "priority",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: `#TK-1001`. Lo emite una **secuencia de PostgreSQL**, no Python.
    #:
    #: El `server_default` es lo que hace que la columna se rellene sola, y está declarado
    #: aquí aunque lo haya creado la migración: sin él, el modelo describe una tabla sin
    #: `DEFAULT` y `alembic check` lo reporta como drift en cada ejecución. Declarar la
    #: verdad del esquema en dos sitios no es duplicación, es que el modelo **es** la segunda
    #: copia que el verificador compara con la primera. Si divergen, salta el error.
    #:
    #: ## Por qué los paréntesis y los casts explícitos no son decorativos
    #:
    #: PostgreSQL no guarda el `DEFAULT` tal cual se escribe: lo normaliza y lo envuelve en
    #: paréntesis, añadiendo los casts de cada operando. Lo que acaba en `pg_attrdef` es
    #: exactamente `('TK-'::text || nextval('support_ticket_number_seq'::regclass))`, con las
    #: llaves incluidas.
    #:
    #: El `DEFAULT` es una **cadena de texto comparada**, no una expresión evaluada, así que
    #: el modelo tiene que declarar esa cadena exacta. Escribirlo sin paréntesis produce la
    #: misma columna y las mismas filas, y aun así `alembic check` pide migrarlo en cada
    #: ejecución porque los dos textos no son iguales.
    #:
    #: Escribir la forma canónica desde el principio deja lo declarado y lo almacenado
    #: idénticos desde el primer `INSERT`. El precio es que los paréntesis parecen
    #: redundantes; son el contrato con el verificador.
    #:
    #: La alternativa —`default=` en Python, o un `SELECT nextval()` antes del `INSERT`— deja
    #: el número en manos de la aplicación y obliga a que **toda** vía de alta pase por ese
    #: camino. Con el `DEFAULT` en la base, un `INSERT` escrito a mano, un `COPY` o una
    #: migración futura también numeran bien.
    ticket_number: Mapped[str] = mapped_column(
        String(32),
        server_default=text(
            "('TK-'::text || nextval('support_ticket_number_seq'::regclass))"
        ),
        nullable=False,
        unique=True,
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[SupportCategoryEnum] = mapped_column(
        SQLEnum(SupportCategoryEnum, name="support_category_enum"),
        nullable=False,
        index=True,
    )
    priority: Mapped[TicketPriorityEnum] = mapped_column(
        SQLEnum(TicketPriorityEnum, name="ticket_priority_enum"),
        nullable=False,
        default=TicketPriorityEnum.NORMAL,
        server_default=TicketPriorityEnum.NORMAL.name,
        index=True,
    )
    status: Mapped[TicketStatusEnum] = mapped_column(
        SQLEnum(TicketStatusEnum, name="ticket_status_enum"),
        nullable=False,
        default=TicketStatusEnum.OPEN,
        server_default=TicketStatusEnum.OPEN.name,
        index=True,
    )
    #: El agente asignado. `SET NULL` a propósito: ver la nota de la cabecera del módulo.
    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    #: `lazy="raise"` en las dos relaciones. Ninguna consulta del sistema necesita recorrer el
    #: grafo de un ticket: el listado usa `JOIN` explícito y el detalle trae el hilo con su
    #: propia consulta. Dejar `lazy="select"` permitiría que un `ticket.organization.nombre`
    #: añadido en el futuro disparara una consulta **sin filtro de tenant** en mitad de una
    #: ruta, que es exactamente el fallo que R3 prohíbe.
    messages: Mapped[list[TicketMessage]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="TicketMessage.created_at",
    )


class TicketMessage(Base):
    """Un mensaje del hilo.

    `CASCADE` desde el ticket: un ticket sin mensajes no es una conversación, es una fila
    huérfana, y borrarla con su ticket es lo que cualquiera esperaría. El rastro de que
    existió vive en `audit_log`, que por R4 no se borra nunca.
    """

    __tablename__ = "ticket_messages"
    __table_args__ = (
        # El hilo se lee entero y en orden cronológico. El índice compuesto evita el
        # `ORDER BY` sobre un ticket con años de conversación.
        Index("ix_ticket_messages_ticket_created", "ticket_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support_tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: `True` si lo escribió la plataforma. El cliente **nunca** puede ponerlo a `True`.
    is_admin_reply: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ticket: Mapped[SupportTicket] = relationship(back_populates="messages", lazy="raise")


#: `ticket_number` no tiene `default` de Python a propósito. El número lo emite una
#: **secuencia de PostgreSQL** cuyo `DEFAULT` está declarado arriba, y una fila escrita a
#: mano —un `INSERT` de una migración, un `COPY`, una consola— quedaría sin número si la
#: numeración viviera en la aplicación. El prefijo `#TK-` y el arranque en `1001` los puso
#: la migración, que es la única que crea la secuencia.
__all__ = [
    "SupportCategoryEnum",
    "SupportTicket",
    "TicketMessage",
    "TicketPriorityEnum",
    "TicketStatusEnum",
]
