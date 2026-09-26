"""Modelo de credenciales de la API pública.

Un token de API es una contraseña de servicio: un secreto largo que un sistema, no una
persona, presenta en cada petición. Esa es la razón de que aquí se guarde un hash y no
el token, y la razón de que el secreto solo exista dos veces: en la respuesta `201` que
lo entrega y en la variable del proceso que lo generó.

## Por qué `revoked_at` y no una fila borrada

Revocar tiene que poder **demostrarse**. Si al revocar se eliminara la fila, no habría
forma de responder meses después a "¿este token estuvo activo el día del incidente?".
La fila se queda, marcada, y el rastro de que existió sobrevive a su baja igual que el
ledger de créditos de R4.

## Por qué no hereda de `TimestampMixin`

El mixin añade `updated_at` con `onupdate=func.now()`. En esta tabla un `updated_at`
que se mueve al revocar sugiere que la fila tiene estado mutable que alguien está
editando, cuando lo único que se le hace es marcarla como revocada. `created_at` se
declara aquí, y `revoked_at` es la única marca que puede cambiar después.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base

#: Prefijo del secreto. Identifica en claro que es una credencial de esta plataforma,
#: para que un secret encontrado por un escáner de filtraciones se reconozca al instante
#: y no haya que adivinar de qué servicio es.
API_TOKEN_PREFIX: Final[str] = "mgf_live_"  # noqa: S105 - prefijo público, no un secreto

#: Bytes de entropía del secreto, no de su representación hexadecimal. 32 bytes son 256
#: bits; subirlo a 64 doblaría el tamaño de la fila sin cambiar nada para nadie que
#: que intente attaquear el hash, porque a 256 bits ya no existe ese atacante.
TOKEN_SECRET_BYTES: Final[int] = 32

#: Caracteres visibles del prefijo. Cuatro hexadecimales son 16 bits: suficiente para que
#: un humano distinga dos tokens en una lista, insuficiente para que nadie pueda
#: reemplazar un token real por uno elegido.
TOKEN_PREFIX_VISIBLE: Final[int] = 4

#: SHA-256 en hexadecimal son exactamente 64 caracteres. Declarar la longitud en el
#: esquema hace que una versión que guardara otra cosa quede rechazada por la base, en
#: vez de pasar desapercibida hasta el momento de buscar.
TOKEN_HASH_LENGTH: Final[int] = 64


def token_prefix_for(raw_token: str) -> str:
    """Extrae la etiqueta visible de un token en claro.

    No se usa para buscar: cuatro caracteres visibles colisionan con facilidad entre
    miles de tokens, así que la búsqueda es siempre por hash.
    """

    return raw_token[: len(API_TOKEN_PREFIX) + TOKEN_PREFIX_VISIBLE]


class ApiToken(Base):
    """Credencial de la API pública de un tenant."""

    __tablename__ = "api_tokens"
    __table_args__ = (
        # Único y a la vez índice de búsqueda. Cada petición autenticada busca por hash,
        # así que el índice no es una optimización: es el camino normal. Y único, porque
        # una colisión de SHA-256 tiene que ser un error de inserción y no dos filas
        # donde revocar una credencial afecta a la otra.
        Index("uq_api_tokens_token_hash", "token_hash", unique=True),
        # La lista de tokens del panel filtra por organización y ordena por creación.
        Index("ix_api_tokens_org_created", "organization_id", "created_at"),
        # `revoked_at` entra en la condición de los tokens vigentes. Sin este índice
        # compuesto, cada petición autenticada recorre los revocados del tenant, que es
        # una lista que solo crece.
        Index("ix_api_tokens_org_revoked", "organization_id", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # RESTRICT y no CASCADE: un tenant con credenciales conserva su fila, igual que
        # con su ledger. `CASCADE` las borraría en silencio y dejaría al cliente sin
        # acceso sin ningún asiento que lo explicara.
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: Etiqueta para el usuario. No es identificador: dos tokens pueden llamarse igual y
    #: el panel los distingue por `token_prefix`.
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: JSONB y no `JSON`: es binario, no deduplica claves y admite un índice GIN el día
    #: que exista una consulta del tipo "qué tokens tienen el scope X". La lista se
    #: escribe y se lee enteras, nunca se filtra por elemento, así que un ARRAY de texto
    #: no aportaría nada y sí impediría esa consulta.
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    #: Lo escribe la autenticación al validar el token, y solo entonces. Sin `default` a
    #: propósito: un `now()` de servidor llenaría la columna al insertar y el panel
    #: mostraría un token recién creado como "usado hace 0 s", una mentira que impide
    #: detectar un token olvidado durante meses.
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def is_expired(self, now: datetime) -> bool:
        """`True` si la vigencia ya pasó en el instante dado.

        La comparación es estricta: un token con `expires_at` exactamente igual a `now`
        ya no sirve. Aceptarlo durante el instante en que ambos valores coinciden no
        aporta nada y hace menos claro el test de caducidad.
        """

        return self.expires_at is not None and self.expires_at <= now

    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_usable(self, now: datetime) -> bool:
        """`True` si el token sirve ahora mismo: no revocado y no caducado."""

        return not self.is_revoked() and not self.is_expired(now)
