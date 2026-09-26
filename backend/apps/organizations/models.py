"""Modelos relacionales multi-tenant de organizaciones y usuarios."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.database import Base


class RoleEnum(StrEnum):
    """Roles disponibles dentro de una organización."""

    ADMIN = "admin"
    MEMBER = "member"


class PlanTierEnum(StrEnum):
    """Planes comerciales disponibles para una organización."""

    FREE = "FREE"
    PRO = "PRO"
    ENTERPRISE = "ENTERPRISE"


class TimestampMixin:
    """Marcas temporales gestionadas por PostgreSQL."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Organization(TimestampMixin, Base):
    """Entidad raíz de aislamiento multi-tenant.

    ## Borrado lógico, no físico

    Un tenant **nunca** se borra físicamente. R4 vuelve imposible: los triggers
    `BEFORE DELETE` sobre `credit_ledger` y `audit_log` bloquean la cascada que
    eliminaría el rastro financiero y forense, y con él la validez probatoria de cada
    asiento.

    La baja es, por tanto, lógica: `deleted_at` marca la fecha y `is_active` apaga el
    tenant. La fila sobrevive porque el ledger y el rastro de auditoría apuntan a ella, y
    porque un asiento financiero que pierde a su organización deja de ser explicable.

    `is_active` e `is_active` de `Membership` son cosas distintas y las dos hacen falta:
    el primero apaga el espacio de trabajo entero, el segundo revoca a una persona
    concreta sin tocar a las demás.
    """

    __tablename__ = "organizations"
    __table_args__ = (Index("ix_organizations_deleted_at", "deleted_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    # `NULL` significa tenant vivo. No se usa un centinela tipo epoch porque
    # `deleted_at` tiene que distinguir "nunca se dio de baja" de "se dio de baja en
    # algún momento", y ambas cosas tienen consecuencias distintas en un informe.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Cliente de Stripe asociado. Sin este campo la plataforma no puede localizar al
    # cliente para cancelar una suscripción: cada sesión de Checkout crearía un
    # cliente nuevo y la organización quedaría sin identidad en el proveedor de pago.
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, default=None
    )
    plan_tier: Mapped[PlanTierEnum] = mapped_column(
        SQLEnum(PlanTierEnum, name="plan_tier_enum"),
        nullable=False,
        default=PlanTierEnum.PRO,
        server_default=PlanTierEnum.PRO.name,
    )
    # Numérico exacto, no coma flotante: es la caché denormalizada del ledger de
    # créditos y un saldo que deriva de centavo hace que el historial no cuadre.
    # Numérico exacto, no coma flotante: es la caché denormalizada del ledger de
    # créditos y un saldo que deriva de centavo hace que el historial no cuadre.
    # Con 1 crédito = 1 USD, `Numeric(12, 4)` cubre 99.999.999,99 créditos.
    credit_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, default=Decimal("0"), server_default="0"
    )

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    invitations: Mapped[list[Invitation]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class User(TimestampMixin, Base):
    """Usuario autenticable de la plataforma."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    email_verification_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Membership(TimestampMixin, Base):
    """Relación activa entre un usuario y una organización."""

    __tablename__ = "memberships"
    __table_args__ = (Index("ix_membership_org_user", "organization_id", "user_id", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[RoleEnum] = mapped_column(
        SQLEnum(RoleEnum, name="role_enum"),
        nullable=False,
        default=RoleEnum.MEMBER,
        server_default=RoleEnum.MEMBER.name,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class Invitation(TimestampMixin, Base):
    """Invitación pendiente para incorporarse a una organización."""

    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[RoleEnum] = mapped_column(
        SQLEnum(RoleEnum, name="role_enum"),
        nullable=False,
        default=RoleEnum.MEMBER,
        server_default=RoleEnum.MEMBER.name,
    )
    token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    organization: Mapped[Organization] = relationship(back_populates="invitations")
