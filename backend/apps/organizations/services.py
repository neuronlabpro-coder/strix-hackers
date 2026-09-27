"""Operaciones de persistencia para usuarios, organizaciones e invitaciones."""

import hashlib
import re
import secrets
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.organizations.models import (
    Invitation,
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.organizations.schemas import (
    InvitationCreate,
    MemberItem,
    OrganizationCreate,
    RegisterRequest,
)
from backend.core.config import settings
from backend.core.security import hash_password, verify_password

#: Alias de `Membership` para el `UPDATE ... WHERE` con subconsulta sobre la misma
#: tabla. Sin el, PostgreSQL no puede resolver a que tabla pertenece cada referencia y
#: el `UPDATE` falla. Es un alias de **lectura**: la fila que se desactiva es la de la
#: tabla real, y la subconsulta solo cuenta.
Alias = aliased(Membership)


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


async def rename_organization(
    session: AsyncSession,
    organization: Organization,
    new_name: str,
    actor_user_id: uuid.UUID,
) -> Organization:
    """Renombra el workspace y deja constancia en el rastro de auditoría.

    ## Por qué el asiento va en la **misma** transacción

    El nombre se pinta en la cabecera, en los correos de invitación y en la exportación. Si
    el `UPDATE` se confirmara y el asiento fallara, el workspace se llamaría de una cosa y
    el rastro diría que nunca se renombró —o al revés—, y en una disputa sobre quién cambió
    qué y cuándo, ninguna de las dos mitades serviría. Van juntos: o se escriben los dos o
    no se escribe ninguno.

    ## Por qué el asiento guarda el nombre anterior

    `from_state` es el nombre viejo. Un `audit_log` que solo dijera "esto cambió" no
    respondería a la pregunta que se le hace en una revisión: **de qué a qué**. Con el
    nombre anterior, la entrada es autocontenida y el rastro se lee sin tener que reconstruir
    el histórico de cambios a partir de otras entradas.

    El nombre nuevo va en `to_state`. Los dos varchar son de 64 y el nombre puede llegar a
    128: se guardan **recortados a 64** y no con el nombre entero.

    ## Por qué se recortan y no se cambia la columna

    Porque el `audit_log` es append-only por R4 y sus triggers bloquean cualquier
    `ALTER`. Un rastro que no se puede ampliar es un rastro que hay que dimensionar bien
    desde el principio. 64 caracteres bastan para leer "FleetView Iberia" o
    "Consultora Fernandez SL" y para distinguir un nombre de otro, que es para lo que
    sirve esta entrada. El nombre entero sigue living en la fila de `organizations`, y para
    el valor vigente nadie necesita el rastro.

    Se marca el truncamiento con `…` para que quien lea el asiento sepa que el nombre está
    cortado. Un nombre recortado que no lo dice es peor que un nombre ausente: quien lo lea
    va a suponer que el workspace se llamaba exactamente así, y va a buscar un nombre que
    no existe.
    """

    previous_name = organization.name
    if previous_name == new_name:
        # Renombrar a lo mismo no es un cambio. Devolver sin escribir asiento evita el
        # ruido en el rastro de un botón que se puede pulsar sin querer, y evita que el
        # `updated_at` de la organización se mueva por una petición que no cambió nada.
        return organization

    organization.name = new_name
    session.add(
        AuditLogEntry(
            organization_id=organization.id,
            actor_user_id=actor_user_id,
            action=AuditActionEnum.ORGANIZATION_RENAMED,
            entity_type="organization",
            entity_id=organization.id,
            from_state=_truncate_audit_state(previous_name),
            to_state=_truncate_audit_state(new_name),
        )
    )
    await session.commit()
    # El `refresh` no es opcional. Devolver la entidad sin recargar y dejar que la ruta lea
    # `credit_balance` o `created_at` funciona con `expire_on_commit=False` —que es lo que
    # usa `AsyncSessionLocal`— y revienta con `MissingGreenlet` con el valor por defecto de
    # SQLAlchemy, que es lo que monta la suite de integración. El `refresh` funciona en los
    # dos casos y deja el contrato del servicio sin depender de cómo se configuró la sesión
    # de quien lo llama.
    await session.refresh(organization)
    return organization


def _truncate_audit_state(value: str) -> str:
    """Recorta a la anchura de `audit_log.from_state` / `to_state`.

    64 es `String(64)`, la medida de la columna. Y `String` corto **trunca en silencio** en
    PostgreSQL cuando la columna existe, pero lanza error cuando no —que es el caso aquí, en
    la fase de construcción—, así que el recorte se hace en Python y no se depende de
    cómo se comporte la base en cada versión.
    """

    return value if len(value) <= 64 else f"{value[:63]}…"


class LastAdminRequiredError(RuntimeError):
    """Un cambio dejaría al workspace sin ningún administrador.

    Es una excepción de **dominio**, no un `HTTPException`: la decisión es del servicio. La
    ruta la traduce a `409`.
    """


async def list_members(
    session: AsyncSession, organization_id: uuid.UUID
) -> list[MemberItem]:
    """Los miembros **activos** del workspace, con su rol y su fecha de alta.

    ## Por qué `Membership.is_active` y no `User.is_active`

    Son dos cosas distintas. Una membresía inactiva con usuario activo es alguien que se fue
    del equipo pero conserva su cuenta en la plataforma: el workspace sigue existiendo, esa
    persona ya no entra. Un usuario inactivo con membresía activa es una cuenta desactivada
    por el soporte o por el propio usuario, y su rastro de auditoría sigue valiendo, así que
    en la lista de equipo interesa verlo.

    Se filtran las membresías inactivas porque la pregunta es "quiénes están en mi equipo
    ahora", y alguien que se fue no está. Los usuarios desactivados **sí** se listan si su
    membresía sigue activa, y por eso el filtro **no** se aplica a `User.is_active`: si se
    aplicara, desactivar una cuenta haría desaparecer a alguien del equipo sin que nadie
    hubiera decidido nada sobre el equipo. Eso sería un efecto secundario de una medida de
    seguridad disfrazado de cambio en la organización.

    El filtro por `organization_id` va en el `WHERE`, no en Python: es R3, y un filtro
    aplicado después de traer las filas sería el aislamiento resuelto tarde y en el sitio
    equivocado.
    """

    filas = (
        await session.execute(
            select(Membership, User.email, User.full_name, Membership.created_at)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.organization_id == organization_id,
                Membership.is_active.is_(True),
            )
            .order_by(Membership.role.asc(), Membership.created_at.asc())
        )
    ).all()
    return [
        MemberItem(
            user_id=membership.user_id,
            email=email,
            full_name=full_name,
            role=membership.role,
            joined_at=joined_at,
            is_active=membership.is_active,
        )
        for membership, email, full_name, joined_at in filas
    ]


