"""Autenticación dual: sesión web (JWT) o credencial de servicio (token de API).

## El problema que resuelve

La plataforma tiene dos clases de cliente con necesidades opuestas. Un usuario en un
navegador tiene sesión, identidad y rol dentro de un tenant. Un sistema tiene un token
estático y una lista explícita de permisos, y no es nadie. Forzar a uno a usar el modelo
del otro significa que el token hereda un rol —y entonces `role = ADMIN` es un permiso
que concede una cadena de texto—, o que la sesión tiene que declarar scopes que nadie
va a leer.

Hay dos sujetos y una dependencia que acepta los dos.

## La asimetría es deliberada, no una comodidad

Un token **nunca** es administrador. `TokenPrincipal.role` es `None` y no hay forma de
que un token convierta un `MEMBER` en `ADMIN`, porque scopes y roles son ejes distintos:
los scopes conceden acciones sobre los recursos del tenant, el rol decide sobre las
personas del tenant. Un token con `members:invite` puede invitar; un usuario `MEMBER`
no, aunque vea la misma pantalla.

Y un usuario web **no** necesita scopes. Entra por sesión y opera sus recursos según su
rol, que es el flujo que ya existía. Exigirle un scope a alguien que ha iniciado sesión
en su propio workspace sería inventar un permiso que el panel no puede asignar.

## Por qué `last_used_at` no se escribe en cada petición

Escribir en cada petición autenticada convertiría cada listado en un `UPDATE` y llenaría
la base de reescrituras sin aportar información: "usado en algún momento de los últimos
cinco minutos" es todo lo que el panel necesita para decidir si un token está en uso.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TypeGuard
from uuid import UUID

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.api_access.models import API_TOKEN_PREFIX, ApiToken
from backend.apps.api_access.scopes import Scope
from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.core.middleware import (
    CredentialsDependency,
    SessionDependency,
    _resolve_authenticated_user,
    resolve_tenant_for_user,
)
from backend.core.security import hash_api_token

logger = logging.getLogger(__name__)

#: Ventana mínima entre dos escrituras de `last_used_at`. Cinco minutos es un
#: compromiso: el panel dice "usado hace 4 min" con una exactitud que nadie necesita
#: mejorar, y una petición autenticada sigue sin escribir en la base.
LAST_USED_WRITE_THROTTLE = timedelta(minutes=5)

#: Mensaje exacto que el contrato de la API pública promete. Se declara como constante
#: para que un cliente pueda compararlo y para que no dependa de cómo redacte cada
#: llamada al 403.
INSUFFICIENT_SCOPE_DETAIL = "Insufficient token permissions"


class PrincipalKind(StrEnum):
    """De qué clase de cliente viene la petición."""

    USER = "user"
    TOKEN = "token"  # noqa: S105 - es el nombre del sujeto, no un secreto


@dataclass(frozen=True, slots=True)
class UserPrincipal:
    """Sujeto de una sesión web: una persona con un rol dentro de un tenant."""

    organization: Organization
    user: User
    membership: Membership

    @property
    def kind(self) -> PrincipalKind:
        return PrincipalKind.USER

    @property
    def role(self) -> RoleEnum:
        return self.membership.role

    @property
    def is_admin(self) -> bool:
        return self.membership.role == RoleEnum.ADMIN

    # `UserPrincipal` **no** tiene `has_scope`, y esa ausencia es deliberada.
    #
    # La primera versión lo tenía y devolvía `True` siempre, con la intención de que
    # "un usuario web no se filtra por scopes". El efecto real fue peor: `require_scope`
    # consultaba `has_scope` antes de mirar el rol, así que un `MEMBER` pasaba cualquier
    # comprobación de scope y podía emitir tokens con todos los permisos. Una escalada
    # de privilegios con la autorización escrita al revés, y la prueba que lo detectó
    # fue la de "un miembro no puede emitir tokens", no la del comportamiento correcto.
    #
    # La ausencia hace que `require_scope` tenga que decidir por `kind` en vez de
    # delegar, y esa decisión es exactamente la que no conviene esconder tras un valor
    # que devuelve `True` en un caso y calcula el resultado en el otro.


@dataclass(frozen=True, slots=True)
class TokenPrincipal:
    """Sujeto de una credencial de servicio: un sistema con permisos explícitos.

    No tiene `user` ni `membership`, y esa ausencia es intencionada: un escaneo lanzado
    por un token no tiene un autor humano, y un endpoint que necesite saber quién es
    alguien no puede usar este sujeto. Cumplimentarlo con un usuario sintético
    acabaría guardando un `actor_user_id` que no corresponde con nadie, que es peor que
    un error claro al desarrollarlo.
    """

    organization: Organization
    token: ApiToken

    @property
    def kind(self) -> PrincipalKind:
        return PrincipalKind.TOKEN

    @property
    def role(self) -> RoleEnum | None:
        """Siempre `None`. Un token no es un miembro de la organización."""

        return None

    @property
    def is_admin(self) -> bool:
        return False

    @property
    def scopes(self) -> frozenset[Scope]:
        from backend.apps.api_access.service import scopes_of

        return frozenset(scopes_of(self.token))

    def has_scope(self, required: Scope) -> bool:
        return required in self.scopes


#: El sujeto autenticado, sea quien sea. Los endpoints que aceptan ambos usan esta
#: anotación y consultan `kind` para saber con cuál de los dos están.
Principal = UserPrincipal | TokenPrincipal

#: Sujeto con la garantía de que el tenant está resuelto. Es lo que se inyecta en los
#: endpoints: `organization` nunca falta, y por eso el filtro por `organization_id` de
#: R3 se puede escribir sin comprobar nada antes.
TenantPrincipal = UserPrincipal | TokenPrincipal


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def is_api_token(
    credentials: HTTPAuthorizationCredentials | None,
) -> TypeGuard[HTTPAuthorizationCredentials]:
    """Distingue un token de API de un JWT de sesión.

    Se decide por el prefijo declarado, no por intentar descifrarlo y mirar el error. Un
    JWT empieza por `eyJ`; un token de API por `mgf_live_`. Probar ambos y quedarse con
    el que funcione haría que "este bearer no es válido" tuviera un texto distinto según
    el orden de los intentos, y ese texto es lo primero que ve quien depura un `401`.
    """

    if credentials is None or credentials.scheme.lower() != "bearer":
        return False
    return credentials.credentials.startswith(API_TOKEN_PREFIX)


async def resolve_api_token(
    session: AsyncSession,
    credentials: HTTPAuthorizationCredentials,
) -> TokenPrincipal:
    """Valida un token de API y devuelve su sujeto.

    ## Por qué todos los fallos son el mismo 401

    Se busca por hash primero y se comprueban revocación y caducidad después, pero las
    tres salidas responden igual. Distinguir "no existe" de "existe y está revocado"
    convierte la respuesta en un oráculo que permite probar credenciales ajenas. Para el
    usuario la acción es la misma en los dos casos: crear otro token.

    ## Por qué se comprueba el estado del tenant

    Un token de un tenant dado de baja deja de servir aunque nadie lo revoque. Esa
    comprobación es la diferencia entre revocar doscientos tokens a mano y no tener que
    hacerlo, y no puede quedar en el `DELETE` de la organización porque un token puede
    seguir existiendo y válido en la base mucho después.
    """

    raw_token = credentials.credentials
    resultado = await session.execute(
        select(ApiToken).where(ApiToken.token_hash == hash_api_token(raw_token))
    )
    token = resultado.scalar_one_or_none()
    if token is None:
        raise _unauthorized("Token de API inválido")

    now = datetime.now(UTC)
    if token.revoked_at is not None:
        raise _unauthorized("Token de API revocado")
    if token.is_expired(now):
        raise _unauthorized("Token de API caducado")

    organizacion = (
        await session.execute(
            select(Organization).where(Organization.id == token.organization_id)
        )
    ).scalar_one_or_none()
    if organizacion is None:
        # No debería ocurrir: la clave foránea lo impide. Si ocurre, el token apunta a un
        # tenant que ya no existe, y desde el punto de vista del cliente su credencial no
        # sirve, que es cierto, así que es un 401 y no un 500.
        logger.error(
            "Token de API %s apunta a una organización inexistente: %s",
            token.id,
            token.organization_id,
        )
        raise _unauthorized("Token de API sin organización asociada")

    if organizacion.deleted_at is not None or not organizacion.is_active:
        raise _unauthorized("Token de API de una organización dada de baja")

    await _register_use(session, token, now)
    return TokenPrincipal(organization=organizacion, token=token)


async def _register_use(session: AsyncSession, token: ApiToken, now: datetime) -> None:
    """Anota el uso del token respetando el umbral de escritura.

    Se usa una sentencia `UPDATE` suelta y no el ORM a propósito: tocar el objeto y hacer
    `commit` traería la fila entera y su versión a la sesión de cada petición, y un
    `UPDATE ... WHERE` no devuelve nada que haya que sincronizar. De paso no deja el
    objeto en estado sucio para el código que viene detrás.
    """

    umbral = now - LAST_USED_WRITE_THROTTLE
    if token.last_used_at is not None and token.last_used_at > umbral:
        return
    await session.execute(
        update(ApiToken).where(ApiToken.id == token.id).values(last_used_at=now)
    )
    await session.commit()
    token.last_used_at = now


def organization_id_from_request(request: Request) -> UUID | None:
    """Lee `X-Organization-Id` si viene, sin fallar si falta o es invalida.

    Devolver `None` y no lanzar deja que cada ruta decida: la emisión de tokens
    funciona sin tenant explicito, y las que lo necesitan comprueban el resultado.
    Un error de formato se trata igual que una ausencia, porque desde el punto de
    vista del servidor son el mismo caso: no hay tenant que usar.
    """

    bruto = request.headers.get("X-Organization-Id")
    if bruto is None:
        return None
    try:
        return UUID(bruto)
    except (TypeError, ValueError):
        return None


async def resolve_principal(
    request: Request,
    credentials: CredentialsDependency,
    session: SessionDependency,
) -> TenantPrincipal:
    """Resuelve el sujeto de la petición, sea token de API o sesión web.

    ## Por qué un token ignora la cabecera de organización

    El tenant de un token es el de su fila. Aceptar `X-Organization-Id` abriría la puerta
    a que un token actuara sobre otro tenant cambiando una cabecera, y el aislamiento R3
    no admite un interruptor. Cuando la cabecera viene, se ignora.

    ## Por qué una sesión web sin cabecera sí funciona

    La cabecera es obligatoria para los recursos del panel, que siempre tienen tenant. El
    caso de uso real es una integración que usa la sesión en vez de un token, y
    rechazar con `403` obligaría a esa integración a montar un token para nada.
    """

    if is_api_token(credentials):
        return await resolve_api_token(session, credentials)

    user = await _resolve_authenticated_user(credentials, session)
    organization_id = organization_id_from_request(request)
    if organization_id is None:
        return await _resolve_principal_without_tenant(user, session)
    contexto = await resolve_tenant_for_user(session, user, organization_id)
    if contexto is None:
        raise _forbidden("Acceso a la organización denegado")
    return UserPrincipal(
        organization=contexto.organization,
        user=contexto.user,
        membership=contexto.membership,
    )


async def _resolve_principal_without_tenant(
    user: User,
    session: AsyncSession,
) -> UserPrincipal:
    """Sujeto de una sesión web a la que no se le exige tenant concreto.

    Se usa solo en la emisión de tokens: crear un token es un acto de cuenta, no de
    recurso, y la respuesta no filtra datos de ningún tenant. Cuando el usuario
    pertenece a varios, se toma el más antiguo para que dos emisiones seguidas produzcan
    el mismo tenant y el cliente no tenga que adivinar en cuál se creó el token.

    Si el usuario no tiene ninguna membresía activa no hay tenant al que colgar el token
    y se responde `403`: emitir un token que no puede servir sería devolver un secreto
    que ya es inservible, que es la peor forma de responder.
    """

    result = await session.execute(
        select(Organization, Membership)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(
            Membership.user_id == user.id,
            Membership.is_active.is_(True),
            Organization.is_active.is_(True),
            Organization.deleted_at.is_(None),
        )
        .order_by(Organization.created_at.asc())
        .limit(1)
    )
    fila = result.first()
    if fila is None:
        raise _forbidden("El usuario no pertenece a ninguna organización activa")
    organization, membership = fila
    return UserPrincipal(
        organization=organization,
        user=user,
        membership=membership,
    )


def require_scope(
    required: Scope,
    *,
    allow_admin_user: bool = True,
) -> Callable[..., object]:
    """Construye una dependencia que exige `required` y resuelve el tenant.

    ## La asimetría, en una línea

    Un token tiene que traer el scope. Un usuario web solo tiene que ser quien es, si
    `allow_admin_user` está activo y su rol es `ADMIN`. Por eso el argumento existe: hay
    operaciones donde "administrador del tenant" no debería bastar, y decidir eso en cada
    endpoint en vez de aquí es donde las dos reglas empezarían a divergir.

    ## Por qué el 403 es siempre el mismo texto

    Un token sin el scope y un token con el scope equivocado no se distinguen, y el
    detalle no dice cuál faltaba. Un `403` que lista los scopes que el token sí tiene
    ayudaría al cliente, pero también le diría a alguien con un token robado qué
    permisos tiene su víctima, que es información que ya no debe tener.
    """

    async def dependency(
        request: Request,
        credentials: CredentialsDependency,
        session: SessionDependency,
    ) -> TenantPrincipal:
        principal = await resolve_principal(request, credentials, session)

        # El orden importa y no es un detalle: se decide por clase de sujeto, y el rol
        # solo se mira en el caso de una sesión web. Un token nunca se valida por rol,
        # y un usuario nunca se valida por scopes.
        if isinstance(principal, TokenPrincipal):
            if not principal.has_scope(required):
                raise _forbidden(INSUFFICIENT_SCOPE_DETAIL)
            return principal

        if allow_admin_user and principal.is_admin:
            return principal
        # El detalle habla de rol y no de scopes porque el que falla es el rol. Decir
        # "Insufficient token permissions" a alguien que ha iniciado sesión en su
        # navegador lo lleva a buscar un token que no tiene y no existe para su caso.
        raise _forbidden("Se requiere permiso de administrador")

    dependency.__name__ = f"require_{required.value.replace(':', '_')}"
    return dependency


#: Anotaciones listas para usar en los routers. Se declaran aquí para que un endpoint no
#: tenga que importar las piezas sueltas y para que el nombre de la variable en el router
#: documente el scope exigido.
Credentials = CredentialsDependency
Session = SessionDependency
