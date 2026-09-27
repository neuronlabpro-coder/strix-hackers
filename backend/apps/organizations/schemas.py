"""Esquemas Pydantic estrictos para autenticación y organizaciones."""

import unicodedata
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from backend.apps.organizations.models import PlanTierEnum, RoleEnum
from backend.core.config import settings


class StrictSchema(BaseModel):
    """Base común para rechazar campos no declarados."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterRequest(StrictSchema):
    email: EmailStr
    password: str = Field(
        min_length=settings.password_min_length, max_length=settings.password_max_length
    )
    full_name: str = Field(min_length=1, max_length=255)
    organization_name: str | None = Field(default=None, min_length=1, max_length=128)


class LoginRequest(StrictSchema):
    email: EmailStr
    password: str = Field(min_length=1, max_length=settings.password_max_length)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str
    is_superuser: bool = False


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    plan_tier: PlanTierEnum
    credit_balance: Decimal
    role: RoleEnum
    created_at: datetime
    updated_at: datetime


class RegisterResponse(BaseModel):
    user: UserResponse
    organization: OrganizationResponse
    verification_required: bool
    verification_token: str | None = None


class OrganizationDeletionResponse(BaseModel):
    """Resultado de una baja lógica.

    Expone los contadores de lo revocado para que el panel pueda decir qué ha pasado
    en lugar de un "operación completada" genérico. Un usuario que da de baja un
    workspace con tres compañeros tiene que ver que a los tres se les ha revocado el
    acceso, porque si no no habra manera de recuperarlo.
    """

    organization_id: UUID
    deleted_at: datetime
    revoked_memberships: int = Field(ge=0)
    cancelled_subscriptions: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)

    @property
    def revoked_access(self) -> int:
        return self.revoked_memberships


class EmailVerificationRequest(StrictSchema):
    token: str = Field(min_length=20, max_length=256)


class EmailVerificationResponse(BaseModel):
    verified: bool


class EmailResendRequest(StrictSchema):
    email: EmailStr


class EmailResendResponse(BaseModel):
    accepted: bool
    verification_token: str | None = None


class OrganizationCreate(StrictSchema):
    name: str = Field(min_length=1, max_length=128)
    slug: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.lower()
        if (
            not normalized.replace("-", "").isalnum()
            or normalized.startswith("-")
            or normalized.endswith("-")
        ):
            raise ValueError("El slug solo puede contener letras, números y guiones")
        return normalized


class OrganizationUpdate(StrictSchema):
    """Renombrar el workspace. Solo el nombre: el `slug` no se toca.

    ## Por qué el `slug` no es editable

    El `slug` es la dirección pública del workspace y aparece en rutas, en correos y —
    antes de que exista el dominio propio— en el botón de compartir. Renombrarlo partiría
    los enlaces ya compartidos, y un enlace de invitación que no abre es un cliente que no
    entra. El nombre cambia; la dirección no. Por eso no hay campo `slug` aquí, y no como
    "todavía no": `extra="forbid"` hace que mandarlo sea un `422` en vez de ignorarlo en
    silencio, que es lo que dejaría al usuario creyendo que lo cambió.
    """

    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Colapsa espacios y descarta los caracteres de control que quedan.

        ## Por qué se colapsa en vez de rechazar

        Un nombre pegado desde un correo llega con tabuladores y saltos de línea dentro. El
        `str_strip_whitespace=True` de la base y el `min_length=1` de Pydantic miran la
        cadena tal cual llega, así que un nombre pegado con tabuladores no pasa por la vía
        de error sino por la de éxito —con los tabuladores dentro— y se guarda roto.

        `" ".join(value.split())` convierte cualquier secuencia de whitespace en un único
        espacio. Es lo que el usuario quería escribir y es lo que se guarda. La alternativa,
        rechazarlo, obligaría a limpiar el nombre a mano y devolvería un error por algo que
        el usuario no hizo mal: lo pegó.

        El `min_length=1` mira la cadena **antes** de colapsar, así que `"   "` lo pasa. Por
        eso el vacío se comprueba **después**, sobre la forma ya normalizada. Sin esa segunda
        comprobación, un workspace con nombre de tres espacios se guardaría y se pintaría
        como una cabecera vacía.

        ## Por qué los caracteres de control se rechazan y no se colapsan

        El colapso de `split()` se come todo lo que `str.isspace()` considera espacio —
        tabuladores, saltos de línea, espacios Unicode—, y eso es lo correcto. Lo que queda
        fuera de `isspace()` y sigue en la categoría `Cc` es el `NUL` y sus primos, que no
        tienen interpretación visual pero **truncan la cadena** en cualquier herramienta que
        la trate como C: SQLite, `printf`, una cabecera HTTP, un PDF.

        Un `NUL` en un nombre se ve perfecto en la interfaz y llega cortado en el correo de
        invitación. Se rechaza en vez de normalizarse porque no hay forma de "arreglarlo":
        quitarlo cambiaría el nombre que el usuario escribió, y aceptarlo lo rompería en
        silencio aguas abajo.

        Todo lo demás se acepta, incluidas tildes, `ñ` y emojis, que es lo que un cliente
        real pone de nombre en su empresa.
        """

        limpio = " ".join(value.split())
        if not limpio:
            raise ValueError("El nombre no puede estar vacío")
        if any(unicodedata.category(c) == "Cc" for c in limpio):
            raise ValueError("El nombre no puede contener caracteres de control")
        return limpio


class MemberItem(BaseModel):
    """Un miembro del workspace.

    ## Por qué `joined_at` y no `created_at`

    `Membership.created_at` es el momento en que se creó la fila, y hay dos casos en los que
    no es el momento en que la persona se unió: una invitación aceptada crea la membresía
    al aceptarse, pero una revocación y un reingreso crean una fila nueva con fecha nueva.
    Para "quiénes son y desde cuándo están en mi equipo" —que es lo que se pregunta al leer
    la tabla— la fecha de la fila activa es exactamente la respuesta correcta, y buscar un
    "alta original" en otra tabla solo añadiría una fuente de verdad más que mantener.

    `is_active` viaja aunque el listado solo devuelva activos. Un cliente que construya la
    lista con esta respuesta y la reutilice en otro sitio tiene el dato, y no tiene que
    inferir "si aparece, está activo" de la presencia en la lista.
    """

    user_id: UUID
    email: EmailStr
    full_name: str
    role: RoleEnum
    joined_at: datetime
    is_active: bool


class MemberListResponse(BaseModel):
    """Miembros del workspace activo."""

    items: list[MemberItem]
    total: int = Field(ge=0)


class MemberRoleUpdate(StrictSchema):
    """Cambio de rol de un miembro.

    Solo dos valores porque solo hay dos roles. Un `PATCH` con el rol **igual** al que ya
    tiene devuelve el estado sin escribir asiento: el servicio lo detecta y sale antes de
    tocar nada, para que el rastro no se llene de cambios que no fueron cambios.
    """

    role: RoleEnum


class InvitationCreate(StrictSchema):
    email: EmailStr
    role: RoleEnum = RoleEnum.MEMBER


class InvitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    role: RoleEnum
    expires_at: datetime
    accepted: bool
    invitation_token: str | None = None


class InvitationAcceptRequest(StrictSchema):
    token: str = Field(min_length=20, max_length=256)


class InvitationAcceptResponse(BaseModel):
    organization_id: UUID
    role: RoleEnum
    accepted: bool
