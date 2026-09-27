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
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    doc_type: KnowledgeDocTypeEnum
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentDetail(KnowledgeDocumentItem):
    """Un documento con su contenido completo."""

    content: str


class KnowledgeDocumentPage(BaseModel):
    documents: list[KnowledgeDocumentItem]
    total: int
