"""Esquemas del conocimiento: catálogo técnico compartido y documentos del cliente."""

from __future__ import annotations

import uuid
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.apps.knowledge.documents import KnowledgeDocTypeEnum
from backend.apps.knowledge.models import KnowledgeCategoryEnum, KnowledgeSeverityEnum


class KnowledgeSummary(BaseModel):
    """Ficha resumida para el catálogo buscable."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reference_code: str
    title: str
    category: KnowledgeCategoryEnum
    severity: KnowledgeSeverityEnum
    risk_summary: str
    owasp_category: str


class KnowledgeDetail(KnowledgeSummary):
    """Ficha completa con ejemplos de código y mitigación."""

    vulnerable_example: str
    secure_example: str
    mitigation: str
    updated_at: datetime


class KnowledgePage(BaseModel):
    """Página del catálogo de remediación."""

    items: list[KnowledgeSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)



# --------------------------------------------------------------------------- #
# Base de conocimiento corporativa
# --------------------------------------------------------------------------- #


class KnowledgeDocumentCreate(BaseModel):
    """Alta de un documento de contexto del workspace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=255)
    doc_type: KnowledgeDocTypeEnum
    #: Markdown, OpenAPI o texto plano. Sin limite de longitud a proposito: una especificacion
    #: de API completa son varios miles de lineas, y un tope obligaria al usuario a partirla en
    #: trozos que luego no significan nada por separado.
    content: str = Field(min_length=1)


class KnowledgeDocumentItem(BaseModel):
    """Un documento en la respuesta, sin su contenido.

    El listado devuelve la **cabecera** y no el cuerpo a proposito. Un cliente con veinte
    documentos de especificacion pesa varios megabytes, y la lista se pide para mostrarlos en
    una tabla, no para leerlos. El cuerpo llega al pedir un documento concreto.

    ## Por qué `description` sí viaja y el cuerpo no

    Porque `description` es **una línea del frontmatter** y el cuerpo son varios miles. La tarjeta
    del panel enseña el título y la descripción —qué es este documento y de qué trata—, y sin la
    descripción la tarjeta solo tiene el título, que es lo que el cliente ya escribió al guardar y
    por lo tanto no le dice nada nuevo.

    Y no es un campo trivial: es la descripción que el motor inyecta cuando el documento se cita
    sin su cuerpo entero. Es la misma línea para el motor y para la persona.

    ## El defecto que este campo arregla, y que no era visible

    Antes de que existiera, la tarjeta del panel sacaba la descripción del `content` del
    **listado**, y el listado no devuelve el `content`. La expresión era
    `extraerParaFormulario(documento.content)` con `documento.content` siendo `undefined`, y
    `dividirDocumento` hacía `contenido.replace(...)` encima: la pantalla entera reventaba con
    «Cannot read properties of undefined» en cuanto el workspace tenía **un** documento.

    No se vio porque el workspace de demostración no tenía ninguno, así que la vista nunca llegó
    a pintar una tarjeta. Un fallo que solo aparece cuando hay datos es el que más cuesta
    encontrar, y por eso el campo se calcula **en el servidor**, donde está el documento entero.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    doc_type: KnowledgeDocTypeEnum
    description: str = ""
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentDetail(KnowledgeDocumentItem):
    """Un documento con su contenido completo."""

    content: str


class KnowledgeDocumentPage(BaseModel):
    """Página del listado de documentos del workspace.

    ## Por qué `limit` y `offset` viajan además de `total`

    Porque la barra de paginación del panel necesita saber el tamaño de página en uso y por
    dónde va la ventana para dibujar el resumen «1 de 50 hasta 50 de 340» y decidir si hay
    botón de «siguiente». Con `total` solo no se puede: el cliente tendría que inventar el
    tamaño de página, y cada pantalla inventaría uno distinto. Los tres vienen del servidor,
    que es quien los aplica.
    """

    documents: list[KnowledgeDocumentItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
