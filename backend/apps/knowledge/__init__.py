"""Conocimiento del producto: catalogo tecnico compartido y documentos del cliente.

Son dos cosas distintas, en dos tablas distintas, y el motivo esta escrito al principio de
`documents.py`. La version corta: `knowledge_entries` clasifica **fallos** y es de solo
lectura; `workspace_knowledge_documents` clasifica **documentos** y es de cada tenant.
"""

from backend.apps.knowledge.documents import (
    KnowledgeDocTypeEnum,
    WorkspaceKnowledgeDocument,
)
from backend.apps.knowledge.models import (
    KnowledgeCategoryEnum,
    KnowledgeEntry,
    KnowledgeSeverityEnum,
)

__all__ = [
    "KnowledgeCategoryEnum",
    "KnowledgeDocTypeEnum",
    "KnowledgeEntry",
    "KnowledgeSeverityEnum",
    "WorkspaceKnowledgeDocument",
]
