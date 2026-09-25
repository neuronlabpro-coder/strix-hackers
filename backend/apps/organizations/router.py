"""Endpoints base de autenticación y organizaciones multi-tenant."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Organization, RoleEnum, User
from backend.apps.organizations.schemas import (
    EmailResendRequest,
    EmailResendResponse,
    EmailVerificationRequest,
    EmailVerificationResponse,
    InvitationAcceptRequest,
    InvitationAcceptResponse,
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
    accept_invitation as accept_organization_invitation,
)
from backend.apps.organizations.services import (
    authenticate_user,
    create_invitation,
    create_organization_for_user,
    list_organizations_for_user,
    register_user_with_initial_organization,
    rotate_email_verification_token,
    verify_user_email,
)
from backend.core.config import settings
from backend.core.database import get_db
from backend.core.email import (
    EmailDeliveryError,
    deliver_email_verification,
    deliver_invitation,
)
from backend.core.middleware import TenantContext, get_current_tenant, get_current_user
from backend.core.rate_limit import (
    enforce_create_organization_rate_limit,
    enforce_email_resend_rate_limit,
    enforce_email_verification_rate_limit,
    enforce_invitation_accept_rate_limit,
    enforce_invitation_rate_limit,
    enforce_login_rate_limit,
    enforce_register_rate_limit,
)
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
        (
            user,
            organization,
            membership,
            verification_token,
        ) = await register_user_with_initial_organization(session, payload)
        await deliver_email_verification(user.email, verification_token)
    except EmailDeliveryError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo enviar el email de verificación",
            headers={"Retry-After": "60"},
        ) from error
    except IntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se pudo completar el registro con esos datos",
        ) from error

    return RegisterResponse(
        user=UserResponse.model_validate(user),
        organization=_organization_response(organization, membership.role),
        verification_required=True,
        verification_token=(
            verification_token
            if settings.email_verification_delivery_mode == "development"
            else None
        ),
    )


@router.post(
    "/api/v1/auth/verify-email",
    response_model=EmailVerificationResponse,
    dependencies=[Depends(enforce_email_verification_rate_limit)],
)
async def verify_email(
    payload: EmailVerificationRequest,
    session: SessionDependency,
) -> EmailVerificationResponse:
    """Confirma la propiedad del email y activa el acceso a la cuenta."""

    if not await verify_user_email(session, payload.token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El token de verificación no es válido o ha expirado",
        )
    return EmailVerificationResponse(verified=True)


@router.post(
    "/api/v1/auth/resend-verification",
    response_model=EmailResendResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(enforce_email_resend_rate_limit)],
)
async def resend_email_verification(
    payload: EmailResendRequest,
    session: SessionDependency,
) -> EmailResendResponse:
    """Rota y reenvía el token sin revelar si una cuenta existe."""

    result = await rotate_email_verification_token(session, str(payload.email))
    if result is not None:
        user, verification_token = result
        try:
            await deliver_email_verification(user.email, verification_token)
        except EmailDeliveryError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No se pudo enviar el email de verificación",
                headers={"Retry-After": "60"},
            ) from error

    return EmailResendResponse(
        accepted=True,
        verification_token=(
            result[1]
            if result is not None
            and settings.email_verification_delivery_mode == "development"
            else None
        ),
    )


@router.post(
    "/api/v1/invitations/accept",
    response_model=InvitationAcceptResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(enforce_invitation_accept_rate_limit)],
)
async def accept_invitation(
    payload: InvitationAcceptRequest,
    current_user: CurrentUserDependency,
    session: SessionDependency,
) -> InvitationAcceptResponse:
    """Acepta la invitación únicamente para el usuario que recibió el email."""

    result = await accept_organization_invitation(session, payload.token, current_user.id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La invitación no es válida o ha expirado",
        )
    organization, membership = result
    return InvitationAcceptResponse(
        organization_id=organization.id,
        role=membership.role,
        accepted=True,
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
    dependencies=[Depends(enforce_create_organization_rate_limit)],
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
    dependencies=[Depends(enforce_invitation_rate_limit)],
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
    try:
        await deliver_invitation(invitation.email, invitation.token)
    except EmailDeliveryError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo enviar la invitación por email",
            headers={"Retry-After": "60"},
        ) from error

    return InvitationResponse(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
        accepted=invitation.accepted,
        invitation_token=(
            invitation.token if settings.email_verification_delivery_mode == "development" else None
        ),
    )