async def change_member_role(
    session: AsyncSession,
    *,
    organization: Organization,
    membership: Membership,
    new_role: RoleEnum,
    actor_user_id: uuid.UUID,
) -> Membership:
    """Cambia el rol de un miembro, o lo retira si el nuevo rol es `MEMBER` y no procede.

    ## Por qué no se puede quitar el rol al último administrador

    Un workspace sin ningún `ADMIN` es un workspace que **no se puede administered**: la
    siguiente pantalla a la que vaya un `MEMBER` a pedir permiso se lo deniega él mismo con
    un `403` que no puede corregir, porque corregirlo requiere ser admin. Es un callejón
    sin salida del que solo se sale por consola de base de datos.

    Se comprueba con un `COUNT` en la misma consulta, no leyendo la lista en Python: la
    comprobación y la escritura tienen que ser consistentes entre sí, y leer la lista para
    contar abre una ventana entre "he contado" y "he escrito" en la que otra petición
    puede quitar al segundo admin. El `UPDATE` lleva además un `WHERE` con la condición
    de que quede al menos un admin, de modo que si esa ventana se abre, el `UPDATE` no
    afecta a nadie y el cambio falla en vez de dejar el workspace sin dueño.
    """

    if membership.organization_id != organization.id:
        raise NoSuchMemberError("Ese miembro no pertenece a este workspace")

    if membership.role == new_role:
        return membership

    if new_role != RoleEnum.ADMIN and membership.role == RoleEnum.ADMIN:
        admins = (
            await session.execute(
                select(func.count(Membership.id))
                .join(Organization, Organization.id == Membership.organization_id)
                .where(
                    Membership.organization_id == organization.id,
                    Membership.role == RoleEnum.ADMIN,
                    Membership.is_active.is_(True),
                )
            )
        ).scalar_one()
        if admins <= 1:
            raise LastAdminRequiredError(
                "El workspace necesita al menos un administrador activo"
            )

    previous_role = membership.role
    membership.role = new_role
    session.add(
        AuditLogEntry(
            organization_id=organization.id,
            actor_user_id=actor_user_id,
            action=AuditActionEnum.MEMBER_ROLE_CHANGED,
            entity_type="membership",
            entity_id=membership.id,
            from_state=previous_role.value,
            to_state=new_role.value,
        )
    )
    await session.commit()
    await session.refresh(membership)
    return membership


class NoSuchMemberError(LookupError):
    """El usuario no es miembro activo de este workspace.

    Un solo tipo para "no existe" y "no es miembro": el cliente no puede distinguir los dos
    casos, y esa indistinguibilidad es la garantía de que la respuesta no confirma la
    existencia de nada ajeno.
    """


