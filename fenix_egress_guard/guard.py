"""Servicio del host: aplica, verifica y atesta el cerco Strix de Fenix."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from fenix_egress_guard.policy import CHAIN, Policy

LOG = logging.getLogger("fenix_egress_guard")
CHECK_INTERVAL_SECONDS = 2
ATTEST_TTL_SECONDS = 5


class GuardError(RuntimeError):
    """La política no puede aplicarse o comprobarse con seguridad."""


def _run(*args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # noqa: S603 - argumentos cerrados, sin shell
            args, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GuardError(f"No se pudo ejecutar {args[0]}") from error
    if result.returncode and not allow_failure:
        raise GuardError(f"Comando falló: {args[0]} {args[1:]}")
    return result


def _docker_networks() -> list[dict[str, Any]]:
    ids = _run("docker", "network", "ls", "-q").stdout.split()
    if not ids:
        raise GuardError("Docker no devolvió redes")
    try:
        networks = json.loads(_run("docker", "network", "inspect", *ids).stdout)
    except (ValueError, TypeError) as error:
        raise GuardError("Inventario Docker inválido") from error
    if not isinstance(networks, list):
        raise GuardError("Inventario Docker inválido")
    return networks


def _verify_network(policy: Policy) -> None:
    networks = _docker_networks()
    candidates = [item for item in networks if item.get("Name") == policy.network_name]
    if len(candidates) != 1:
        raise GuardError("La red exclusiva Strix no existe o está duplicada")
    network = candidates[0]
    if (
        network.get("Driver") != "bridge"
        or network.get("EnableIPv6") is not False
        or network.get("Internal") is not False
        or (network.get("Labels") or {}).get("com.mindguard.fenix") != "true"
        or (network.get("Options") or {}).get("com.docker.network.bridge.enable_icc") != "false"
    ):
        raise GuardError("La red Strix no cumple la configuración exclusiva IPv4")
    ipam = (network.get("IPAM") or {}).get("Config") or []
    if len(ipam) != 1 or ipam[0].get("Subnet") != str(policy.network_subnet):
        raise GuardError("Subnet de Docker distinta del /24 aprobado")
    try:
        gateway = ipaddress.IPv4Address(ipam[0]["Gateway"])
    except (KeyError, ipaddress.AddressValueError) as error:
        raise GuardError("Gateway Docker inválido") from error
    if gateway not in policy.network_subnet:
        raise GuardError("Gateway fuera del /24 Strix")
    for other in networks:
        if other.get("Id") == network.get("Id"):
            continue
        for entry in (other.get("IPAM") or {}).get("Config") or []:
            raw = entry.get("Subnet")
            if not raw:
                continue
            try:
                subnet = ipaddress.ip_network(raw, strict=False)
            except ValueError as error:
                raise GuardError("Subnet Docker ajena inválida") from error
            if subnet.version == 4 and subnet.overlaps(policy.network_subnet):
                raise GuardError("El /24 Strix solapa una red Docker ajena")
    for container_id in (network.get("Containers") or {}):
        try:
            inspected = json.loads(_run("docker", "container", "inspect", container_id).stdout)
            labels = inspected[0]["Config"]["Labels"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise GuardError("Container de la red Strix no verificable") from error
        if labels.get("com.mindguard.fenix") != "true":
            raise GuardError("Container ajeno conectado a la red Strix")


def _rule_lines(chain: str) -> list[tuple[str, ...]] | None:
    result = _run("iptables", "-w", "-t", "filter", "-S", chain, allow_failure=True)
    if result.returncode:
        return None
    lines = []
    for line in result.stdout.splitlines():
        if not line.startswith("-A "):
            continue
        tokens = shlex.split(line)
        for protocol in ("tcp", "udp"):
            if "-m" in tokens:
                index = tokens.index("-m")
                if tokens[index + 1] == protocol:
                    del tokens[index : index + 2]
        lines.append(tuple(tokens))
    return lines


def _expected_chain(policy: Policy) -> list[tuple[str, ...]]:
    return [("-A", CHAIN, *rule) for rule in policy.chain_rules()]


def _verify_firewall(policy: Policy) -> None:
    if _rule_lines(CHAIN) != _expected_chain(policy):
        raise GuardError("La cadena Fenix está ausente o alterada")
    expected_jump = ("-A", "", *policy.jump_rule())
    for parent in ("DOCKER-USER", "INPUT"):
        lines = _rule_lines(parent)
        if not lines or lines[0] != (expected_jump[0], parent, *expected_jump[2:]):
            raise GuardError(f"Salto Fenix ausente o no prioritario en {parent}")
        if sum(1 for line in lines if line[-1] == CHAIN) != 1:
            raise GuardError(f"Salto Fenix duplicado en {parent}")


def check(policy: Policy) -> None:
    """Solo lecturas: Docker inspect e iptables -S."""
    _verify_network(policy)
    _verify_firewall(policy)


def apply(policy: Policy) -> None:
    """Solo crea la cadena propia y dos saltos acotados al /24 Fenix."""
    _verify_network(policy)
    existing = _rule_lines(CHAIN)
    if existing is None:
        _run("iptables", "-w", "-t", "filter", "-N", CHAIN)
        for rule in policy.chain_rules():
            _run("iptables", "-w", "-t", "filter", "-A", CHAIN, *rule)
    elif existing != _expected_chain(policy):
        raise GuardError("Cadena Fenix alterada; no se modifica una política en uso")
    for parent in ("DOCKER-USER", "INPUT"):
        lines = _rule_lines(parent)
        if lines is None:
            raise GuardError(f"Cadena {parent} ausente")
        jump = ("-A", parent, *policy.jump_rule())
        if jump not in lines:
            _run("iptables", "-w", "-t", "filter", "-I", parent, "1", *policy.jump_rule())
        elif lines[0] != jump:
            raise GuardError(f"Salto Fenix no prioritario en {parent}")
    check(policy)


def _boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not value:
        raise GuardError("boot_id no disponible")
    return value


def attest(policy: Policy, *, now: float | None = None, boot_id: str | None = None) -> None:
    """Publica una prueba breve y atómica solo tras check satisfactorio."""
    checked = time.time() if now is None else now
    status = {
        "status": "PASS",
        "network_name": policy.network_name,
        "network_subnet": str(policy.network_subnet),
        "boot_id": _boot_id() if boot_id is None else boot_id,
        "policy_hash": policy.policy_hash(),
        "checked_at": checked,
        "expires_at": checked + ATTEST_TTL_SECONDS,
    }
    parent = policy.status_path.parent
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".egress-", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), 0o644)
            json.dump(status, file, sort_keys=True, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, policy.status_path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def invalidate(policy: Policy) -> None:
    policy.status_path.unlink(missing_ok=True)


def refresh(policy: Policy) -> None:
    try:
        check(policy)
        attest(policy)
    except (GuardError, OSError, ValueError):
        invalidate(policy)
        raise


def serve(policy: Policy) -> None:
    try:
        apply(policy)
        refresh(policy)
        while True:
            time.sleep(CHECK_INTERVAL_SECONDS)
            try:
                refresh(policy)
            except (GuardError, OSError, ValueError):
                LOG.exception("Cerco Fenix inválido; atestación retirada")
    except BaseException:
        invalidate(policy)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("policy-hash", "apply", "check", "serve"))
    action = parser.parse_args().action
    policy = Policy.from_env()
    logging.basicConfig(level=logging.INFO)
    if action == "policy-hash":
        print(policy.policy_hash())
    elif action == "apply":
        apply(policy)
    elif action == "check":
        check(policy)
    else:
        serve(policy)


if __name__ == "__main__":
    main()
