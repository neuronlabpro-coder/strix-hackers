"""Catálogo técnico de remediación compartido entre tenants."""

from backend.apps.knowledge.models import (
    KnowledgeCategoryEnum,
    KnowledgeEntry,
    KnowledgeSeverityEnum,
)

__all__ = ["KnowledgeCategoryEnum", "KnowledgeEntry", "KnowledgeSeverityEnum"]
