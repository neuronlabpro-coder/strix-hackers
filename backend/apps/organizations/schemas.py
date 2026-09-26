"""Esquemas Pydantic estrictos para autenticación y organizaciones."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from backend.apps.organizations.models import PlanTierEnum, RoleEnum
from backend.core.config import settings


class StrictSchema(BaseModel):
    """Base común para rechazar campos no declarados."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterRequest(StrictSchema):
    email: EmailStr
    password: str = Field(
        min_length=settings.password_min_length, max_length=settings.password_max_length
    )
    full_name: str = Field(min_length=1, max_length=255)
    organization_name: str | None = Field(default=None, min_length=1, max_length=128)


class LoginRequest(StrictSchema):
    email: EmailStr
    password: str = Field(min_length=1, max_length=settings.password_max_length)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str
    is_superuser: bool = False


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    plan_tier: PlanTierEnum
    credit_balance: Decimal
    role: RoleEnum
    created_at: datetime
    updated_at: datetime


class RegisterResponse(BaseModel):
    user: UserResponse
    organization: OrganizationResponse
    verification_required: bool
    verification_token: str | None = None


class OrganizationDeletionResponse(BaseModel):
    """Resultado de una baja lógica.

    Expone los contadores de lo revocado para que el panel pueda decir qué ha pasado
    en lugar de un "operación completada" genérico. Un usuario que da de baja un
    workspace con tres compañeros tiene que ver que a los tres se les ha revocado el
    acceso, porque si no no habra manera de recuperarlo.
    """

    organization_id: UUID
    deleted_at: datetime
    revoked_memberships: int = Field(ge=0)
    cancelled_subscriptions: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)

    @property
    def revoked_access(self) -> int:
        return self.revoked_memberships


class EmailVerificationRequest(StrictSchema):
    token: str = Field(min_length=20, max_length=256)


class EmailVerificationResponse(BaseModel):
    verified: bool


class EmailResendRequest(StrictSchema):
    email: EmailStr


class EmailResendResponse(BaseModel):
    accepted: bool
    verification_token: str | None = None


class OrganizationCreate(StrictSchema):
    name: str = Field(min_length=1, max_length=128)
    slug: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.lower()
        if (
            not normalized.replace("-", "").isalnum()
            or normalized.startswith("-")
            or normalized.endswith("-")
        ):
            raise ValueError("El slug solo puede contener letras, números y guiones")
        return normalized


class InvitationCreate(StrictSchema):
    email: EmailStr
    role: RoleEnum = RoleEnum.MEMBER


class InvitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    role: RoleEnum
    expires_at: datetime
    accepted: bool
    invitation_token: str | None = None


class InvitationAcceptRequest(StrictSchema):
    token: str = Field(min_length=20, max_length=256)


class InvitationAcceptResponse(BaseModel):
    organization_id: UUID
    role: RoleEnum
    accepted: bool
