"""Política cerrada para la red Strix de producción."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from dataclasses import dataclass
from pathlib import Path

NETWORK_NAME = "fenix-team-strix-prod"
NETWORK_SUBNET = ipaddress.IPv4Network("172.31.250.0/24")
TEST_NETWORK_NAME = "fenix-team-strix-test"
APPROVED_DNS = (ipaddress.IPv4Address("1.1.1.1"), ipaddress.IPv4Address("1.0.0.1"))
CHAIN = "FENIX_TEAM_EGRESS"
STATUS_PATH = Path("/run/mindguard-fenix/egress-status.json")
COMMENT = "fenix-team-egress"
POLICY_VERSION = 1
BLOCKED_DESTINATIONS = (
    "127.0.0.0/8",
    "169.254.0.0/16",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "0.0.0.0/8",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


@dataclass(frozen=True)
class Policy:
    network_name: str
    network_subnet: ipaddress.IPv4Network
    dns_resolvers: tuple[ipaddress.IPv4Address, ...]
    status_path: Path = STATUS_PATH

    @classmethod
    def from_env(cls) -> Policy:
        name = os.environ.get("FENIX_STRIX_NETWORK", "")
        subnet = os.environ.get("FENIX_STRIX_SUBNET", "")
        resolvers = os.environ.get("FENIX_DNS_RESOLVERS", "")
        test_mode = os.environ.get("FENIX_GUARD_TEST_MODE") == "true"
        if test_mode:
            if name != TEST_NETWORK_NAME:
                raise ValueError("Modo test requiere la red Strix de prueba")
            try:
                selected_subnet = ipaddress.IPv4Network(subnet, strict=True)
            except ValueError as error:
                raise ValueError("Subnet de prueba inválida") from error
            if selected_subnet.prefixlen != 24 or not selected_subnet.is_private:
                raise ValueError("Subnet de prueba debe ser un /24 privado")
        elif name != NETWORK_NAME or subnet != str(NETWORK_SUBNET):
            raise ValueError("La red Strix PROD debe ser exactamente el /24 aprobado")
        else:
            selected_subnet = NETWORK_SUBNET
        try:
            dns = tuple(ipaddress.IPv4Address(value.strip()) for value in resolvers.split(","))
        except ipaddress.AddressValueError as error:
            raise ValueError("FENIX_DNS_RESOLVERS debe contener IPv4") from error
        if dns != APPROVED_DNS:
            raise ValueError("Solo se permiten los dos resolvedores DNS aprobados, en orden")
        return cls(name, selected_subnet, dns)

    def chain_rules(self) -> tuple[tuple[str, ...], ...]:
        # DNS embebido de Docker (127.0.0.11) es local al namespace del sandbox.
        # No necesita acceso IP al gateway del host. La regla LOCAL bloquea también
        # la IP pública del propio host, incluso si escucha HTTPS.
        deny = tuple(("-d", cidr, "-j", "DROP") for cidr in BLOCKED_DESTINATIONS)
        dns = tuple(
            ("-d", f"{resolver}/32", "-p", protocol, "--dport", "53", "-j", "RETURN")
            for resolver in self.dns_resolvers
            for protocol in ("udp", "tcp")
        )
        return (
            ("-m", "addrtype", "--dst-type", "LOCAL", "-j", "DROP"),
            *deny,
            *dns,
            ("-p", "tcp", "--dport", "443", "-j", "RETURN"),
            ("-j", "DROP"),
        )

    def jump_rule(self) -> tuple[str, ...]:
        return (
            "-s",
            str(self.network_subnet),
            "-m",
            "comment",
            "--comment",
            COMMENT,
            "-j",
            CHAIN,
        )

    def policy_hash(self) -> str:
        document = {
            "version": POLICY_VERSION,
            "network_name": self.network_name,
            "network_subnet": str(self.network_subnet),
            "dns_resolvers": [str(resolver) for resolver in self.dns_resolvers],
            "network_driver": "bridge",
            "network_ipv6": False,
            "network_internal": False,
            "network_icc": False,
            "network_owner_label": "com.mindguard.fenix=true",
            "chain": CHAIN,
            "chain_rules": self.chain_rules(),
            "jump_parents": ("DOCKER-USER", "INPUT"),
            "jump_rule": self.jump_rule(),
            "default": "DROP",
        }
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()
