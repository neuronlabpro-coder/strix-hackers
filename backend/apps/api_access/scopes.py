"""Catálogo oficial de scopes de la API de Mind Guard Fenix Team.

Un scope es una promesa: si el token lo tiene, la plataforma promete hacer esa cosa. Por
eso el catálogo es **inmutable y declarado aquí**, no derivado de los endpoints ni
construido por convención a partir de los nombres de ruta. Un catálogo derivado de los
endpoints se ampliaría solo cada vez que se añadiera una ruta, y nadie decidiría si esa
ruta es un permiso nuevo.

## Nombres de los permisos

`grupo:acción`, con `:` como separador. Los grupos son los que ve el panel, así que un
scope de `billing` aparece bajo "Facturación" sin trabajo adicional.

Se distinguen `lectura` y `escritura` de forma explícita (`ledger_read`, `supply_chain_write`)
y no por presencia de un sufijo. La razón es práctica: una lista de permisos con un
único nombre por recurso obliga a elegir entre "leer todo lo del recurso" o "nada del
recurso", y las dos opciones son inaceptables a la vez. El coste es un scope más en
algunos grupos, que es el precio de poder conceder lectura sin conceder escritura.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Scope(StrEnum):
    """Los 46 permisos del catálogo oficial.

    Es un `StrEnum` y no un `str` suelto porque `Scope.PENTESTS_READ == "pentests:read"`
    tiene que ser cierto para que los valores se puedan comparar con lo que viene en la
    petición, del JSON guardado y de la configuración. `StrEnum` da las dos cosas sin
    una capa de conversión.
    """

    # Pentests
    PENTESTS_READ = "pentests:read"
    PENTESTS_CREATE = "pentests:create"
    PENTESTS_ABORT = "pentests:abort"
    PENTESTS_DELETE = "pentests:delete"

    # Vulnerabilidades
    VULNERABILITIES_READ = "vulnerabilities:read"
    VULNERABILITIES_TRIAGE = "vulnerabilities:triage"
    VULNERABILITIES_EXPORT = "vulnerabilities:export"

    # Repositorios
    REPOSITORIES_READ = "repositories:read"
    REPOSITORIES_CONNECT = "repositories:connect"
    REPOSITORIES_SYNC = "repositories:sync"
    REPOSITORIES_DELETE = "repositories:delete"

    # Revisiones de PR
    PR_REVIEWS_READ = "pr_reviews:read"
    PR_REVIEWS_TRIGGER = "pr_reviews:trigger"

    # Conocimiento de negocio
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_WRITE = "knowledge:write"
    KNOWLEDGE_DELETE = "knowledge:delete"

    # CVE
    CVE_READ = "cve:read"
    CVE_SEARCH = "cve:search"

    # Facturación
    BILLING_READ = "billing:read"
    BILLING_CHECKOUT = "billing:checkout"
    BILLING_LEDGER_READ = "billing:ledger_read"

    # Tokens
    TOKENS_READ = "tokens:read"
    TOKENS_CREATE = "tokens:create"
    TOKENS_REVOKE = "tokens:revoke"

    # Webhooks
    WEBHOOKS_READ = "webhooks:read"
    WEBHOOKS_CREATE = "webhooks:create"
    WEBHOOKS_UPDATE = "webhooks:update"
    WEBHOOKS_DELETE = "webhooks:delete"

    # Organización
    ORGANIZATION_READ = "organization:read"
    ORGANIZATION_UPDATE = "organization:update"

    # Miembros
    MEMBERS_READ = "members:read"
    MEMBERS_INVITE = "members:invite"
    MEMBERS_REMOVE = "members:remove"

    # Auditoría
    AUDIT_READ = "audit:read"

    # Enterprise
    ENTERPRISE_NETWORKS_READ = "enterprise:networks_read"
    ENTERPRISE_NETWORKS_WRITE = "enterprise:networks_write"
    ENTERPRISE_CONTAINERS_READ = "enterprise:containers_read"
    ENTERPRISE_CONTAINERS_WRITE = "enterprise:containers_write"
    ENTERPRISE_SUPPLY_CHAIN_READ = "enterprise:supply_chain_read"
    ENTERPRISE_SUPPLY_CHAIN_WRITE = "enterprise:supply_chain_write"

    # Orquestador LLM
    LLM_MODELS_READ = "llm:models_read"
    LLM_USAGE_READ = "llm:usage_read"

    # Administración de plataforma
    ADMIN_MODELS_MANAGE = "admin:models_manage"
    ADMIN_ANALYTICS_READ = "admin:analytics_read"

    # MCP
    MCP_CONNECT = "mcp:connect"
    MCP_INVOKE = "mcp:invoke"


@dataclass(frozen=True, slots=True)
class ScopeDefinition:
    """Un permiso y sus metadatos de presentación y de permiso.

    `label_key` existe para que el panel no tenga que traducir 46 cadenas por su cuenta
    y para que el nombre se pueda cambiar sin tocar el contrato. `is_privileged` marca
    los scopes que conceden capacidad de plataforma: asignables a un token, pero su alta
    pide confirmación explícita y el panel los muestra aparte de los de solo lectura.
    """

    scope: Scope
    label_key: str
    is_privileged: bool = False

    @property
    def group(self) -> str:
        return self.scope.value.split(":", 1)[0]

    @property
    def action(self) -> str:
        return self.scope.value.split(":", 1)[1]


def _definitions(*items: tuple[Scope, str, bool]) -> tuple[ScopeDefinition, ...]:
    return tuple(ScopeDefinition(*item) for item in items)


# Agrupado por recurso, que es como lo presenta el panel. El orden de los grupos es el
# de la navegación principal, no alfabético: el panel enumera permisos y el orden es la
# jerarquía de la plataforma.
SCOPE_CATALOG: Final[MappingProxyType[str, tuple[ScopeDefinition, ...]]] = (
    MappingProxyType(
        {
            "pentests": _definitions(
                (Scope.PENTESTS_READ, "scopes.pentests.read", False),
                (Scope.PENTESTS_CREATE, "scopes.pentests.create", False),
                (Scope.PENTESTS_ABORT, "scopes.pentests.abort", False),
                (Scope.PENTESTS_DELETE, "scopes.pentests.delete", False),
            ),
            "vulnerabilities": _definitions(
                (Scope.VULNERABILITIES_READ, "scopes.vulnerabilities.read", False),
                (Scope.VULNERABILITIES_TRIAGE, "scopes.vulnerabilities.triage", False),
                (Scope.VULNERABILITIES_EXPORT, "scopes.vulnerabilities.export", False),
            ),
            "repositories": _definitions(
                (Scope.REPOSITORIES_READ, "scopes.repositories.read", False),
                (Scope.REPOSITORIES_CONNECT, "scopes.repositories.connect", True),
                (Scope.REPOSITORIES_SYNC, "scopes.repositories.sync", True),
                (Scope.REPOSITORIES_DELETE, "scopes.repositories.delete", True),
            ),
            "pr_reviews": _definitions(
                (Scope.PR_REVIEWS_READ, "scopes.prReviews.read", False),
                (Scope.PR_REVIEWS_TRIGGER, "scopes.prReviews.trigger", True),
            ),
            "knowledge": _definitions(
                (Scope.KNOWLEDGE_READ, "scopes.knowledge.read", False),
                (Scope.KNOWLEDGE_WRITE, "scopes.knowledge.write", True),
                (Scope.KNOWLEDGE_DELETE, "scopes.knowledge.delete", True),
            ),
            "cve": _definitions(
                (Scope.CVE_READ, "scopes.cve.read", False),
                (Scope.CVE_SEARCH, "scopes.cve.search", False),
            ),
            "billing": _definitions(
                (Scope.BILLING_READ, "scopes.billing.read", False),
                (Scope.BILLING_CHECKOUT, "scopes.billing.checkout", True),
                # Leer el libro de asientos expone el historial financiero completo del
                # tenant, así que se marca como privilegiado aunque solo lea.
                (Scope.BILLING_LEDGER_READ, "scopes.billing.ledgerRead", True),
            ),
            "tokens": _definitions(
                (Scope.TOKENS_READ, "scopes.tokens.read", False),
                (Scope.TOKENS_CREATE, "scopes.tokens.create", True),
                (Scope.TOKENS_REVOKE, "scopes.tokens.revoke", True),
            ),
            "webhooks": _definitions(
                (Scope.WEBHOOKS_READ, "scopes.webhooks.read", False),
                (Scope.WEBHOOKS_CREATE, "scopes.webhooks.create", True),
                (Scope.WEBHOOKS_UPDATE, "scopes.webhooks.update", True),
                (Scope.WEBHOOKS_DELETE, "scopes.webhooks.delete", True),
            ),
            "organization": _definitions(
                (Scope.ORGANIZATION_READ, "scopes.organization.read", False),
                (Scope.ORGANIZATION_UPDATE, "scopes.organization.update", True),
            ),
            "members": _definitions(
                (Scope.MEMBERS_READ, "scopes.members.read", False),
                (Scope.MEMBERS_INVITE, "scopes.members.invite", True),
                (Scope.MEMBERS_REMOVE, "scopes.members.remove", True),
            ),
            "audit": _definitions(
                (Scope.AUDIT_READ, "scopes.audit.read", True),
            ),
            "enterprise": _definitions(
                (Scope.ENTERPRISE_NETWORKS_READ, "scopes.enterprise.networksRead", False),
                (Scope.ENTERPRISE_NETWORKS_WRITE, "scopes.enterprise.networksWrite", True),
                (Scope.ENTERPRISE_CONTAINERS_READ, "scopes.enterprise.containersRead", False),
                (Scope.ENTERPRISE_CONTAINERS_WRITE, "scopes.enterprise.containersWrite", True),
                (Scope.ENTERPRISE_SUPPLY_CHAIN_READ, "scopes.enterprise.supplyChainRead", False),
                (Scope.ENTERPRISE_SUPPLY_CHAIN_WRITE, "scopes.enterprise.supplyChainWrite", True),
            ),
            "llm": _definitions(
                (Scope.LLM_MODELS_READ, "scopes.llm.modelsRead", False),
                (Scope.LLM_USAGE_READ, "scopes.llm.usageRead", False),
            ),
            "admin": _definitions(
                (Scope.ADMIN_MODELS_MANAGE, "scopes.admin.modelsManage", True),
                (Scope.ADMIN_ANALYTICS_READ, "scopes.admin.analyticsRead", True),
            ),
            "mcp": _definitions(
                (Scope.MCP_CONNECT, "scopes.mcp.connect", True),
                (Scope.MCP_INVOKE, "scopes.mcp.invoke", True),
            ),
        }
    )
)

#: Los 46 scopes en orden estable. Tupla inmutable: el orden lo consume el panel, y una
#: lista sería mutable desde cualquier módulo que la importara.
ALL_SCOPES: Final[tuple[Scope, ...]] = tuple(
    definition.scope
    for group in SCOPE_CATALOG.values()
    for definition in group
)

_KNOWN_SCOPES: Final[frozenset[str]] = frozenset(scope.value for scope in ALL_SCOPES)

PRIVILEGED_SCOPES: Final[frozenset[Scope]] = frozenset(
    definition.scope
    for group in SCOPE_CATALOG.values()
    for definition in group
    if definition.is_privileged
)


def scope_is_known(value: str) -> bool:
    """Indica si el valor pertenece al catálogo.

    Se compara contra el conjunto de valores exactos y no se intenta normalizar antes:
    un scope con espacios alrededor es un error del cliente y merece un 422 que lo diga,
    no un token que funciona con un permiso que nadie eligió.
    """

    return value in _KNOWN_SCOPES


def unknown_scopes(values: list[str]) -> list[str]:
    """Devuelve los valores que no pertenecen al catálogo, en el orden recibido.

    Se usa para que el 422 pueda enumerar los scopes concretos que se rechazaron. Un
    "scope inválido" sin decir cuál obliga al cliente a comparar listas a mano.
    """

    return [value for value in values if not scope_is_known(value)]


def normalize_scopes(values: list[str]) -> tuple[Scope, ...]:
    """Convierte cadenas en scopes ordenados y sin duplicados.

    Solo acepta valores que ya están en el catálogo: usa `Scope(...)`, que lanza
    `ValueError` ante un desconocido. Es la función de bajo nivel, y quien tenga que
    validar antes de convertir usa `require_known_scopes`.

    Ordena porque la fila guardada tiene que ser comparable: dos peticiones con los
    mismos permisos en distinto orden producen la misma lista, y así el panel no ve dos
    tokens idénticos como si fueran distintos.
    """

    unicos = dict.fromkeys(values)
    return tuple(sorted((Scope(item) for item in unicos), key=lambda s: s.value))


def require_known_scopes(values: list[str]) -> tuple[Scope, ...]:
    """Valida y normaliza, o lanza `ValueError` nombrando lo que no se reconoce."""

    rejected = unknown_scopes(values)
    if rejected:
        raise ValueError(f"Scopes desconocidos: {', '.join(rejected)}")
    return normalize_scopes(values)