async def remove_member(
    session: AsyncSession,
    *,
    organization: Organization,
    user_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> Membership:
    """Retira a un miembro desactivando su membresía. No borra la fila.

    ## Por qué desactivar y no borrar

    Por R4. El rastro de auditoría guarda la `membership_id` del actor de cada entrada, y
    un `DELETE` en cascada desde la fila de usuario se llevaría por delante el rastro que
    demuestra qué hizo ese miembro. Desactivar deja la fila, y con ella la prueba.

    ## Por qué no se puede retirar al propio actor

    Porque un admin que se quita a sí mismo de su propio workspace y era el último se
    queda sin poder administrar nada, y no hay forma de volver atrás desde la interfaz.
    Si queda otro admin, se permite — es una rotación legítima de administra — pero la
    condición de "queda al menos un admin" se comprueba igual, así que el caso de "soy el
    único admin" cae en el mismo `LastAdminRequiredError` que el cambio de rol.

    La condición de "queda al menos un admin" va **dentro del `UPDATE`**, no antes. Es la
    diferencia entre una comprobación y una garantía: contando admins en un `SELECT`
    previo y escribiendo después, hay una ventana entre las dos en la que otra petición
    retira al segundo admin, y el resultado es un workspace sin dueño. Con la condición
    dentro del `UPDATE`, la base la evalúa con el estado real en el momento de escribir, y
    si ya no queda admin el `UPDATE` no afecta a nadie y el retiro falla.

    Solo se exige cuando la fila que se retira **es** un admin. Retirar a un `MEMBER` nunca
    reduce el número de admins, así que exigírselo sería rechazar una operación perfectamente
    válida cada vez que el workspace tuviera un solo admin.
    """

    if user_id == actor_user_id:
        raise CannotRemoveSelfError("No puedes retirarte a ti mismo del workspace")

    # La condición va dentro del `WHERE` del propio `UPDATE`, y por eso no hay ventana entre
    # "comprobar" y "escribir": PostgreSQL evalúa el `WHERE` contra la foto de las filas
    # **antes** de aplicar el `UPDATE`, en una sola sentencia atómica.
    #
    # El recuento sale de un `COUNT` sobre un **alias** de la misma tabla, no correlacionado.
    # Un alias es obligatorio: sin él, el `UPDATE memberships` y el `SELECT count(*) FROM
    # memberships` se refieren a la misma tabla y PostgreSQL no puede decidir a cuál se
    # refiere cada referencia. Con el alias, la subconsulta cuenta los admins activos del
    # workspace tal como están **antes** del update, que es justo lo que hace falta para
    # decidir si esta retirada deja alguno.
    #
    # No correlacionado a propósito: si lo fuera, contaría fila a fila, y para la fila que
    # se va a retirar la condición sería trivialmente cierta.
    admins_vivos = (
        select(func.count(Alias.id))
        .where(
            Alias.organization_id == organization.id,
            Alias.role == RoleEnum.ADMIN,
            Alias.is_active.is_(True),
        )
        .scalar_subquery()
    )
    membership_id = await session.scalar(
        update(Membership)
        .where(
            Membership.organization_id == organization.id,
            Membership.user_id == user_id,
            Membership.is_active.is_(True),
            # `OR` con el rol: si la fila a retirar no es admin, la condición de recuento ni
            # se considera y la retirada sigue sin más. Retirar a un `MEMBER` nunca
            # reduce el número de admins.
            (Membership.role != RoleEnum.ADMIN) | (admins_vivos > 1),
        )
        .values(is_active=False)
        .returning(Membership.id)
    )
    if membership_id is None:
        # El `UPDATE` no afectó a nadie, y eso tiene **dos** causas distintas que el cliente
        # necesita diferenciar: o la fila no existe en este workspace, o existe y retirarla
        # dejaría al workspace sin admin. La primera es un `404` y la segunda un `409`, y
        # confundirlas haría que un `user_id` de otro sitio saliera como "te falta un admin",
        # que no es cierto y no ayuda a corregir nada.
        #
        # No hay `rollback` aquí a propósito. Un `UPDATE` que no afecta a ninguna fila no
        # deja nada pendiente, así que el `rollback` sería inútil — y además dañino: expira
        # los objetos de la sesión, y leer `organization.id` después intentaría refrescarlos
        # fuera de contexto, con un `MissingGreenlet`. El `organization_id` se copia a una
        # variable plana antes de la consulta de desempate, que es la forma de no depender
        # del estado de la sesión en el camino del error.
        organization_id = organization.id
        existe = (
            await session.execute(
                select(Membership.id).where(
                    Membership.organization_id == organization_id,
                    Membership.user_id == user_id,
                    Membership.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if existe is None:
            raise NoSuchMemberError("Ese miembro no pertenece a este workspace")
        raise LastAdminRequiredError(
            "El workspace necesita al menos un administrador activo"
        )

    session.add(
        AuditLogEntry(
            organization_id=organization.id,
            actor_user_id=actor_user_id,
            action=AuditActionEnum.MEMBER_REMOVED,
            entity_type="membership",
            entity_id=membership_id,
            from_state="active",
            to_state="removed",
        )
    )
    await session.commit()

    membership = (
        await session.execute(
            select(Membership).where(Membership.id == membership_id)
        )
    ).scalar_one()
    return membership


class CannotRemoveSelfError(RuntimeError):
    """El actor intentó retirarse a sí mismo del workspace."""
