"""Base de conocimiento corporativa del workspace.

## Por que **otra** tabla y no una fila mas en `knowledge_entries`

Porque las dos cosas no se parecen en nada y mezclarlas rompe las dos.

`knowledge_entries` es la **taxonomía técnica** del escáner: una fila por familia de
vulnerabilidad, con código vulnerable, código seguro y mitigación. No pertenece a ninguna
organización, es de solo lectura y la mantiene una migración. Sus categorías son
`INJECTION`, `XSS`, `SSRF`… porque lo que clasifica es el fallo.

`workspace_knowledge_documents` es la **documentación del cliente**: por qué ese endpoint es de
solo lectura, qué campo es un identificador falso, qué tabla nunca se toca. Sus categorías son
`DOCUMENTATION`, `BUSINESS_RULE`, `API_SPEC`, `ARCHITECTURE` porque lo que clasifica es el
documento.

Meter una columna `organization_id` en `knowledge_entries` obligaría a decidir qué se hace con
las 10 filas técnicas cuando un tenant las lee — ¿son suyas? ¿se comparten? — y la respuesta
no tiene forma buena. Dos tablas, dos vidas, cero ambigüedad.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


class KnowledgeDocTypeEnum(StrEnum):
    """Naturaleza del documento que el cliente aporta.

    No es una taxonomía de severidad ni de familia de fallo, y no se cruza con
    `KnowledgeCategoryEnum`. Es una pregunta distinta: qué **es** este texto, para que el motor
    sepa si puede usarlo como contexto de análisis o solo como referencia de lectura.
    """

    #: Documentación general: guías, manuales, onboarding del equipo.
    DOCUMENTATION = "DOCUMENTATION"
    #: Regla de negocio: lo que el producto considera aceptable o no en su contexto.
    BUSINESS_RULE = "BUSINESS_RULE"
    #: Especificación de API: OpenAPI, Swagger o un esquema equivalente.
    API_SPEC = "API_SPEC"
    #: Diagrama o descripción de despliegue, servicios y dependencias.
    ARCHITECTURE = "ARCHITECTURE"


class WorkspaceKnowledgeDocument(Base):
    """Un documento de contexto propiedad de un workspace.

    Sirve para que un escaneo no marque como vulnerabilidad algo que en ese producto es
    deliberado: un endpoint de solo lectura, un campo que parece un identificador y es fijo, una
    tabla de pruebas que existe a propósito.
    """

    __tablename__ = "workspace_knowledge_documents"
    __table_args__ = (
        Index("ix_workspace_knowledge_documents_organization_id", "organization_id"),
        Index("ix_workspace_knowledge_documents_doc_type", "doc_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: `CASCADE` y no `RESTRICT`. El documento es contenido del workspace, no evidencia: si el
    #: cliente se da de baja, sus documentos se van con el. Lo contrario —un `RESTRICT` que
    #: impide dar de baja un tenant hasta que borre sus documentos a mano— convierte una baja
    #: en un trámite, y el botón de baja es de los que un usuario necesita sin fricción.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)

    doc_type: Mapped[KnowledgeDocTypeEnum] = mapped_column(
        SQLEnum(KnowledgeDocTypeEnum, name="knowledge_doc_type_enum"),
        nullable=False,
    )

    #: Markdown, OpenAPI en YAML o JSON, o texto plano. Un `Text` y no un `JSONB` porque el
    #: contenido no se consulta por campo: se indexa entero y se busca dentro. Guardarlo
    #: estructurado obligaria a parsear documentos que el usuario pegaran tal cual, y un
    #: documento que falla al pegar es un documento que no se guarda.
    content: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
