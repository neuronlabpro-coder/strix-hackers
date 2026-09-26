"""Esquemas del sistema de tickets.

## Por qué el cliente no puede pedir `URGENT` sin Enterprise

`TicketCreate` acepta `priority` con `URGENT` en la lista, y **el servicio** lo rechaza si
el tenant no tiene plan. Podría haberlo restringido en el esquema con un validador
condicional, pero el esquema no sabe el plan del tenant: solo lo sabe el servicio, que lo
lee. Poner la regla en el esquema obligaría a duplicarla en la ruta, y las dos copias
divergen en cuanto una se actualiza.

Además, un validador en el esquema que mirara `tenant.organization` —algo que el esquema no
tiene— metería una dependencia del dominio dentro de la capa de validación. La regla vive
en el servicio y la ruta la traduce a `403`.

## Por qué `is_admin_reply` no aparece en ningún esquema de entrada

El cliente escribe mensajes con un `POST` y el soporte los escribe con otro, y **los dos
son el mismo endpoint en el servidor pero rutas distintas para el cliente**. El campo se
deduce del rol de quien llama, no de lo que envía. Un esquema que aceptara
`is_admin_reply` permitiría que un cliente sehaciera pasar por soporte, y ese campo es lo
único que el hilo usa para distinguir las dos voces.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.apps.support.models import (
    SupportCategoryEnum,
    TicketPriorityEnum,
    TicketStatusEnum,
)


def _clean_subject(value: str) -> str:
    """Quita los espacios de los extremos y colapsa los internos.

    Un asunto de `"  error   de  login  "` sale como `"error de login"`. Sin esto, dos
    tickets que se ven idénticos en la lista se cuelan como distintos, y el buscador
    posterior tiene que tolerar espacios que nadie quiere.
    """

    return " ".join(value.split())


class TicketCreate(BaseModel):
    """Apertura de un ticket por parte de un cliente."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject: str = Field(min_length=3, max_length=255)
    category: SupportCategoryEnum
    priority: TicketPriorityEnum = TicketPriorityEnum.NORMAL
    #: El primer mensaje. Va en la misma petición que la apertura para que un ticket nunca
    #: exista vacío: un ticket sin texto no dice qué falla, y el agente tendría que pedirlo
    #: en un segundo intercambio.
    message: str = Field(min_length=10, max_length=8000)

    @field_validator("subject")
    @classmethod
    def normalize_subject(cls, value: str) -> str:
        limpio = _clean_subject(value)
        if len(limpio) < 3:
            raise ValueError("El asunto debe tener al menos 3 caracteres sin contar espacios")
        return limpio

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        limpio = value.strip()
        if len(limpio) < 10:
            raise ValueError("La descripción debe tener al menos 10 caracteres")
        return limpio


class TicketMessageCreate(BaseModel):
    """Un mensaje añadido a un hilo ya abierto."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=8000)

    @field_validator("content")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        limpio = value.strip()
        if not limpio:
            raise ValueError("El mensaje no puede estar vacío")
        return limpio


class TicketMessageItem(BaseModel):
    """Un mensaje del hilo, con su autor resuelto."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sender_user_id: UUID
    sender_email: str
    is_admin_reply: bool
    content: str
    created_at: datetime


class TicketItem(BaseModel):
    """Un ticket en la lista, sin su hilo.

    El hilo **no** viaja en el listado. Un cliente con veinte tickets abierta cargaría
    veinte conversaciones enteras para pintar una tabla, y la tabla enseña seis filas por
    página. El detalle se pide aparte, y ya se cargó cuando el usuario abre el ticket.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_number: str
    subject: str
    category: SupportCategoryEnum
    priority: TicketPriorityEnum
    status: TicketStatusEnum
    organization_id: UUID
    organization_name: str
    created_by_user_id: UUID
    created_by_email: str
    assigned_to_user_id: UUID | None
    assigned_to_email: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int = Field(default=0, ge=0)


class TicketDetail(TicketItem):
    """El ticket con su conversación completa, en orden cronológico."""

    messages: list[TicketMessageItem] = Field(default_factory=list)


class TicketPage(BaseModel):
    """Página de tickets."""

    items: list[TicketItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class AdminTicketUpdate(BaseModel):
    """Cambios administrativos sobre un ticket.

    ## Por qué la asignación usa `model_fields_set` y no un valor por defecto

    Asignar a alguien y **desasignar** —devolver el ticket a la cola— son acciones
    distintas, y con `None` como valor por defecto serían la misma: los dos se leerían
    como "sin agente". Un agente que deja la empresa sí tiene tickets abiertos, y su
    baja los devuelve a la cola automáticamente por el `SET NULL` de la columna; devolver
    uno a la cola a mano es la operación de todos los días.

    La distinción sale de `model_fields_set`, que es el conjunto de claves que el cliente
    **envió** —distinto de las que tienen valor—. Solo se toca la asignación si la clave
    vino en el cuerpo. Un `PATCH` con el objeto entero distinguiría "no lo he pensado" de
    "lo he quitado".
    """

    model_config = ConfigDict(extra="forbid")

    status: TicketStatusEnum | None = None
    priority: TicketPriorityEnum | None = None
    assigned_to_user_id: UUID | None = None

    @property
    def toca_asignacion(self) -> bool:
        """`True` si el cuerpo incluía la clave de asignación, viniera o no con valor.

        `model_fields_set` guarda las claves **enviadas**, no las que tienen valor. Sin
        esto, desasignar un ticket —mandar `null`— y no hablar de la asignación serían el
        mismo `None`, y devolver un ticket a la cola sería imposible.
        """

        return "assigned_to_user_id" in self.model_fields_set


class SupportSummary(BaseModel):
    """Recuentos para las tarjetas de la vista de soporte del cliente."""

    open_count: int = Field(ge=0)
    waiting_count: int = Field(ge=0)
    resolved_count: int = Field(ge=0)
    #: Si el tenant puede abrir tickets urgentes. El panel lo lee para decidir si activa el
    #: selector o lo muestra bloqueado: el `403` del servidor sigue siendo la garantía,
    #: pero un control que no se puede usar no debería parecer que se puede.
    can_request_urgent: bool
    plan_tier: Literal["FREE", "PRO", "ENTERPRISE"]
