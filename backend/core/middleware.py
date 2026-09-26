"""Dependencias FastAPI para autenticación y aislamiento de tenant."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.core.database import get_db
from backend.core.security import InvalidTokenError, decode_access_token

_bearer_scheme = HTTPBearer(auto_error=False)
CredentialsDependency = Annotated[
    HTTPAuthorizationCredentials | None,
    Depends(_bearer_scheme),
]
SessionDependency = Annotated[AsyncSession, Depends(get_db)]


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Contexto de una membresía autenticada y validada."""

    organization: Organization
    user: User
    membership: Membership

    @property
    def role(self) -> RoleEnum:
        """Devuelve el rol persistido de la membresía actual."""

        return self.membership.role


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _resolve_authenticated_user(
    credentials: HTTPAuthorizationCredentials | None,
    session: AsyncSession,
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("Autenticación bearer requerida")

    try:
        claims = decode_access_token(credentials.credentials)
    except InvalidTokenError as error:
        raise _unauthorized("Token de acceso inválido") from error

    subject = claims.get("sub")
    try:
        user_id = UUID(str(subject))
    except (TypeError, ValueError) as error:
        raise _unauthorized("El token no contiene un usuario válido") from error

    result = await session.execute(
        select(User).where(
            User.id == user_id,
            User.is_active.is_(True),
            User.email_verified.is_(True),
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _unauthorized("Usuario no encontrado o inactivo")
    return user


async def get_current_user(
    credentials: CredentialsDependency,
    session: SessionDependency,
) -> User:
    """Resuelve el usuario activo desde el JWT Bearer."""

    return await _resolve_authenticated_user(credentials, session)


async def get_current_tenant(
    request: Request,
    credentials: CredentialsDependency,
    session: SessionDependency,
) -> TenantContext:
    """Valida usuario, organización activa y membresía antes de proteger un recurso."""

    user = await _resolve_authenticated_user(credentials, session)
    organization_header = request.headers.get("X-Organization-Id")
    if organization_header is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso a la organización denegado",
        )

    try:
        organization_id = UUID(organization_header)
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso a la organización denegado",
        ) from error

    result = await session.execute(
        select(Organization, Membership, User)
        .join(Membership, Membership.organization_id == Organization.id)
        .join(User, User.id == Membership.user_id)
        .where(
            Organization.id == organization_id,
            Membership.organization_id == organization_id,
            Membership.user_id == user.id,
            Membership.is_active.is_(True),
            User.is_active.is_(True),
            # Un tenant dado de baja lógicamente no resuelve contexto. La baja
            # desactiva las membresías, así que esta condición es normalmente
            # redundante; se declara igualmente porque es la que define el estado del
            # workspace, y un tenant desactivado a mano sin pasar por la baja debe
            # seguir siendo inaccesible.
            Organization.is_active.is_(True),
            Organization.deleted_at.is_(None),
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso a la organización denegado",
        )

    organization, membership, authenticated_user = row
    tenant_context = TenantContext(
        organization=organization,
        user=authenticated_user,
        membership=membership,
    )
    request.state.current_tenant = tenant_context
    return tenant_context
