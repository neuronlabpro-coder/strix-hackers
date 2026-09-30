"""Endpoints base de autenticación y organizaciones multi-tenant."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.deletion import _soft_delete_organization
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
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
    MemberListResponse,
    MemberRoleUpdate,
    OrganizationCreate,
    OrganizationDeletionResponse,
    OrganizationResponse,
    OrganizationUpdate,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserResponse,
)
from backend.apps.organizations.services import (
    CannotRemoveSelfError,
    LastAdminRequiredError,
    NoSuchMemberError,
    authenticate_user,
    change_member_role,
    create_invitation,
    create_organization_for_user,
    list_members,
    list_organizations_for_user,
    register_user_with_initial_organization,
    remove_member,
    rename_organization,
    rotate_email_verification_token,
    verify_user_email,
)
from backend.apps.organizations.services import (
    accept_invitation as accept_organization_invitation,
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


@router.get("/api/v1/auth/me", response_model=UserResponse)
async def read_current_user(current_user: CurrentUserDependency) -> UserResponse:
    """Devuelve el perfil autenticado, incluida la marca de superusuario."""

    return UserResponse.model_validate(current_user)


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


@router.delete(
    "/api/v1/organizations/{organization_id}",
    response_model=OrganizationDeletionResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_organization(
    organization_id: UUID,
    tenant: TenantDependency,
    session: SessionDependency,
) -> OrganizationDeletionResponse:
    """Da de baja el workspace actual de forma lógica.

    No borra la fila. R4 hace el borrado físico imposible: los triggers append-only
    sobre `credit_ledger` y `audit_log` bloquean la cascada, y con ella desaparecería
    el rastro financiero y forense que da validez a cada asiento. La baja marca
    `deleted_at`, apaga el tenant, asienta el motivo y revoca el acceso.

    Solo un administrador del propio tenant puede darlo de baja, y el tenant se toma
    del contexto: un `organization_id` de la URL que no coincide con el contexto no
    entra ni se comprueba, para no filtrar la existencia de otros workspaces.
    """

    if tenant.role != RoleEnum.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador para dar de baja el workspace",
        )
    if organization_id != tenant.organization.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace no encontrado",
        )

    report = await _soft_delete_organization(
        session, tenant.organization, tenant.user.id
    )
    return OrganizationDeletionResponse(
        organization_id=report.organization_id,
        deleted_at=report.deleted_at,
        revoked_memberships=report.revoked_memberships,
        cancelled_subscriptions=report.cancelled_subscriptions,
        warnings=report.warnings,
    )


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

    invitation, token = await create_invitation(session, organization_id, payload)
    try:
        await deliver_invitation(invitation.email, token)
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
        # El token solo se devuelve en desarrollo, y solo porque en staging y produccion el
        # correo es el unico canal: publicarlo en la respuesta seria una segunda via de entrega
        # de una credencial que la plataforma no puede volver a mostrar.
        invitation_token=(
            token if settings.email_verification_delivery_mode == "development" else None
        ),
    )


# --------------------------------------------------------------------------- #
# Ajustes del workspace activo
# --------------------------------------------------------------------------- #
#
# Todas estas rutas toman el tenant del **contexto**, no de un parámetro. Un
# `organization_id` en la URL o en el cuerpo sería un filtro que el servidor tendría que
# comprobar contra el contexto para no filtrar nada —R3—; no tenerlo hace que sea imposible
# equivocarse: no hay ningún valor que el cliente pueda manipular.


def _exigir_admin(tenant: TenantContext) -> None:
    """Exige rol `ADMIN` del tenant activo, con excepción del superusuario.

    El superusuario pasa siempre. No por comodidad, sino porque administra la plataforma
    entera: un superusuario que no puede entrar en los ajustes de un workspace que está
    revisando no puede hacer su trabajo. Y no es una vía paralela: la escritura queda
    asentada igual en `audit_log` con su `actor_user_id`, que es donde se comprueba quién
    hizo qué.
    """

    if tenant.role != RoleEnum.ADMIN and not tenant.user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador para modificar el workspace",
        )


@router.patch("/api/v1/organizations/me", response_model=OrganizationResponse)
async def update_my_organization(
    payload: OrganizationUpdate,
    tenant: TenantDependency,
    session: SessionDependency,
) -> OrganizationResponse:
    """Renombra el workspace activo.

    El `slug` no cambia, aunque venga en el cuerpo: `OrganizationUpdate` no lo declara y
    `extra="forbid"` lo convierte en `422`. Preferible a ignorarlo en silencio, que dejaría
    al usuario creyendo que la dirección cambió cuando no lo ha hecho.
    """

    _exigir_admin(tenant)
    organization = await rename_organization(
        session, tenant.organization, payload.name, tenant.user.id
    )
    return _organization_response(organization, tenant.role)


@router.get(
    "/api/v1/organizations/me/members", response_model=MemberListResponse
)
async def list_my_members(
    tenant: TenantDependency,
    session: SessionDependency,
) -> MemberListResponse:
    """Los miembros activos del workspace activo.

    Lo puede leer **cualquier** miembro, no solo el admin. Se parece a la lista de un canal
    de equipo: cualquiera necesita saber quién más está dentro para nouahle por email a
    alguien que ya tiene la respuesta, y el dato que sale —nombre, email, rol— no es nada
    que el propio miembro no pueda ver de otra forma.
    """

    items = await list_members(session, tenant.organization.id)
    return MemberListResponse(items=items, total=len(items))


@router.patch("/api/v1/organizations/me/members/{user_id}", response_model=MemberListResponse)
async def update_member_role(
    user_id: UUID,
    payload: MemberRoleUpdate,
    tenant: TenantDependency,
    session: SessionDependency,
) -> MemberListResponse:
    """Cambia el rol de un miembro del workspace activo.

    ## Por qué un `409` cuando se quedaría sin admin

    Un workspace sin ningún `ADMIN` no se puede administrar. El siguiente `MEMBER` que pida
    permiso se lo deniega él mismo con un `403` que no puede corregir, porque corregirlo
    requiere ser admin. Es un callejón sin salida del que solo se sale por consola.

    Un `409` —y no un `403`— porque la petición es legítima y el estado es el que no
    permite: el admin puede invitar a otro admin y volver. Un `403` diría "no tienes
    permiso", y sí lo tiene.
    """

    _exigir_admin(tenant)
    try:
        await change_member_role(
            session,
            organization=tenant.organization,
            membership=await _membership_de(session, tenant.organization.id, user_id),
            new_role=payload.role,
            actor_user_id=tenant.user.id,
        )
    except LastAdminRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except NoSuchMemberError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error

    items = await list_members(session, tenant.organization.id)
    return MemberListResponse(items=items, total=len(items))


@router.delete(
    "/api/v1/organizations/me/members/{user_id}", response_model=MemberListResponse
)
async def remove_my_member(
    user_id: UUID,
    tenant: TenantDependency,
    session: SessionDependency,
) -> MemberListResponse:
    """Retira a un miembro desactivando su membresía. La fila se conserva.

    `DELETE` y no `PATCH {"is_active": false}` porque para quien llama es una baja: el
    recurso "membresía activa" deja de existir y no hay forma de expresarlo mejor. Lo que
    hay debajo es una desactivación, y eso lo dice el `audit_log`.
    """

    _exigir_admin(tenant)
    try:
        await remove_member(
            session,
            organization=tenant.organization,
            user_id=user_id,
            actor_user_id=tenant.user.id,
        )
    except CannotRemoveSelfError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except LastAdminRequiredError as error:
        # El servicio lanza esta cuando el `UPDATE` no afectó a nadie **porque** habría
        # dejado el workspace sin admin. Sin este `except` se escaparía como `500`, que le
        # diría al superusuario que el servidor falló cuando lo que pasó es que la
        # operación no es válida. Y el `500` no lleva el mensaje: el `detail` de un `500`
        # es genérico, así que el usuario se quedaría sin ninguna explicación.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except NoSuchMemberError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error

    items = await list_members(session, tenant.organization.id)
    return MemberListResponse(items=items, total=len(items))


async def _membership_de(
    session: AsyncSession, organization_id: UUID, user_id: UUID
) -> Membership:
    """Resuelve una membresía **del tenant activo**, con su `404` si no es suya.

    El filtro por organización va en el `WHERE` junto al `user_id`. Filtrar el resultado
    en Python traería la fila de otro workspace a memoria para decidir que no es del
    usuario: es R3 resuelto tarde y en el sitio equivocado.
    """

    membership = (
        await session.execute(
            select(Membership).where(
                Membership.organization_id == organization_id,
                Membership.user_id == user_id,
                Membership.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise NoSuchMemberError("Ese miembro no pertenece a este workspace")
    return membership
