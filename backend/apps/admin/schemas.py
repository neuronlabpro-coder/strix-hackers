"""Esquemas de la consola de SuperAdmin."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.apps.organizations.models import PlanTierEnum


class DependencyStatusEnum(StrEnum):
    """Estado agregado de una dependencia de infraestructura."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"


class DependencyHealth(BaseModel):
    """Estado de una dependencia concreta, con su latencia observada."""

    status: Literal["online", "offline"]
    latency_ms: int = Field(ge=0)


class InfrastructureHealthResponse(BaseModel):
    """Sondeo de PostgreSQL y Redis para la consola de SuperAdmin."""

    status: DependencyStatusEnum
    database: DependencyHealth
    cache: DependencyHealth
    checked_at: datetime


class AdminOrganizationItem(BaseModel):
    """Organización registrada con su plan y saldo de créditos."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    plan_tier: PlanTierEnum
    credit_balance: float
    created_at: datetime
    updated_at: datetime


class AdminOrganizationPage(BaseModel):
    """Página de organizaciones registradas."""

    items: list[AdminOrganizationItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
