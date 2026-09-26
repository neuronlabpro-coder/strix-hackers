"""Esquemas de la API de webhooks.

La respuesta de alta es el único momento en que el secreto de firma existe fuera del
proceso que lo generó, y por eso tiene su propio esquema: no es un campo más de la lista,
es el campo que no se puede recuperar. Un esquema único con un campo opcional que se
rellena solo en el `POST` convertiría "el secreto aparece en el listado" en un fallo el día
que alguien serialice sin el campo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.apps.webhooks.events import EVENT_CATALOG, EVENTS_BY_GROUP, normalize_events
from backend.apps.webhooks.models import (
    FAILURE_AUTO_DISABLE_THRESHOLD,
    WebhookDelivery,
    WebhookEndpoint,
)
from backend.core.ssrf import MAX_RESPONSE_BODY_CHARS


class WebhookCreate(BaseModel):
    """Alta de un endpoint de webhook."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=8, max_length=2048)
    description: str | None = Field(default=None, max_length=255)
    event_types: list[str] = Field(min_length=1)

    @field_validator("event_types")
    @classmethod
    def _validate_events(cls, value: list[str]) -> list[str]:
        """Rechaza los eventos desconocidos **nombrándolos**.

        Sin nombrarlos, el cliente tiene que comparar listas a mano para encontrar un typo.
        Se normaliza al orden del catálogo para que dos peticiones equivalentes produzcan
        la misma fila y el panel no las muestre como endpoints distintos.
        """

        return normalize_events(value)


class WebhookUpdate(BaseModel):
    """Cambios sobre un endpoint existente.

    Todos los campos son opcionales y se aplican los que vengan. Un `PATCH` con campos
    `None` se trata como "no lo has enviado" y no como "ponlo a null", que es la
    diferencia entre no poder borrar una descripción y borrarla por accidente.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str | None = Field(default=None, min_length=8, max_length=2048)
    description: str | None = Field(default=None, max_length=255)
    event_types: list[str] | None = Field(default=None, min_length=1)
    is_active: bool | None = None

    @field_validator("event_types")
    @classmethod
    def _validate_events(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return normalize_events(value)

    def changes(self) -> dict[str, Any]:
        """Solo los campos presentes en el cuerpo.

        Se usa `exclude_unset` y no "lo que no sea `None`" a propósito: un cliente que
        mande `{"is_active": false}` tiene que poder **desactivar** un endpoint, y
        confundirlo con "ausente" haría que no se podría desactivar nunca.
        """

        return self.model_dump(exclude_unset=True)


class WebhookResponse(BaseModel):
    """Metadatos de un endpoint.

    No expone `encrypted_secret`. No es que se oculte en la respuesta: la respuesta se
    construye con una lista explícita de campos, así que una columna nueva en el modelo no
    aparece por descuido.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    description: str | None
    event_types: list[str]
    is_active: bool
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime

    @property
    def is_auto_disabled(self) -> bool:
        """`True` si llegó al umbral de fallos y se apagó solo.

        Se expone para que el panel pueda explicar **por qué** un endpoint está inactivo.
        Sin esto, un endpoint desactivado parece un endpoint que el usuario desactivó, y
        la diferencia entre las dos cosas es precisamente lo que el usuario viene a
        averiguar.
        """

        return not self.is_active and self.consecutive_failures >= FAILURE_AUTO_DISABLE_THRESHOLD

    @classmethod
    def from_model(cls, endpoint: WebhookEndpoint) -> Self:
        return cls(
            id=endpoint.id,
            url=endpoint.url,
            description=endpoint.description,
            event_types=list(endpoint.event_types),
            is_active=endpoint.is_active,
            consecutive_failures=endpoint.consecutive_failures,
            created_at=endpoint.created_at,
            updated_at=endpoint.updated_at,
        )


class WebhookCreatedResponse(WebhookResponse):
    """Alta con el secreto de firma, una única vez."""

    signing_secret: str = Field(
        description="Secreto `whsec_` para verificar la firma. Solo se devuelve aquí.",
        repr=False,
    )


