"""Esquemas de Supply Chain: inventario de dependencias del workspace.

## Por qué `has_vulnerabilities` sale como `bool | None` y no como `bool`

Porque son tres estados, y el que se pierde al tiparlo como `bool` es el que más importa:
**no comprobado**. Ver el encabezado de `backend/apps/supply_chain/models.py`.

En el panel los tres se ven distinto, y confundirlos con dos sería mentir en la dirección más
cara: decir "limpio" de algo que nadie ha mirado. Un cliente que recibe un falso "tu
dependencia está limpia" deja de comprobar por su cuenta, y ese es el daño.

## Por qué `cve_ids` es una lista y no un número

Porque "tiene 3 vulnerabilidades" no dice nada accionable y "tiene CVE-2024-3094" sí. El
número va en la longitud de la lista, y el panel enseña los identificadores para que el usuario
pueda buscarlos en la fuente oficial.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.apps.supply_chain.models import EcosystemEnum


class SupplyChainPackageItem(BaseModel):
    """Una dependencia en la respuesta del listado."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    version: str
    ecosystem: EcosystemEnum
    license: str | None
    #: `None` significa **no comprobado**, no "limpio". Ver el encabezado del modulo.
    has_vulnerabilities: bool | None
    cve_ids: list[str]
    is_dev_dependency: bool
    manifest_path: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    #: El repositorio del que viene. Viene porque el panel lo muestra en una columna y traerlo
    #: con un join es mejor que un `N+1` de una consulta por fila.
    repository_id: uuid.UUID
    repository_name: str


class SupplyChainPackagePage(BaseModel):
    """El listado paginado.

    `total` cuenta lo que cumple el filtro, no lo que devuelve la pagina. Un `len()` de la
    pagina no es el total, y con eso el panel no puede pintar la paginacion.
    """

    items: list[SupplyChainPackageItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class SupplyChainSummary(BaseModel):
    """Los numeros de cabecera.

    ## Por qué hay un `unchecked` y no solo `vulnerable` y `clean`

    Porque **"comprobado" no es "limpio"**. Un inventario donde todo esta sin comprobar y una
    tabla que solo distinguiera "vulnerable" de "limpio" diria que todo esta limpio. El
    contador de sin comprobar es el que hace que el usuario sepa, antes de mirar cualquier
    fila, cuanto de lo que ve no se sabe nada.
    """

    total_dependencies: int = Field(ge=0)
    vulnerable: int = Field(ge=0)
    clean: int = Field(ge=0)
    unchecked: int = Field(ge=0)
    #: Conteo por ecosistema, para los filtros de la cabecera.
    by_ecosystem: dict[str, int] = Field(default_factory=dict)
    #: Repositorios con al menos una dependencia indexada. Un repositorio sin manifiesto no
    #: aparece, y la diferencia entre los dos numeros es lo que dice quantos hay sin cubrir.
    repositories_indexed: int = Field(ge=0)


class SupplyChainIndexRequest(BaseModel):
    """Indexar un manifiesto que el cliente aporta.

    ## Por qué existe este endpoint y no un botón de "sincronizar"

    Porque sincronizar significa **traer** el manifiesto, y traerlo es la decisión que este
    proyecto todavía no ha tomado —ver el encabezado de `service.py`. Con este endpoint el
    cableado existe y es utilizable hoy, y el día que se decida una vía automática, quien la
    construya llama a `indexar_manifiesto` y esta ruta puede quedarse para depurar a un cliente
    concreto.

    El texto **no se persiste**: entra, se parsea y se descartan sus identificadores. Y la
    limpieza de credenciales de `manifests.py` se aplica antes de nada, de forma que un token
    en una URL de registro privado no llega a existir en la base.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    manifest_path: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=2_000_000)


class SupplyChainIndexResult(BaseModel):
    """Lo que una indexación ha producido."""

    inserted: int = Field(ge=0)
    updated: int = Field(ge=0)
    discarded: int = Field(ge=0)
    total: int = Field(ge=0)


__all__ = [
    "SupplyChainIndexRequest",
    "SupplyChainIndexResult",
    "SupplyChainPackageItem",
    "SupplyChainPackagePage",
    "SupplyChainSummary",
]
