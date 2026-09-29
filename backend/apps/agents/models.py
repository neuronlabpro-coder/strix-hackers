"""Agentes de escaneo que corren **dentro de la red del cliente**.

## Por qué existe un agente y por qué no se escanea desde el SaaS

El sandbox de Strix vive en un bridge aislado cuyo cerco de salida permite DNS, HTTPS y
conexiones establecidas: `scripts/harden_runner_egress.sh` descarta en silencio todo lo demás,
incluidos el 22, el 80 y el 8080. Ese cerco es correcto y no se toca —es lo que impide que un
agente comprometido saque la clave del proveedor por un puerto alto—, pero significa que
**desde el SaaS no se puede pentestar la red privada de nadie**.

Y esa es la mayor parte del valor: una base de datos en un contenedor y un segmento interno no
son accesibles desde Internet. Un motor de pentesting que solo sabe atacar objetivos públicos no
cobra por lo que el cliente quiere comprar.

La salida no es abrir el cerco. Es que **el escaneo ocurra donde la red es alcanzable**: un
agente que el cliente instala en su propia red, que ve lo que el cliente ve, y al que no se le
entrega ninguna credencial con la que pueda atacar la plataforma.

## El agente no lleva la clave del proveedor

Es lo más importante de este diseño, y es una consecuencia y no una decisión de estilo. Un
agente corre en la red del cliente, con los permisos del cliente, sobre infraestructura del
cliente: es el entorno **menos** confiable de todo el sistema para la plataforma. Poner ahí la
clave de inferencia significaría que comprometer la red de un cliente compromete la
facturación de **todos** los clientes, y que el radio de impacto de un archivo `README` fuese la
plataforma entera.

Así que el agente hace la parte **determinista** —descubrir, medir puertos, inventariar la
imagen— y publica **hechos**. El razonamiento ocurre en la plataforma. El agente no recibe
ninguna credencial de la plataforma con la que pueda hacer daño: solo un token que únicamente
puede pedir trabajo y devolver resultados.

## Por qué el token no tiene lista de permisos

Porque un permiso es una lista que hay que mantener, y una lista se puede ampliar sin que nadie
lo note. El token de un agente no tiene permisos: tiene exactamente dos acciones, los dos
endpoints de este router, y no hay una tercera que pueda hacer. Añadirle una sería **cambiar
el router**, que es un cambio visible en revisión. Un permiso que solo se concede editando un
fichero es un permiso que no se concede por descuido.

Y la autenticación es distinta de la de las personas a propósito: un token de agente **no** pasa
por `get_current_tenant` y **no** alcanza ningún endpoint de panel. Que el agente no pueda
suplantar a un usuario no es una propiedad que se comprueba endpoint a endpoint: es que
`get_current_agent` y `get_current_tenant` no comparten nada y solo se montan en routers
distintos.

## Por qué el trabajo se **tira** hacia el agente y no se empuja

Porque un agente es un proceso que el cliente enciende y apaga, detrás de un NAT y de un
cortafuegos que no controlamos. Un push exigiría que el cliente abriera un puerto entrante, que
es justo lo que un cliente no quiere abrir en su red de producción.

Tirar significa que solo hace falta **salida HTTPS** del agente hacia la plataforma, que es la
dirección que los cortafuegos ya permiten. La contrapartida es que hay que resolver qué pasa
cuando el agente muere a mitad de un trabajo, y de eso se ocupa el **alquiler** de `AgentJob`:
un trabajo reclamado tiene fecha de vencimiento, y vencido vuelve a la cola. Sin eso, un agente
que se apaga deja un trabajo en curso para siempre, que es un trabajo perdido y un hueco en la
postura del cliente que nadie sabría leer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base

#: Prefijo del token de un agente. Distinto del de la API pública a propósito: el prefijo va
#: en claro en el registro y en los logs del cliente, y quien mira un log tiene que poder
#: distinguir de un vistazo una credencial de panel de una de agente. Además permite que
#: `get_current_agent` decida **sin tocar la base** que un token no es suyo, que es la primera
#: barrera y la más barata.
#: El `noqa` es el mismo que lleva `API_TOKEN_PREFIX`: un prefijo público no es un secreto, y
#: `S105` no lo distingue de una contraseña porque solo mira el nombre de la constante.
AGENT_TOKEN_PREFIX: Final[str] = "mgf_agent_"  # noqa: S105 - prefijo público, no un secreto

#: Bytes de entropía del secreto, los mismos que en la API pública: 256 bits generados por
#: `secrets.token_hex`.
AGENT_TOKEN_SECRET_BYTES: Final[int] = 32

#: Caracteres del token que quedan visibles en la fila, para distinguir dos agentes sin ver
#: ningún secreto. Cuatro es lo que hace inservible enumerarlos.
AGENT_TOKEN_PREFIX_VISIBLE: Final[int] = 4


class AgentStatusEnum(StrEnum):
    """Estado de un agente registrado.

    `REVOKED` **no** borra la fila, y por eso el `agent_id` de un trabajo es `SET NULL` y no
    `CASCADE`: el trabajo y su evidencia se quedan para siempre aunque el agente que lo hizo se
    haya dado de baja. Un escaneo que nadie puede leer porque se revocó quien lo ejecutó es una
    prueba que desaparece, y la postura de un cliente no puede depender de si su técnico sigue
    en la nómina.
    """

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class AgentJobKindEnum(StrEnum):
    """Lo que se le pide al agente.

    `CONTAINER_SCAN` recibe una referencia de imagen y devuelve el inventario de su sistema
    operativo y sus paquetes. `NETWORK_SCAN` recibe un CIDR y devuelve los hosts vivos y sus
    puertos.

    No hay un `kind` de "haz un pentest": el razonamiento no ocurre en el agente, y por eso no
    hay ningún trabajo que le pida pensar.
    """

    CONTAINER_SCAN = "CONTAINER_SCAN"
    NETWORK_SCAN = "NETWORK_SCAN"


class AgentJobStatusEnum(StrEnum):
    """Ciclo de vida de un trabajo.

    `CLAIMED` y `RUNNING` se distinguen porque el instante en que el agente toma el trabajo y el
    instante en que lo termina no son lo mismo, y la diferencia separa "el agente está
    trabajando" de "el agente aceptó el trabajo y murió". Los dos son trabajo en curso para el
    cliente y son fallos distintos con arreglos distintos para el operador.
    """

    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ScannerAgent(Base):
    """Un agente de escaneo instalado por un cliente, con su credencial propia."""

    __tablename__ = "scanner_agents"
    __table_args__ = (
        # Único y a la vez índice de búsqueda: cada petición del agente busca por hash, así que
        # este índice no es una optimización, es el camino normal. Y único, porque una
        # colisión de SHA-256 tiene que ser un error de inserción y no dos filas con el mismo
        # secreto.
        #
        # Es `UniqueConstraint` y no `Index(unique=True)` porque es lo que creó la migración y
        # las dos cosas **no** son la misma para el comparador de esquema: `alembic check` las
        # ve como "borra una restricción y añade un índice" y se pone en rojo. Como la
        # migración ya está aplicada, la que se ajusta es la declaración del modelo.
        UniqueConstraint("token_hash", name="uq_scanner_agents_token_hash"),
        # La lista de agentes del panel filtra por organización y ordena por alta.
        Index("ix_scanner_agents_org_enrolled", "organization_id", "enrolled_at"),
        # `revoked_at` entra en la condición de los agentes vigentes. Sin este índice
        # compuesto, cada autenticación recorre los revocados del tenant, que es una lista
        # que solo crece.
        Index("ix_scanner_agents_org_revoked", "organization_id", "revoked_at"),
        # Dos agentes con el mismo nombre en la misma organización son el mismo agente
        # instalándose dos veces, y ese error tiene que verlo quien lo cometió al listar, no
        # un `IntegrityError` en la consola.
        UniqueConstraint("organization_id", "name", name="uq_scanner_agents_org_name"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_scanner_agents_name_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    #: **Nunca** el token. Esta es la línea de la que depende todo el resto del módulo.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Cuatro caracteres visibles, para distinguir dos agentes en la lista. No se busca por
    #: este: cuatro caracteres visibles colisionarían entre miles de agentes.
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)

    status: Mapped[AgentStatusEnum] = mapped_column(
        SQLEnum(AgentStatusEnum, name="agent_status_enum"),
        nullable=False,
        default=AgentStatusEnum.ACTIVE,
        server_default=AgentStatusEnum.ACTIVE.name,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Lo que el agente declaró al darse de alta. No autoriza nada: es contexto de soporte.
    #: Un agente que dice `linux/amd64` y funciona en `linux/arm64` no miente en nada que
    #: importe, y un filtro que lo comprueba solo genera discusiones. Se guarda porque sin él,
    #: un fallo del tipo "el agente no devuelve nada" no tiene ninguna pista con la que empezar.
    agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    platform_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Última vez que el agente se identificó con éxito. Es lo que convierte "tengo un agente
    #: dado de alta" en "tengo un agente vivo", que son preguntas distintas, y la segunda es la
    #: que un cliente hace cuando un escaneo no llega.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentJob(Base):
    """Un encargo de escaneo que un agente ejecuta y la plataforma archiva.

    ## Por qué el resultado se guarda con una huella y no solo con el JSON

    Porque el resultado **es la prueba**. Un escaneo de red dice qué puertos había abiertos
    ayer; dentro de seis meses, cuando se discuta si un servidor estaba expuesto, lo que vale
    es poder demostrar que el registro no se tocó después. Un `result` que se puede reescribir
    con una llamada a la API no es evidencia de nada, y por eso:

    - `result_digest` es el SHA-256 de la forma canónica del JSON, y se comprueba al leer.
    - Un `trigger` de PostgreSQL rechaza el `UPDATE` de un trabajo ya terminal. La protección
      está en la base y no en el código, porque el código se puede evitar escribiendo SQL.

    Y la huella no es una firma criptográfica: no protege contra un atacante que pueda escribir
    en la base. Protege contra el caso real, que es una corrección posterior —un cliente que
    "en realidad no era así"— hecha por la propia plataforma con su propia API.

    ## Por qué `agent_id` es `SET NULL` y no `CASCADE`

    Por lo que dice `AgentStatusEnum.revoked`: revocar un agente no puede borrar la evidencia
    de lo que ya escaneó. Con `CASCADE`, dar de baja a un agente que falló en un escaneo
    borraría el registro de ese escaneo, y con él el hueco en la postura del cliente.
    """

    __tablename__ = "agent_jobs"
    __table_args__ = (
        Index("ix_agent_jobs_org_status", "organization_id", "status"),
        # La cola se lee por estado y se ordena por prioridad, siempre dentro de una
        # organización. Este índice **no** lleva `organization_id` a propósito: el agente no
        # elige su organización, la recibe de la sesión, y un índice que la incluyera
        # invitaría a escribir un `WHERE` sin ella y a que pareciera correcto.
        Index("ix_agent_jobs_queue", "status", "priority_order", "created_at"),
        # El reaper busca los trabajos cuyo alquiler venció. Es la única consulta que mira
        # `lease_expires_at`, y por eso tiene índice propio.
        Index("ix_agent_jobs_lease", "status", "lease_expires_at"),
        # Cada agente pide "lo siguiente que me toca" con un índice por agente y estado, que
        # es la consulta más repetida del sistema: la hace cada agente cada pocos segundos.
        Index("ix_agent_jobs_agent_status", "agent_id", "status"),
        CheckConstraint("priority_order >= 1", name="ck_agent_jobs_priority_positive"),
        CheckConstraint("attempt_count >= 0", name="ck_agent_jobs_attempts_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Nulo cuando el agente que lo ejecutó se dio de baja. Ver el docstring de la clase.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scanner_agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Quién lo pidió. Nulo si lo pidió una integración, que es lo normal cuando el escaneo
    #: viene de un webhook o de una revisión de PR. Se guarda para que la pantalla pueda decir
    #: "lo pediste tú" y para que un cliente distinga sus escaneos de los que alguien programó.
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    kind: Mapped[AgentJobKindEnum] = mapped_column(
        SQLEnum(AgentJobKindEnum, name="agent_job_kind_enum"),
        nullable=False,
    )
    #: Lo que se escanea: una referencia de imagen o un CIDR, según `kind`. Es texto libre
    #: porque lo valida el agente, que es quien sabe si existe, y no porque se acepte cualquier
    #: cosa: la forma la comprueba el esquema de entrada y la existencia la comprueba el agente.
    target: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[AgentJobStatusEnum] = mapped_column(
        SQLEnum(AgentJobStatusEnum, name="agent_job_status_enum"),
        nullable=False,
        default=AgentJobStatusEnum.QUEUED,
        server_default=AgentJobStatusEnum.QUEUED.name,
        index=True,
    )
    priority_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Fin de la reserva. Vencido sin resultado, el trabajo vuelve a `QUEUED`. Es lo que
    #: impide que un agente que se apaga deje un trabajo colgado para siempre.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: Los hechos que devuelve el agente: para un contenedor, el digest de la imagen, su
    #: sistema operativo y sus paquetes; para una red, los hosts vivos y sus puertos. JSONB y
    #: no `JSON` porque es binario, no deduplica claves y admite un índice GIN el día que exista
    #: una consulta del tipo "qué trabajos encontraron el puerto 22".
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: SHA-256 de la forma canónica de `result`. Se comprueba al leer; ver la clase.
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
