"""Inventario de dependencias de los repositorios del workspace (Supply Chain, MENU-MAP §6.1).

## Por que una fila es **una dependencia declarada** y no un paquete instalado

Porque es lo unico que se puede afirmar sin mentir. El arbol de dependencias instalado de un
proyecto tiene cientos de paquetes transitivos, y resolverlo exige instalar el proyecto —o al
menos un `npm install` completo— en algun sitio. Este modulo solo lee los **manifiestos**, que
declaran las dependencias directas. Un inventario honesto de un manifiesto se puede construir
hoy; un inventario de un arbol resuelto, no.

La distincion se declara en el nombre de la columna y en el doc de la API, no en un comentario:
el panel lo llama "dependencias directas" y no "paquetes", porque un usuario que lea "tenemos
812 paquetes" y luego vea 40 en la tabla creera que faltan 772.

## Por que `has_vulnerabilities` es **nullable** y no `bool` con `default=False`

Porque son tres estados y con un booleano solo caben dos:

- `True`: se ha comprobado y tiene vulnerabilidades conocidas.
- `False`: se ha comprobado y esta limpio.
- `NULL`: **no se ha comprobado**, porque no hay fuente de Vulnerabilidades que lo compruebe.

El proyecto tiene `cve_records`, pero esa tabla guarda `cve_id`, severidad, CVSS, EPSS y una
descripcion: **no guarda que paquetes afecta cada CVE**. Buscar una dependencia por nombre
dentro del texto libre de una descripcion produce falsos positivos, y en una herramienta de
seguridad un "este paquete tiene un CVE" erroneo cuesta mas que un "no lo sabemos": el usuario
o deja de confiar en la herramienta o descarta una alerta real.

Con `default=False` la columna miente por construccion: todos los paquetes recien indexados
aparecerian como "limpios" sin que nadie los haya comprobado. Por eso el valor inicial es `NULL`
y el panel tiene un tercer estado, "sin comprobar", que no se confunde con "limpio".

## Que **no** guarda

Ni el contenido del manifiesto, ni una huella del arbol instalado, ni el codigo. R5: el codigo
fuente del cliente no se persiste nunca, y un manifiesto es parte de ese codigo. De el se extraen
**identificadores** —nombre, version, licencia— y nada mas. Ver `manifests.py`, que ademas
elimina las credenciales que los manifiestos traen consigo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

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


class EcosystemEnum(StrEnum):
    """De que gestor de paquetes sale la dependencia.

    `OTHER` no es un lujo de la enumeracion: es lo que recibe un `Dockerfile`, un `Gemfile` o
    un proyecto sin manifiesto reconocible, que son la mayoria de los repositorios que no son
    de aplicacion. Sin el, un repositorio con mixed ecosystemas no tendria donde ir y la
    dependencia directa se perderia en lugar de quedar marcada como no clasificada.
    """

    NPM = "NPM"
    PYPI = "PYPI"
    GO = "GO"
    CARGO = "CARGO"
    MAVEN = "MAVEN"
    COMPOSER = "COMPOSER"
    OTHER = "OTHER"


#: Manifiesto que produce cada ecosistema. Vive aqui y no en el parser para que la tabla y el
#: codigo no puedan separarse: anadir un ecosistema sin su manifiesto dejaria un valor de enum
#: que nada produce, y quitar un manifiesto sin quitar su valor dejaria uno que nada consume.
MANIFIESTO_POR_ECOSISTEMA: Final[dict[str, EcosystemEnum]] = {
    "package.json": EcosystemEnum.NPM,
    "requirements.txt": EcosystemEnum.PYPI,
    "go.mod": EcosystemEnum.GO,
    "Cargo.toml": EcosystemEnum.CARGO,
}


class SupplyChainPackage(Base):
    """Una dependencia directa declarada en el manifiesto de un repositorio.

    El nombre de la clase es `SupplyChainPackage` y no `SbomPackage` a proposito, por R2 y por
    honestidad tecnica: un **SBOM** es un documento normalizado —CycloneDX o SPDX— con un
    formato y una firma. Lo que esta tabla guarda es un inventario de dependencias directas
    leidas de un manifiesto, que es el paso anterior a un SBOM. Llamarlo SBOM haria que un
    cliente con una auditoria de cumplimiento buscase un documento que no existe.
    """

    __tablename__ = "supply_chain_packages"
    __table_args__ = (
        # Una dependencia con nombre y version, dentro de un repositorio, es una sola. El
        # `repository_id` va **primero** en el indice porque el filtro por repositorio es el
        # acceso mas frecuente —el panel lo pide al abrir un repo— y un indice que empieza por
        # `organization_id` obligaria a recorrer todas las dependencias de la organizacion para
        # descartar las de los demas repositorios.
        Index("ix_supply_chain_repo_name_version", "repository_id", "name", "version"),
        # El listado global ordenado por gravedad necesita poder filtrar por vulnerabilidad
        # sin recorrer la tabla. Se incluye `organization_id` porque ese filtro es **obligatorio**
        # (R3) y un indice que no lo cubre obliga a descartar en memoria lo de otros tenants.
        Index("ix_supply_chain_org_vulnerable", "organization_id", "has_vulnerabilities"),
        Index("ix_supply_chain_org_ecosystem", "organization_id", "ecosystem"),
        # Un mismo paquete no se puede declarar dos veces en un repositorio: `package.json`
        # puede repetirlo en `dependencies` y `devDependencies`, y `requirements.txt` admite
        # la misma pinned dos veces. Sin esta restriccion el inventario contaria de mas.
        UniqueConstraint(
            "repository_id",
            "name",
            "ecosystem",
            name="uq_supply_chain_repo_name_ecosystem",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    #: `CASCADE` y no `RESTRICT`, al contrario que en las conversaciones. Una dependencia no
    #: tiene asientos en el ledger ni es evidencia de nada: cuando se da de baja el repositorio
    #: o el tenant, su inventario no tiene por que sobrevivir. Un `RESTRICT` obligaria a
    #: borrarlo a mano y dejaria el repositorio sin poder borrarse.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: `CASCADE` por la misma razon. Es la columna de la clave natural de la fila.
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Se guarda como texto y no como `version` de empaquetado. Los rangos de npm
    #: (`^4.17.21`) y de pip (`>=2.0,<3.0`) no son versiones: son **declaraciones de rango**, y
    #: guardarlas en una columna de version haria que un `ORDER BY` por version ordenara
    #: alfabeticamente y que una busqueda por version fallara en silencio.
    version: Mapped[str] = mapped_column(String(100), nullable=False)

    ecosystem: Mapped[EcosystemEnum] = mapped_column(
        SQLEnum(
            EcosystemEnum,
            name="ecosystem_enum",
            # Sin esto, SQLAlchemy persiste el `.name` en vez del `.value`. Aqui coinciden
            # porque los dos son el mismo texto, pero se declara explicito para que anadir un
            # miembro en minusculas no empiece a guardar algo distinto de lo que se lee.
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )

    #: `NULL` cuando el manifiesto no la declara. `package.json` solo trae licencia en la raiz y
    #: muchos paquetes no la ponen; inventar "UNKNOWN" seria un dato falso, y medir la cobertura
    #: de licencias es precisamente una de las razones por las que este inventario existe.
    license: Mapped[str | None] = mapped_column(String(100), nullable=True)

    #: Ver el encabezado del modulo para por que es nullable y no `bool`.
    has_vulnerabilities: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=None, server_default=None
    )

    #: Identificadores CVE asociados. `JSONB` y no texto: la consulta natural es "este paquete
    #: tiene el CVE-2024-1234", y sobre un `array(String)` PostgreSQL puede usar el indice GIN y
    #: sobre `text` tendria que buscar por subcadena.
    cve_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    #: El manifiesto del que salio, para poder explicar de donde viene cada fila sin guardarlo.
    manifest_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: `TRUE` cuando la dependencia viene de `devDependencies`. Es la distincion que mas
    #: importa en supply chain y la que un inventario plano esconde: una vulnerabilidad en una
    #: dependencia de desarrollo no llega a produccion, y sin la columna el panel obliga al
    #: usuario a averiguarlo mirando el manifiesto a mano.
    is_dev_dependency: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["MANIFIESTO_POR_ECOSISTEMA", "EcosystemEnum", "SupplyChainPackage"]
