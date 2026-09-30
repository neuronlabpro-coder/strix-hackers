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


async def resolve_tenant_for_user(
    session: AsyncSession,
    user: User,
    organization_id: UUID,
) -> TenantContext | None:
    """Resuelve el contexto de tenant de un usuario ya autenticado, o `None`.

    Se extrae de `get_current_tenant` para que la autenticación dual pueda reutilizar la
    **misma** consulta en vez de copiarla. Copiarla garantizaría que las dos rutas se
    desincronizasen en cuanto se añadiera una condición —un tenant dado de baja, un
    usuario desactivado— y el fallo aparecería solo en la ruta nueva, que es la que
    nadie revisa porque la otra tiene años de tests.

    `None` en lugar de excepción para que quien llama decida el código de salida: una
    sesión web sin cabecera de organización y un token de API sin tenant no merecen el
    mismo error.
    """

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
            # Un tenant dado de baja lógicamente no resuelve contexto. La baja desactiva
            # las membresías, así que esta condición es normalmente redundante; se declara
            # igualmente porque es la que define el estado del workspace, y un tenant
            # desactivado a mano sin pasar por la baja debe seguir siendo inaccesible.
            Organization.is_active.is_(True),
            Organization.deleted_at.is_(None),
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    organization, membership, authenticated_user = row
    return TenantContext(
        organization=organization,
        user=authenticated_user,
        membership=membership,
    )


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

    tenant_context = await resolve_tenant_for_user(session, user, organization_id)
    if tenant_context is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso a la organización denegado",
        )

    request.state.current_tenant = tenant_context
    return tenant_context


TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]


async def exigir_admin_del_tenant(tenant: TenantDependency) -> None:
    """Falla cerrado si quien llama no es `ADMIN` del workspace, o superusuario.

    ## Por qué vive aquí y no en cada router

    Porque «escribir exige `ADMIN`» es una regla de la plataforma, no de un módulo. Estaba
    escrita cuatro veces —en `assets`, en `agents`, y las copias que hicieron `knowledge` y
    `supply_chain`— y cuatro copias de una regla de autorización son cuatro reglas que
    divergen en cuanto una cambia. La divergencia que ya existía: dos usaban `Depends` y
    llamaban a la función, dos la metían en el `dependencies=[...]` del decorador, y solo
    una de esas cuatro formas funciona con FastAPI (ver abajo).

    ## Por qué `async` y por qué el alias, y no `TenantContext` a secas

    Porque con la anotación suelta, `Depends(exigir_admin_del_tenant)` le dice a FastAPI que
    `TenantContext` es un **modelo de respuesta**: lo lee como un `dataclass` que Pydantic tiene
    que convertir, no como un parámetro que ya tiene un valor, y la aplicación no arranca con

        `FastAPIError: Invalid args for response field! Hint: check that <class
        'backend.core.middleware.TenantContext'> is a valid Pydantic field type.`

    El alias `Annotated[TenantContext, Depends(get_current_tenant)]` lleva la dependencia
    declarada, así que FastAPI sabe que hay que resolverla antes de llamar. Es el mismo motivo
    por el que `enforce_pentest_rate_limit` declara `tenant: TenantDependency`.

    ## Por qué también pasa el superusuario

    Porque la consola de plataforma puede tener que reparar el contexto de un cliente, y negarle
    eso obliga a pedir a alguien del cliente que lo haga.

    Y por qué `is not` en vez de `!=`: `RoleEnum` es un enumerado de `str`, y con `str` el
    operador `!=` no lanza pero compara contenido, de modo que un `"ADMIN"` suelto —el tipo
    que llega de un `dict` de una prueba o de un descodificador— pasaría como si fuera el
    enumerado. `is not` no acepta el impostor.
    """

    if tenant.role is not RoleEnum.ADMIN and not tenant.user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere permiso de administrador para esta operación",
        )


AdminRequired = Depends(exigir_admin_del_tenant)