class WebhookPage(BaseModel):
    """Listado de endpoints del tenant activo."""

    items: list[WebhookResponse]
    total: int


class WebhookDeliveryResponse(BaseModel):
    """Un intento de entrega, con el cuerpo que salió.

    El payload viaja en la respuesta porque el diagnóstico de una entrega fallida es
    **comparar** los dos cuerpos: si el receptor devuelve `400`, la causa casi siempre
    está en el JSON que recibió, y sin verlo el cliente solo tiene un código y un
    `error_message` que no dice qué pidió mal.

    ## Por qué no va en un endpoint de detalle aparte

    Serían 20 peticiones extra al desplegar el acordeón de una fila, para leer un campo
    que la plataforma ya tiene en la fila que está leyendo. El tamaño está acotado porque
    el payload lo construye la propia plataforma en `enqueue_event` y no lo elige el
    cliente. Cuando algún día un evento lleve un adjunto grande, el lugar de partir es
    un endpoint de detalle, y el cambio no rompe a nadie porque `payload` es aditivo.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    status_code: int | None
    response_body: str | None
    execution_time_ms: int | None
    attempt: int
    error_message: str | None
    delivered_at: datetime

    @classmethod
    def from_model(cls, delivery: WebhookDelivery) -> Self:
        return cls(
            id=delivery.id,
            endpoint_id=delivery.endpoint_id,
            event_type=delivery.event_type,
            payload=delivery.payload,
            status_code=delivery.status_code,
            response_body=delivery.response_body,
            execution_time_ms=delivery.execution_time_ms,
            attempt=delivery.attempt,
            error_message=delivery.error_message,
            delivered_at=delivery.delivered_at,
        )


class WebhookDeliveryPage(BaseModel):
    """Historial de entregas de un endpoint, paginado."""

    items: list[WebhookDeliveryResponse]
    total: int
    limit: int
    offset: int

    @property
    def response_truncated(self) -> bool:
        """`True` si algún cuerpo llegó recortado.

        Se declara en la respuesta para que el panel pueda decirlo. Un cuerpo recortado
        sin aviso se lee como completo, y el usuario busca en un JSON un mensaje que se
        comió el límite.
        """

        return any(
            item.response_body is not None and len(item.response_body) > MAX_RESPONSE_BODY_CHARS
            for item in self.items
        )


class WebhookPingResult(BaseModel):
    """Resultado de una prueba de conexión."""

    delivered: bool
    status_code: int | None
    execution_time_ms: int | None
    error_message: str | None
    delivery_id: uuid.UUID


class WebhookEventDefinitionResponse(BaseModel):
    """Un evento del catálogo y sus metadatos de presentación."""

    event_type: str
    group: str
    action: str
    label_key: str
    description_key: str


class WebhookEventGroupResponse(BaseModel):
    """Los eventos de un recurso, en el orden del catálogo."""

    group: str
    events: list[WebhookEventDefinitionResponse]


class WebhookEventCatalogResponse(BaseModel):
    """Los eventos disponibles, agrupados.

    El panel los pide en vez de tenerlos escritos, por la misma razón que los scopes: un
    catálogo duplicado en el cliente es un segundo sitio donde un evento puede existir sin
    que el backend lo emita, y el síntoma sería una casilla que se marca y luego nunca
    suena.
    """

    groups: list[WebhookEventGroupResponse]
    total: int


def event_catalog_response() -> WebhookEventCatalogResponse:
    """Construye la respuesta del catálogo desde la definición del módulo."""

    return WebhookEventCatalogResponse(
        groups=[
            WebhookEventGroupResponse(
                group=group,
                events=[
                    WebhookEventDefinitionResponse(
                        event_type=definition.event_type.value,
                        group=definition.group,
                        action=definition.action,
                        label_key=definition.label_key,
                        description_key=definition.description_key,
                    )
                    for definition in definitions
                ],
            )
            for group, definitions in EVENTS_BY_GROUP.items()
        ],
        total=len(EVENT_CATALOG),
    )
