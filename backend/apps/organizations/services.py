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


async def rotate_email_verification_token(
    session: AsyncSession,
    email: str,
) -> tuple[User, str] | None:
    """Rota el token de una cuenta pendiente y lo persiste antes del envío."""

    result = await session.execute(
        select(User).where(
            User.email == normalize_email(email),
            User.is_active.is_(True),
            User.email_verified.is_(False),
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        return None

    verification_token = secrets.token_urlsafe(32)
    user.email_verification_token_hash = hash_email_verification_token(verification_token)
    user.email_verification_expires_at = datetime.now(UTC) + timedelta(
        minutes=settings.email_verification_ttl_minutes
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise
    return user, verification_token


async def accept_invitation(
    session: AsyncSession,
    token: str,
    user_id: uuid.UUID,
) -> tuple[Organization, Membership] | None:
    """Acepta una invitación válida para el usuario autenticado."""

    result = await session.execute(
        select(Invitation, Organization, User)
        .join(Organization, Organization.id == Invitation.organization_id)
        .join(User, User.email == Invitation.email)
        .where(
            Invitation.token == token.strip(),
            Invitation.accepted.is_(False),
            Invitation.expires_at > datetime.now(UTC),
            User.id == user_id,
            User.is_active.is_(True),
            User.email_verified.is_(True),
        )
    )
    row = result.one_or_none()
    if row is None:
        return None

    invitation, organization, user = row
    if normalize_email(user.email) != normalize_email(invitation.email):
        return None

    existing_result = await session.execute(
        select(Membership).where(
            Membership.organization_id == organization.id,
            Membership.user_id == user.id,
            Membership.is_active.is_(True),
        )
    )
    existing_membership = existing_result.scalar_one_or_none()
    if existing_membership is not None:
        invitation.accepted = True
        await session.commit()
        return organization, existing_membership

    membership = Membership(
        organization_id=organization.id,
        user_id=user.id,
        role=invitation.role,
    )
    session.add(membership)
    invitation.accepted = True
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise
    return organization, membership


async def list_organizations_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> list[tuple[Organization, RoleEnum]]:
    """Lista únicamente organizaciones con membresía activa del usuario.

    Filtra además por `deleted_at IS NULL`. La baja lógica desactiva las membresías,
    así que el filtro de `Membership.is_active` ya lo excluye en la práctica; el
    `deleted_at` se comprueba igualmente porque es la condición que **declara** el
    estado del tenant, y depender de un efecto secundario para que un workspace
    desapareciera sería confiar en que nada reintroduce la fila.
    """

    result = await session.execute(
        select(Organization, Membership.role)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(
            Membership.user_id == user_id,
            Membership.is_active.is_(True),
            Organization.deleted_at.is_(None),
            Organization.is_active.is_(True),
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
