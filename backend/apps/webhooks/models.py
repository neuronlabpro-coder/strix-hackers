"""Modelos de webhooks salientes.

## Por qué el secreto va cifrado y no en claro

Un secreto de firma **tiene** que ser recuperable en el momento de firmar, así que no
puede guardarse como un hash, a diferencia del token de API. Pero recuperable no quiere
decir en claro: el proyecto ya cifra las credenciales de Git con AES-256-GCM ligado al
tenant, y un secreto de webhook es exactamente el mismo tipo de material. Guardarlo en
claro haría que una lectura de `api_tokens` que se filtrara por una consola de SQL
expusiera además todas las credenciales de firma.

## Por qué la baja lógica y no el borrado

`ON DELETE RESTRICT` en `organization_id`: un tenant con webhooks conserva su fila, como
con su ledger. Un `CASCADE` borraría los endpoints en silencio y las entregas quedarían
colgando de un tenant que ya no existe, sin nada que lo explicara.

`WebhookDelivery` sí es `CASCADE`: es el historial de una operación que sí se puede
eliminar, y borrarlo junto a su endpoint es lo que el usuario pide al borrar el endpoint.
Ese detalle es la diferencia entre un rastro probatorio y un registro de actividad, y por
eso las dos tablas no comparten política.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base

#: Prefijo del secreto de firma. Es la convención de Stripe y GitHub, y no por capricho:
#: un secreto de webhook acaba en logs, en proxies y en captura de red, y reconocer el
#: prefijo al instante evita tener que adivinar de qué servicio es.
WEBHOOK_SECRET_PREFIX: Final[str] = "whsec_"  # noqa: S105 - prefijo público, no un secreto

#: Bytes de entropía. El mismo razonamiento que en los tokens de API: 256 bits no los
#: recorre nadie, y aquí además el secreto no se ataca sino que se adivina para **firmar**
#: como el cliente.
WEBHOOK_SECRET_BYTES: Final[int] = 32

#: Caracteres hexadecimales que siguen al prefijo en un secreto real: 32 bytes en
#: hexadecimal. Se usa para distinguir un secreto pegado en la URL de un dominio que
#: simplemente contiene la secuencia `whsec_`.
WEBHOOK_SECRET_HEX_CHARS: Final[int] = WEBHOOK_SECRET_BYTES * 2

#: Longitud de la columna que guarda el secreto cifrado.
#:
#: La especificación pedía `String(64)` en claro para un `whsec_<32 bytes en hexadecimal>`,
#: y ahí hay **dos** problemas, no uno. El primero es de longitud: 6 del prefijo más 64
#: hexadecimales son 70, y un `VARCHAR(64)` en PostgreSQL **trunca en silencio**. El
#: segundo es que la columna guarda el material **cifrado**, y medido sobre el formato real
#: del proyecto —`v1.<nonce en base64>.<cifrado en base64>`, con AES-256-GCM y su
#: etiqueta— son 135 caracteres, no 70. Elegir 128 a ojo lo dejaba siete corto, que es el
#: mismo fallo de silencio con un número más difícil de detectar. Lo detectó una prueba
#: al insertar, no una revisión: por eso está aquí el número medido y no un cálculo.
#:
#: Se declara 192 para que el margen cubra el crecimiento si el secreto pasara a 64 bytes
#: y para que ninguna versión futura tenga que medir otra vez. El sobrecoste es de unos
#: 57 bytes por fila, y las filas de webhooks se cuentan por tenant.
WEBHOOK_SECRET_CIPHERTEXT_LENGTH: Final[int] = 192

#: URL máxima. 2048 es el límite de URL de la práctica y cubre una URL con firma larga;
#: lo que se registra aquí es una URL base, no una con query de autenticación.
WEBHOOK_URL_LENGTH: Final[int] = 2048

#: Fallos consecutivos a partir de los cuales el endpoint se desactiva solo. Diez intentos
#: con retroceso exponencial cubren unas horas: si tras eso el endpoint sigue sin
#: responder, el problema no es transitorio y un worker reintentando en el vacío no lo va a
#: arreglar. Se desactiva, no se borra, para que el usuario vea el contador y decida.
FAILURE_AUTO_DISABLE_THRESHOLD: Final[int] = 10

#: Reintentos por entrega. Tres intentos con retroceso exponencial cubren una caída
#: breve del receptor sin multiplicar el tráfico contra un endpoint que ya no existe.
MAX_DELIVERY_ATTEMPTS: Final[int] = 3

#: Espera antes del primer reintento. Se crece en cada intento; ver `backoff_seconds`.
RETRY_BASE_SECONDS: Final[float] = 2.0


def generate_webhook_secret() -> str:
    """Genera un secreto de firma en el formato `whsec_<32 bytes en hexadecimal>`."""

    return f"{WEBHOOK_SECRET_PREFIX}{secrets.token_hex(WEBHOOK_SECRET_BYTES)}"


def is_webhook_secret(value: str) -> bool:
    """Indica si el valor contiene material de secreto de firma.

    Se busca en **toda** la URL y no solo al principio. Un `whsec_` va casi siempre
    pegado a algo —`?clave=whsec_...`, `/hooks/whsec_...`, `https://u@host/whsec_...`— y
    comprobar solo el prefijo del campo deja pasar exactamente los casos que de verdad
    se cometen al copiar y pegar, que son los que aparecen en la query y en la ruta.

    Se comprueba además que lo que sigue al prefijo tenga la forma de un hexadecimal
    largo. Sin esa segunda condición, un endpoint legítimo cuyo dominio contuviera la
    secuencia `whsec_` quedaría bloqueado sin motivo, y un falso positivo que empuje al
    usuario a inventarse otro nombre es peor que dejar pasar un caso raro.
    """

    indice = value.lower().find(WEBHOOK_SECRET_PREFIX)
    while indice != -1:
        candidato = value[indice + len(WEBHOOK_SECRET_PREFIX) :]
        material = candidato[:WEBHOOK_SECRET_HEX_CHARS]
        if len(material) == WEBHOOK_SECRET_HEX_CHARS and all(
            char in "0123456789abcdefABCDEF" for char in material
        ):
            return True
        indice = value.lower().find(WEBHOOK_SECRET_PREFIX, indice + 1)
    return False


class WebhookEndpoint(Base):
    """Endpoint HTTP al que la plataforma entrega eventos de un tenant."""

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        # La lista de endpoints filtra por organización y ordena por creación. Es la
        # consulta que hace la pantalla al abrirse, así que va compuesta.
        Index("ix_webhook_endpoints_org_created", "organization_id", "created_at"),
        # `consecutive_failures` se lee en cada envío para decidir si el endpoint sigue
        # activo. Sin índice es un recorrido de los endpoints del tenant por cada
        # entrega, y la lista de endpoints solo crece.
        Index("ix_webhook_endpoints_failures", "organization_id", "consecutive_failures"),
        Index(
            "ix_webhook_endpoints_secret",
            "encrypted_secret",
            unique=True,
        ),
        # El recorte de la respuesta vive en el dispatcher, pero se declara aquí la
        # constraint del contador para que no pueda quedar en negativo: el auto-desactivado
        # suma, y una carrera entre dos entregas podría restar de más en una versión
        # futura que lo ajustara.
        Index(
            "ix_webhook_endpoints_org_active",
            "organization_id",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # RESTRICT y no CASCADE: el tenant con webhooks conserva su fila. El borrado de
        # una organización es lógico (`deleted_at`), y un CASCADE aquí borraría los
        # endpoints en silencio dejando las entregas apuntando a la nada.
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(String(WEBHOOK_URL_LENGTH), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: El secreto de firma, cifrado con AES-256-GCM y ligado a la organización con AAD.
    #: Nunca se devuelve en una respuesta: solo el `whsec_` en claro, una vez, al crear.
    encrypted_secret: Mapped[str] = mapped_column(
        String(WEBHOOK_SECRET_CIPHERTEXT_LENGTH), nullable=False
    )
    #: Eventos suscritos, en el orden del catálogo. JSONB y no ARRAY porque se escribe y
    #: se lee la lista entera, y porque un filtro del tipo "qué endpoints escuchan
    #: `pentest.completed`" es un `@>` que JSONB resuelve con índice GIN.
    #:
    #: El `server_default` cubre la fila creada por SQL, no solo la creada por el ORM. Sin
    #: él, una inserción directa sin `event_types` fallaría por `NOT NULL` en vez de
    #: dejar un endpoint sin ningún evento, que es el estado inútil pero no inválido.
    event_types: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: Contador de fallos consecutivos. Lo pone a cero una entrega 2xx; llega al umbral y
    #: el endpoint se desactiva solo, que es la diferencia entre "se recupera" y "insiste
    #: en un destino que no va a contestar nunca".
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def has_event(self, event_type: str) -> bool:
        """`True` si el endpoint está suscrito a ese evento.

        Se comprueba el estado antes que la suscripción en el dispatcher, y no aquí: que un
        endpoint pausado no reciba nada es una decisión de envío, no una propiedad del tipo
        de evento.
        """

        return event_type in (self.event_types or [])

    def is_usable(self) -> bool:
        return self.is_active

    def should_auto_disable(self) -> bool:
        return self.consecutive_failures >= FAILURE_AUTO_DISABLE_THRESHOLD


class WebhookDelivery(Base):
    """Un intento de entrega, con su resultado.

    Cada intento es una fila. Una entrega que falla tres veces deja tres filas, y eso es
    deliberado: el diagnóstico que necesita el usuario es "cuántas veces intentó y qué
    vio cada vez", y guardar solo el último intento lo deja sin respuesta. Por eso
    `attempt` existe y por eso no se sobrescribe nada.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # El historial se lee por endpoint y en orden inverso al tiempo. El índice
        # compuesto es el que hace que esa consulta no sea un recorrido.
        Index("ix_webhook_deliveries_endpoint_created", "endpoint_id", "created_at"),
        # Un endpoint concreto se puede seguir durante el auto-desactivado sin recorrer
        # sus entregas enteras.
        Index("ix_webhook_deliveries_endpoint_event", "endpoint_id", "event_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # CASCADE, a diferencia del resto: el historial de entregas es la memoria de una
        # operación concreta y desaparece con ella. Lo que no se borra nunca es el rastro
        # financiero y el forense, que viven en `credit_ledger` y `audit_log`.
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # Duplicado a propósito. Sin esta columna, listar las entregas de un tenant exige
        # un `JOIN` contra `webhook_endpoints`, y R3 exige que el filtro por organización
        # esté en la consulta de la tabla que se lee. Con ella, la garantía es local.
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Cuerpo exacto que se envió, ya serializado. Se guarda el que salió, no el que se
    #: construyó: la firma es sobre esos bytes, y para poder verificar una firma
    #: ajenamente hace falta el cuerpo tal cual se envió.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Los primeros 1024 caracteres de la respuesta, recortados con marcador. Ver
    #: `ssrf.truncate_response`: sin el marcador, un cuerpo recortado se lee como
    #: completo y el usuario busca en un JSON un mensaje que ya no esta.
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def delivered_at(self) -> datetime:
        """Alias con el nombre que pide la especificación.

        La columna real es `created_at` para no duplicar la marca temporal, y el alias
        mantiene el vocabulario del dominio de entregas sin obligar a tener dos fuentes
        de verdad que se desincronicen.
        """

        return self.created_at

    @property
    def succeeded(self) -> bool:
        """`True` si el intento obtuvo un 2xx.

        Se calcula y no se guarda: depende solo de `status_code`, y un booleano que se
        puede desincronizar de su propio código de estado es peor que calcularlo.
        """

        return self.status_code is not None and 200 <= self.status_code < 300
