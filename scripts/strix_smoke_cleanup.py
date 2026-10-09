"""Cleanup verificable para smokes Strix locales, incluidos creates parciales."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

LOG = logging.getLogger(__name__)
MAX_REMOVE_ATTEMPTS = 3


class ResourceAdapter(Protocol):
    """Operaciones acotadas a los recursos creados por un único smoke."""

    def inventory(self) -> dict[str, set[str]]: ...

    def remove_container(self, container_id: str) -> None: ...

    def remove_network(self, network_id: str) -> None: ...


class SmokeCleanupError(RuntimeError):
    """Al menos un recurso del smoke sigue presente o no se pudo verificar."""


@dataclass(frozen=True)
class SmokeInventory:
    resources_before: dict[str, set[str]]
    resources_created: dict[str, set[str]]
    resources_removed: dict[str, set[str]]
    resources_after: dict[str, set[str]]
    orphans: dict[str, set[str]]

    @property
    def cleanup_pass(self) -> bool:
        return not any(self.orphans.values())

    def as_dict(self) -> dict[str, object]:
        def sorted_resources(value: dict[str, set[str]]) -> dict[str, list[str]]:
            return {kind: sorted(value[kind]) for kind in ("containers", "networks")}

        return {
            "RESOURCES_BEFORE": sorted_resources(self.resources_before),
            "RESOURCES_CREATED": sorted_resources(self.resources_created),
            "RESOURCES_REMOVED": sorted_resources(self.resources_removed),
            "RESOURCES_AFTER": sorted_resources(self.resources_after),
            "ORPHANS": sorted_resources(self.orphans),
            "CLEANUP": "PASS" if self.cleanup_pass else "FAIL",
        }


class SmokeCleanup:
    """Registra cada ID al crearse y comprueba su desaparición antes de avanzar."""

    def __init__(
        self,
        adapter: ResourceAdapter,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.adapter = adapter
        self.sleep = sleep
        self.before = self._inventory()
        self.created: dict[str, set[str]] = {"containers": set(), "networks": set()}
        self.removed: dict[str, set[str]] = {"containers": set(), "networks": set()}
        self.report: SmokeInventory | None = None

    def _inventory(self) -> dict[str, set[str]]:
        result = self.adapter.inventory()
        if set(result) != {"containers", "networks"}:
            raise SmokeCleanupError("Inventario de Docker incompleto")
        return {kind: set(result[kind]) for kind in ("containers", "networks")}

    def track_container(self, container_id: str) -> None:
        if not container_id:
            raise ValueError("Container ID vacío")
        self.created["containers"].add(container_id)

    def track_network(self, network_id: str) -> None:
        if not network_id:
            raise ValueError("Network ID vacío")
        self.created["networks"].add(network_id)

    def _remove(self, kind: str, resource_id: str) -> bool:
        remove = (
            self.adapter.remove_container
            if kind == "containers"
            else self.adapter.remove_network
        )
        for attempt in range(1, MAX_REMOVE_ATTEMPTS + 1):
            try:
                remove(resource_id)
            except Exception:
                LOG.exception("Cleanup %s %s falló (intento %s)", kind, resource_id, attempt)
            try:
                present = resource_id in self._inventory()[kind]
            except Exception:
                LOG.exception("No se pudo verificar cleanup %s %s", kind, resource_id)
                present = True
            if not present:
                self.removed[kind].add(resource_id)
                return True
            if attempt < MAX_REMOVE_ATTEMPTS:
                self.sleep(0.2)
        return False

    def close(self) -> SmokeInventory:
        # Si Docker creó el recurso pero falló el parsing de la respuesta, el
        # inventario por label del run lo descubre antes del primer DELETE.
        current = self._inventory()
        for kind in ("containers", "networks"):
            self.created[kind].update(current[kind] - self.before[kind])
        # Si queda un contenedor, no se retira antes su red ni se declara PASS.
        containers_clean = True
        for resource_id in sorted(self.created["containers"]):
            if not self._remove("containers", resource_id):
                containers_clean = False
        if containers_clean:
            for resource_id in sorted(self.created["networks"]):
                self._remove("networks", resource_id)
        after = self._inventory()
        orphans = {
            kind: self.created[kind] & after[kind] for kind in ("containers", "networks")
        }
        self.report = SmokeInventory(self.before, self.created, self.removed, after, orphans)
        LOG.info("Inventario smoke: %s", self.report.as_dict())
        if not self.report.cleanup_pass:
            raise SmokeCleanupError(f"Recursos huérfanos: {self.report.as_dict()}")
        return self.report

    def __enter__(self) -> SmokeCleanup:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
