"""Validación de productos y acuerdos administrados."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonnegativeLimit = Annotated[int, Field(ge=0)]


class PentestProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name_es: str | None = Field(default=None, min_length=1, max_length=128)
    scan_mode: Literal["QUICK", "STANDARD", "DEEP"] | None = None
    name_en: str | None = Field(default=None, min_length=1, max_length=128)
    description_es: str | None = Field(default=None, max_length=2000)
    description_en: str | None = Field(default=None, max_length=2000)
    price_label_es: str | None = Field(default=None, min_length=1, max_length=128)
    price_label_en: str | None = Field(default=None, min_length=1, max_length=128)
    price_min_usd: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    price_max_usd: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    credits_required: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=4)
    max_budget_usd: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    max_turns: int | None = Field(default=None, gt=0)
    features: list[str] | None = None
    limits: dict[str, NonnegativeLimit] | None = None
    is_active: bool | None = None


class PentestProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: str = Field(pattern=r"^[a-z][a-z0-9-]*$", max_length=64)
    scan_mode: Literal["QUICK", "STANDARD", "DEEP"] | None = None
    name_es: str = Field(min_length=1, max_length=128)
    name_en: str = Field(min_length=1, max_length=128)
    price_label_es: str = Field(min_length=1, max_length=128)
    price_label_en: str = Field(min_length=1, max_length=128)
    description_es: str = Field(default="", max_length=2000)
    description_en: str = Field(default="", max_length=2000)
    price_min_usd: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    price_max_usd: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    credits_required: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=4)
    max_budget_usd: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    max_turns: int | None = Field(default=None, gt=0)
    features: list[str] = Field(default_factory=list)
    limits: dict[str, NonnegativeLimit] = Field(default_factory=dict)
    is_active: bool = True


class PentestProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    scan_mode: str | None
    name_es: str
    name_en: str
    description_es: str
    description_en: str
    price_label_es: str
    price_label_en: str
    price_min_usd: Decimal | None
    price_max_usd: Decimal | None
    credits_required: Decimal | None
    max_budget_usd: Decimal | None
    max_turns: int | None
    features: list[str]
    limits: dict[str, int]
    is_active: bool


class EnterpriseAgreementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price_monthly_usd: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    seats: int | None = Field(default=None, gt=0)
    included_credits: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=4)
    discount_pct: Decimal = Field(default=Decimal(0), ge=0, le=100, decimal_places=2)
    max_budget_usd: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    max_turns: int | None = Field(default=None, gt=0)
    features: dict[str, bool] = Field(default_factory=dict)
    limits: dict[str, NonnegativeLimit] = Field(default_factory=dict)
    special_operations: list[str] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validar_vigencia(self) -> EnterpriseAgreementCreate:
        if self.valid_from is not None and self.valid_from.tzinfo is None:
            raise ValueError("valid_from debe tener zona horaria")
        if self.valid_until is not None:
            if self.valid_until.tzinfo is None:
                raise ValueError("valid_until debe tener zona horaria")
            if self.valid_from is not None and self.valid_until <= self.valid_from:
                raise ValueError("valid_until debe ser posterior a valid_from")
        return self


class EnterpriseAgreementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    price_monthly_usd: Decimal | None
    seats: int | None
    included_credits: Decimal
    discount_pct: Decimal
    max_budget_usd: Decimal | None
    max_turns: int | None
    features: dict[str, bool]
    limits: dict[str, int]
    special_operations: list[str]
    valid_from: datetime
    valid_until: datetime | None
    created_at: datetime
