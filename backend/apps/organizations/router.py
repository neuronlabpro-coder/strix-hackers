"""Endpoints base de autenticación y organizaciones multi-tenant."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization, RoleEnum, User
from backend.apps.organizations.schemas import (
    InvitationCreate,
    InvitationResponse,
    LoginRequest,
    OrganizationCreate,
    OrganizationResponse,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserResponse,
)
from backend.apps.organizations.services import (
    authenticate_user,
    create_invitation,
    create_organization_for_user,
    list_organizations_for_user,
    register_user_with_initial_organization,
)
from backend.core.config import settings
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant, get_current_user
from backend.core.rate_limit import enforce_login_rate_limit, enforce_register_rate_limit
from backend.core.security import create_access_token

router = APIRouter()
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
CurrentUserDependency = Annotated[User, Depends(get_current_user)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]


def _organization_response(
    organization: Organization,
    role: RoleEnum,
) -> OrganizationResponse:
    return OrganizationResponse(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        plan_tier=organization.plan_tier,
        credit_balance=organization.credit_balance,
        role=role,
        created_at=organization.created_at,
        updated_at=organization.updated_at,
    )


@router.post(
    "/api/v1/auth/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_register_rate_limit)],
)
async def register(
    payload: RegisterRequest,
    session: SessionDependency,
) -> RegisterResponse:
    """Registra un usuario y crea su organización inicial con rol admin."""

    try:
        user, organization, membership = await register_user_with_initial_organization(
            session, payload
        )
    except IntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se pudo completar el registro con esos datos",
        ) from error

    return RegisterResponse(
        user=UserResponse.model_validate(user),
        organization=_organization_response(organization, membership.role),
    )


@router.post(
    "/api/v1/auth/login",
    response_model=TokenResponse,
    dependencies=[Depends(enforce_login_rate_limit)],
)
async def login(
    payload: LoginRequest,
    session: SessionDependency,
) -> TokenResponse:
    """Autentica un usuario y devuelve un access token Bearer."""

    user = await authenticate_user(session, str(payload.email), payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token({"sub": str(user.id), "email": user.email})
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.get("/api/v1/organizations/me", response_model=list[OrganizationResponse])
async def list_my_organizations(
    current_user: CurrentUserDependency,
    session: SessionDependency,
) -> list[OrganizationResponse]:
    """Lista los workspaces del usuario autenticado sin requerir un tenant activo."""

    organizations = await list_organizations_for_user(session, current_user.id)
    return [_organization_response(organization, role) for organization, role in organizations]


@router.post(
    "/api/v1/organizations/",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    payload: OrganizationCreate,
    current_user: CurrentUserDependency,
    session: SessionDependency,
) -> OrganizationResponse:
    """Crea un workspace adicional y lo asocia al usuario como admin."""

    try:
        organization, membership = await create_organization_for_user(
            session, current_user, payload
        )
    except IntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El identificador de organización ya existe",
        ) from error

    return _organization_response(organization, membership.role)


@router.post(
    "/api/v1/organizations/{organization_id}/invite",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    organization_id: UUID,
    payload: InvitationCreate,
    tenant: TenantDependency,
    session: SessionDependency,
) -> InvitationResponse:
    """Crea una invitación después de validar el tenant y el rol admin."""

    if tenant.organization.id != organization_id or tenant.role != RoleEnum.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso a la organización denegado",
        )

    invitation = await create_invitation(session, organization_id, payload)
    return InvitationResponse.model_validate(invitation)
