"""Operaciones de persistencia para usuarios, organizaciones e invitaciones."""

import hashlib
import re
import secrets
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import (
    Invitation,
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.organizations.schemas import (
    InvitationCreate,
    OrganizationCreate,
    RegisterRequest,
)
from backend.core.config import settings
from backend.core.security import hash_password, verify_password


def normalize_email(email: str) -> str:
    """Normaliza el email para impedir duplicados por mayúsculas o espacios."""

    return email.strip().lower()


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug or "workspace"


def _unique_slug(value: str) -> str:
    return f"{_slugify(value)[:119]}-{uuid.uuid4().hex[:8]}"


def hash_email_verification_token(token: str) -> str:
    """Almacena únicamente el hash del token de verificación de email."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def register_user_with_initial_organization(
    session: AsyncSession,
    payload: RegisterRequest,
) -> tuple[User, Organization, Membership, str]:
    """Crea usuario, organización inicial y membresía administradora en una transacción."""

    email = normalize_email(str(payload.email))
    verification_token = secrets.token_urlsafe(32)
    try:
        user = User(
            email=email,
            email_verified=False,
            email_verification_token_hash=hash_email_verification_token(verification_token),
            email_verification_expires_at=datetime.now(UTC)
            + timedelta(minutes=settings.email_verification_ttl_minutes),
            hashed_password=hash_password(payload.password),
            full_name=payload.full_name,
        )
        session.add(user)
        await session.flush()

        organization = Organization(
            name=payload.organization_name or payload.full_name,
            slug=_unique_slug(email.split("@", maxsplit=1)[0]),
        )
        session.add(organization)
        await session.flush()

        membership = Membership(
            organization_id=organization.id,
            user_id=user.id,
            role=RoleEnum.ADMIN,
        )
        session.add(membership)
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise

    return user, organization, membership, verification_token


async def authenticate_user(session: AsyncSession, email: str, password: str) -> User | None:
    """Busca un usuario activo y valida su contraseña."""

    result = await session.execute(
        select(User).where(
            User.email == normalize_email(email),
            User.is_active.is_(True),
            User.email_verified.is_(True),
        )
    )
    user = result.scalar_one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
        return None
    return user


async def verify_user_email(session: AsyncSession, token: str) -> bool:
    """Activa la cuenta solo con un token vigente y de un solo uso."""

    token_hash = hash_email_verification_token(token.strip())
    result = await session.execute(
        select(User).where(
            User.email_verification_token_hash == token_hash,
            User.email_verification_expires_at > datetime.now(UTC),
            User.email_verified.is_(False),
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        return False

    user.email_verified = True
    user.email_verification_token_hash = None
    user.email_verification_expires_at = None
    await session.commit()
    return True


async def list_organizations_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> list[tuple[Organization, RoleEnum]]:
    """Lista únicamente organizaciones con membresía activa del usuario."""

    result = await session.execute(
        select(Organization, Membership.role)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(
            Membership.user_id == user_id,
            Membership.is_active.is_(True),
        )
        .order_by(Organization.created_at.asc())
    )
    return list(result.all())


async def create_organization_for_user(
    session: AsyncSession,
    user: User,
    payload: OrganizationCreate,
) -> tuple[Organization, Membership]:
    """Crea un workspace y membresía administradora para el usuario autenticado."""

    slug = payload.slug or _unique_slug(payload.name)
    try:
        organization = Organization(name=payload.name, slug=slug)
        session.add(organization)
        await session.flush()

        membership = Membership(
            organization_id=organization.id,
            user_id=user.id,
            role=RoleEnum.ADMIN,
        )
        session.add(membership)
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise

    return organization, membership


async def create_invitation(
    session: AsyncSession,
    organization_id: uuid.UUID,
    payload: InvitationCreate,
) -> Invitation:
    """Crea una invitación no destructiva con expiración configurable."""

    try:
        invitation = Invitation(
            organization_id=organization_id,
            email=normalize_email(str(payload.email)),
            role=payload.role,
            token=secrets.token_urlsafe(32),
            expires_at=datetime.now(UTC) + timedelta(days=settings.invitation_expire_days),
        )
        session.add(invitation)
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise

    return invitation
