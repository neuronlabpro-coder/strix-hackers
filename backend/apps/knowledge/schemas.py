"""Esquemas del catálogo técnico de remediación."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
