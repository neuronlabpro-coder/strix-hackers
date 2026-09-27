"""Modelos de la superficie de ataque: dominios verificados y activos descubiertos.

## Por qué `organization_id` es `RESTRICT` en las dos tablas

Por el mismo motivo que en los tickets: el inventario de activos es la **evidencia** de qué
se escaneó para cada cliente. En una disputa sobre un contrato de pentest, la lista de
dominios verificados y los hosts encontrados es lo que demuestra el alcance del trabajo.

Con `CASCADE` se perdería al dar de baja el workspace, que es la operación que más falta
hace para poder destruir la prueba. `RESTRICT` obliga a usar la baja lógica, que es la vía
correcta y conserva el inventario.

## Por qué `domain_id` es `CASCADE`

Porque un activo **pertenece** a su dominio. Un dominio sin verificar genera activos basura —
resultados de una resolución DNS sobre un nombre que el cliente no controla todavía— y
borrarlo tiene que llevárselos, o el inventario se llenaría de basura de dominios que ya no
existen y nunca se limpió.

Es el caso opuesto al de `organization_id` a propósito: uno es la prueba y no se toca, el otro
es un borrador y se va con su dueño.

## Por qué la unicidad es `(domain_id, asset_type, value)` y no solo `value`

Porque el mismo host puede pertenecer legítimamente a dos dominios de la misma organización:
`api.empresa.com` y `api.grupoholding.com` resuelven a la misma IP, y son dos activos
distintos con distinto contexto. Lo que no puede haber son dos filas del **mismo** activo en
el mismo dominio, y eso es lo que garantiza el índice único.

La unicidad global por `value` sería la que impide el alta de duplicados, pero descartaría
información real: dos dominios apuntando al mismo host son dos clientes afectados, no uno.

## Por qué el token de verificación es una columna y no un hash

Porque el servidor tiene que **comparar** el valor que viene del registro TXT con el
que guardó, y un hash no sirve: el token llega del exterior y solo existe en el DNS del
cliente, así que no hay nada local contra lo que aplicar la función de hash.

Guardar el token en claro significa que quien pueda leer la tabla conoce la prueba de
propiedad de los dominios. Esa tabla está detrás de R3, y leerla ya implica acceso a los
datos de todas las organizaciones, así que el token no añade exposición real. Aun así el
token **no** es un secreto de autenticación: solo sirve para probar que quien pide el
descubrimiento controla el DNS del nombre, y por eso se regenera en cada alta.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base


class DomainVerificationMethodEnum(StrEnum):
    """Cómo se prueba que el workspace controla el dominio.

    ## Por qué `HTTP_FILE` existe y por qué no se implementa

    Un método alternativo sería servir un fichero en `https://dominio/.well-known/fenix-...`.
    Se declara para que el catálogo de la columna sea completo y no haya que hacer una
    migración de tipo para añadirlo, **pero no se usa**: mediría la propiedad del dominio vía
    HTTP, que no distingue un dominio de un *CDN* que reenvía, y ese es precisamente el
    ataque que el descubrimiento de subdominios necesita cerrar.

    Declarar un valor que no se usa es preferible a no declararlo y necesitar `ALTER TYPE`
    —que en una tabla con datos de clientes tiene coste— el día que se implemente. Lo que no
    se hace es *aceptarlo* en la verificación: `verifier` solo implementa `DNS_TXT` y rechaza
    el resto, en vez de dar por buena una propiedad que no ha comprobado.
    """

    DNS_TXT = "DNS_TXT"
    HTTP_FILE = "HTTP_FILE"


class AssetTypeEnum(StrEnum):
    """La clase de activo descubierto.

    `SUBDOMAIN` es el resultado principal del descubrimiento pasivo; `IP_ADDRESS` y
    `API_ENDPOINT` son los que aparecen al resolver y al sondear. No hay `PORT_SERVICE` como
    tipo propio: un puerto con su servicio es un **atributo** de un host —columna
    `service_name`—, no un activo. Modelarlo como tipo haría que `nmap -sV` sobre una sola IP
    crease seis activos, y el inventario dejaría de describir la superficie para describir
    los puertos de una de sus máquinas.
    """

    SUBDOMAIN = "SUBDOMAIN"
    IP_ADDRESS = "IP_ADDRESS"
    API_ENDPOINT = "API_ENDPOINT"


class DomainClaimConflict(StrEnum):
    """Por qué se rechaza un alta de dominio. No es un detalle del mensaje: es la decisión.

    Vive en `models.py` y no en `service.py` porque **los dos lo necesitan** y
    `service` ya importa de `schemas`. Ponerlo en el servicio obligaría a que el esquema
    importara al servicio para describir una respuesta, que es un ciclo. Aquí no hay ciclo:
    `models` no importa nada del módulo.

    ## Por qué el motivo viaja estructurado y no solo en prosa

    Porque el panel ofrece una acción distinta según el caso: si el dominio ya está en este
    workspace, el botón correcto es abrir el dominio; si lo tiene otro, no hay ninguna
    acción posible y solo cabe un mensaje. Interpretar el texto para decidir eso lo ataría a
    un idioma.
    """

    #: Ya está en **otro** workspace. Es el caso de seguridad: hay que impedirlo siempre.
    OTRO_WORKSPACE = "OTRO_WORKSPACE"
    #: Ya está en **este** workspace. No es un problema de seguridad sino de usabilidad, y
    #: el mensaje tiene que decirlo, porque "otra organización" dejaría al usuario pensando
    #: que su alta se ha colado en el workspace equivocado.
    ESTE_WORKSPACE = "ESTE_WORKSPACE"
    #: Dos altas simultáneas del mismo nombre. Nadie lo ha pedido todavía; lo decide el
    #: `UNIQUE`. Solo el backstop de una carrera, y se trata como "otro workspace" porque en
    #: ese instante no se puede saber de quién es.
    CARRERA = "CARRERA"


class VerifiedDomain(Base):
    """Un dominio que el workspace ha demostrado controlar."""

    __tablename__ = "verified_domains"
    __table_args__ = (
        # Un dominio no puede estar dado de alta dos veces en el mismo workspace. Es un
        # `UNIQUE` y no un índice por rendimiento: el nombre es corto, y la restricción es lo
        # que evita que el mismo dominio aparezca dos veces con dos tokens distintos, que es
        # un estado en el que "verificar" no tiene respuesta clara.
        UniqueConstraint("organization_id", "domain_name", name="uq_verified_domains_org_name"),
        # La lista de dominios del panel es siempre "los míos, verificados primero". El
        # índice compuesto cubre ese acceso sin `ORDER BY`.
        Index("ix_verified_domains_org_verified", "organization_id", "is_verified"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: **Sin** `index=True`. El único índice de esta columna es el compuesto
    #: `ix_verified_domains_org_verified`, y su prefijo izquierdo es `organization_id`: un
    #: índice simple aquí no lo reemplazaría, PostgreSQL usaría el compuesto igual. Solo
    #: costaría una escritura adicional en cada alta de dominio.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: Normalizado: minúsculas y sin esquema ni barra final. La comparación de dominios es
    #: de cadenas y no de DNS, así que la normalización tiene que ser exacta y no una
    #: convención: `Empresa.COM` y `empresa.com` son el mismo nombre y tienen que ser la misma
    #: fila.
    domain_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: El valor completo del registro TXT, listo para copiar. Es exactamente lo que se
    #: publica en DNS, no solo el token suelto, para que el panel no pueda enseñar un valor
    #: distinto del que se verifica.
    verification_token: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_method: Mapped[DomainVerificationMethodEnum] = mapped_column(
        SQLEnum(DomainVerificationMethodEnum, name="domain_verification_method_enum"),
        nullable=False,
        default=DomainVerificationMethodEnum.DNS_TXT,
        server_default=DomainVerificationMethodEnum.DNS_TXT.name,
    )
    #: **Sin** `index=True`, por el mismo motivo que `organization_id`: no hay ninguna
    #: consulta que filtre por veracidad sin filtrar antes por organización, y el índice
    #: compuesto las cubre a las dos.
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class DiscoveredAsset(Base):
    """Un activo encontrado bajo un dominio verificado."""

    __tablename__ = "discovered_assets"
    __table_args__ = (
        # La deduplicación del descubrimiento. Ver la nota de la cabecera: por dominio y
        # tipo, no global, porque el mismo host puede pertenecer a dos dominios distintos.
        UniqueConstraint("domain_id", "asset_type", "value", name="uq_discovered_assets_unique"),
        # El inventario del panel se filtra por tenant y a veces por tipo, ordenado por última
        # revisión. Cubre el caso habitual, que es el listado sin filtro de tipo.
        Index("ix_discovered_assets_org_scanned", "organization_id", "last_scanned_at"),
        Index("ix_discovered_assets_org_type", "organization_id", "asset_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    domain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verified_domains.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: **Sin** `index=True`: es el prefijo izquierdo de los dos compuestos de esta tabla.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: **Sin** `index=True`: solo se filtra por tipo **junto a** `organization_id`, que es
    #: exactamente lo que hace `ix_discovered_assets_org_type`.
    asset_type: Mapped[AssetTypeEnum] = mapped_column(
        SQLEnum(AssetTypeEnum, name="asset_type_enum"),
        nullable=False,
    )
    #: El valor del activo tal como se encontró: un FQDN, una IP, o una URL. Es la clave
    #: lógica del activo y por eso participa en el único de la tabla.
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Lo que se sabe del servicio. Es texto libre y no un enum porque la enumeración de
    #: servicios no está cerrada y porque la plataforma no manda sobre lo que hay
    #: escuchando: `HTTPS / Nginx 1.24` y `SSH` tienen que poder convivir.
    service_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: Tecnologías detectadas. Se **fusiona** en cada descubrimiento, nunca se reemplaza: un
    #: escaneo posterior que no detecta Cloudflare no significa que Cloudflare haya
    #: desaparecido, y vaciar la lista haría que el inventario perdiera información por no
    #: haber ejecutado un escaneo completo.
    technologies: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    last_scanned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "AssetTypeEnum",
    "DiscoveredAsset",
    "DomainClaimConflict",
    "DomainVerificationMethodEnum",
    "VerifiedDomain",
]
