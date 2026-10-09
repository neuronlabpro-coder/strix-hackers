"""Pruebas sin tocar firewall ni daemon Docker reales."""

# ruff: noqa: S101

from __future__ import annotations

import ipaddress
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from backend.workers.runner.egress_attestation import verify_attestation
from backend.workers.runner.egress_fence import EgressFenceMissingError
from fenix_egress_guard import guard
from fenix_egress_guard.policy import APPROVED_DNS, CHAIN, NETWORK_NAME, NETWORK_SUBNET, Policy


class FakeHost:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.chains: dict[str, list[tuple[str, ...]]] = {"INPUT": [], "DOCKER-USER": []}
        self.mutations: list[tuple[str, ...]] = []
        self.networks = [
            {
                "Id": "a" * 64,
                "Name": NETWORK_NAME,
                "Driver": "bridge",
                "EnableIPv6": False,
                "Internal": False,
                "Labels": {"com.mindguard.fenix": "true"},
                "Options": {"com.docker.network.bridge.enable_icc": "false"},
                "IPAM": {"Config": [{"Subnet": str(NETWORK_SUBNET), "Gateway": "172.31.250.1"}]},
                "Containers": {},
            },
            {
                "Id": "b" * 64,
                "Name": "mindguard-original",
                "IPAM": {"Config": [{"Subnet": "172.19.0.0/16"}]},
            },
        ]

    def run(self, *args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
        del allow_failure
        if args[:3] == ("docker", "network", "ls"):
            return subprocess.CompletedProcess(args, 0, "a\nb\n", "")
        if args[:3] == ("docker", "network", "inspect"):
            return subprocess.CompletedProcess(args, 0, json.dumps(self.networks), "")
        if args[:4] != ("iptables", "-w", "-t", "filter"):
            raise AssertionError(args)
        action, chain, *rule = args[4:]
        if action == "-S":
            if chain not in self.chains:
                return subprocess.CompletedProcess(args, 1, "", "missing")
            lines = ["-N " + chain]
            lines.extend(" ".join(("-A", chain, *entry)) for entry in self.chains[chain])
            return subprocess.CompletedProcess(args, 0, "\n".join(lines), "")
        self.mutations.append(args)
        if action == "-N":
            self.chains[chain] = []
        elif action == "-A":
            self.chains[chain].append(tuple(rule))
        elif action == "-I":
            assert rule[0] == "1"
            self.chains[chain].insert(0, tuple(rule[1:]))
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def policy(tmp_path: Path) -> Policy:
    return Policy(
        NETWORK_NAME,
        NETWORK_SUBNET,
        APPROVED_DNS,
        tmp_path / "egress-status.json",
    )


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, policy: Policy) -> FakeHost:
    fake = FakeHost(policy)
    monkeypatch.setattr(guard, "_run", fake.run)
    return fake


def test_apply_solo_crea_cadena_propia_y_dos_saltos(policy: Policy, host: FakeHost) -> None:
    guard.apply(policy)
    assert set(host.chains) == {"INPUT", "DOCKER-USER", CHAIN}
    assert len([command for command in host.mutations if command[4] == "-I"]) == 2
    assert all(command[4] in {"-N", "-A", "-I"} for command in host.mutations)
    before = list(host.mutations)
    guard.apply(policy)
    assert host.mutations == before


def test_check_no_modifica_y_detecta_regla_eliminada(policy: Policy, host: FakeHost) -> None:
    guard.apply(policy)
    before = list(host.mutations)
    guard.check(policy)
    assert host.mutations == before
    guard.attest(policy, now=100, boot_id="boot-uno")
    host.chains[CHAIN].pop(1)
    with pytest.raises(guard.GuardError):
        guard.refresh(policy)
    assert not policy.status_path.exists()


def test_check_rechaza_salto_desplazado(policy: Policy, host: FakeHost) -> None:
    guard.apply(policy)
    host.chains["DOCKER-USER"].insert(0, ("-j", "RETURN"))
    with pytest.raises(guard.GuardError, match="no prioritario"):
        guard.check(policy)


def test_check_rechaza_regla_extra(policy: Policy, host: FakeHost) -> None:
    guard.apply(policy)
    host.chains[CHAIN].insert(0, ("-j", "RETURN"))
    with pytest.raises(guard.GuardError):
        guard.check(policy)


def test_red_ajena_solapada_rechazada(policy: Policy, host: FakeHost) -> None:
    host.networks[1]["IPAM"]["Config"][0]["Subnet"] = "172.31.0.0/16"
    with pytest.raises(guard.GuardError, match="solapa"):
        guard.apply(policy)
    assert host.mutations == []


@pytest.mark.parametrize("field,value", [("EnableIPv6", True), ("Internal", True)])
def test_red_insegura_rechazada(
    policy: Policy, host: FakeHost, field: str, value: bool
) -> None:
    host.networks[0][field] = value
    with pytest.raises(guard.GuardError):
        guard.apply(policy)
    assert host.mutations == []


@pytest.mark.parametrize(
    "change",
    [
        {"status": "FAIL"},
        {"network_name": "otra"},
        {"network_subnet": "172.31.0.0/16"},
        {"boot_id": "boot-viejo"},
        {"policy_hash": "0" * 64},
        {"expires_at": 100},
        {"checked_at": float("nan")},
    ],
)
def test_worker_rechaza_atestacion_alterada(
    policy: Policy, host: FakeHost, change: dict[str, object]
) -> None:
    guard.apply(policy)
    guard.attest(policy, now=100, boot_id="boot-actual")
    status = json.loads(policy.status_path.read_text(encoding="utf-8"))
    status.update(change)
    policy.status_path.write_text(json.dumps(status), encoding="utf-8")
    with pytest.raises(EgressFenceMissingError):
        verify_attestation(
            path=policy.status_path,
            expected_network=NETWORK_NAME,
            expected_subnet=str(NETWORK_SUBNET),
            expected_hash=policy.policy_hash(),
            boot_id="boot-actual",
            now=101,
        )


def test_worker_acepta_atestacion_valida(policy: Policy, host: FakeHost) -> None:
    guard.apply(policy)
    guard.attest(policy, now=100, boot_id="boot-actual")
    verify_attestation(
        path=policy.status_path,
        expected_network=NETWORK_NAME,
        expected_subnet=str(NETWORK_SUBNET),
        expected_hash=policy.policy_hash(),
        boot_id="boot-actual",
        now=101,
    )


def test_worker_rechaza_atestacion_ausente(policy: Policy) -> None:
    with pytest.raises(EgressFenceMissingError):
        verify_attestation(
            path=policy.status_path,
            expected_network=NETWORK_NAME,
            expected_subnet=str(NETWORK_SUBNET),
            expected_hash=policy.policy_hash(),
            boot_id="boot-actual",
            now=101,
        )


def test_atestacion_dura_cinco_segundos_y_check_cada_dos(policy: Policy, host: FakeHost) -> None:
    guard.apply(policy)
    guard.attest(policy, now=100, boot_id="boot-actual")
    status = json.loads(policy.status_path.read_text(encoding="utf-8"))
    assert guard.CHECK_INTERVAL_SECONDS == 2
    assert status["expires_at"] == 105
    with pytest.raises(EgressFenceMissingError):
        verify_attestation(
            path=policy.status_path,
            expected_network=NETWORK_NAME,
            expected_subnet=str(NETWORK_SUBNET),
            expected_hash=policy.policy_hash(),
            boot_id="boot-actual",
            now=105,
        )


def test_hash_cambia_con_dns_subnet_y_regla(
    policy: Policy, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = policy.policy_hash()
    assert replace(policy, dns_resolvers=tuple(reversed(APPROVED_DNS))).policy_hash() != original
    assert replace(
        policy, network_subnet=ipaddress.IPv4Network("172.31.249.0/24")
    ).policy_hash() != original
    monkeypatch.setattr(
        Policy,
        "chain_rules",
        lambda self: (("-j", "DROP"),),
    )
    assert policy.policy_hash() != original


def test_guard_rechaza_subnet_y_dns_no_aprobados(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FENIX_STRIX_NETWORK", NETWORK_NAME)
    monkeypatch.setenv("FENIX_STRIX_SUBNET", "172.31.0.0/16")
    monkeypatch.setenv("FENIX_DNS_RESOLVERS", "1.1.1.1,1.0.0.1")
    with pytest.raises(ValueError, match="/24"):
        Policy.from_env()
    monkeypatch.setenv("FENIX_STRIX_SUBNET", str(NETWORK_SUBNET))
    monkeypatch.setenv("FENIX_DNS_RESOLVERS", "172.31.250.1,1.0.0.1")
    with pytest.raises(ValueError, match="aprobados"):
        Policy.from_env()
    monkeypatch.setenv("FENIX_DNS_RESOLVERS", "1.1.1.1,1.0.0.1")
    assert Policy.from_env().policy_hash()


def test_guard_test_mode_requiere_nombre_y_subnet_privada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FENIX_GUARD_TEST_MODE", "true")
    monkeypatch.setenv("FENIX_STRIX_NETWORK", "fenix-team-strix-test")
    monkeypatch.setenv("FENIX_STRIX_SUBNET", "172.31.249.0/24")
    monkeypatch.setenv("FENIX_DNS_RESOLVERS", "1.1.1.1,1.0.0.1")
    assert str(Policy.from_env().network_subnet) == "172.31.249.0/24"
    monkeypatch.setenv("FENIX_STRIX_NETWORK", "bridge")
    with pytest.raises(ValueError):
        Policy.from_env()
    monkeypatch.setenv("FENIX_STRIX_NETWORK", "fenix-team-strix-test")
    monkeypatch.setenv("FENIX_STRIX_SUBNET", "8.8.8.0/24")
    with pytest.raises(ValueError):
        Policy.from_env()


def test_orden_de_denegacion_antes_de_https_y_dns(policy: Policy) -> None:
    rules = policy.chain_rules()
    destinations = [str(rule[1]) for rule in rules if rule[0] == "-d"]
    assert destinations[:6] == [
        "127.0.0.0/8",
        "169.254.0.0/16",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]
    assert rules[0] == ("-m", "addrtype", "--dst-type", "LOCAL", "-j", "DROP")
    assert rules[-1] == ("-j", "DROP")
    https_index = rules.index(("-p", "tcp", "--dport", "443", "-j", "RETURN"))
    assert https_index > max(
        index for index, rule in enumerate(rules) if rule[:1] == ("-d",)
    )


@pytest.mark.parametrize(
    "address",
    [
        "172.17.0.1",
        "172.19.0.8",
        "172.20.0.8",
        "172.22.0.8",
        "172.23.0.8",
        "10.0.0.8",
        "10.0.1.8",
        "100.100.100.100",
        "169.254.169.254",
        "172.31.250.1",
    ],
)
def test_todos_los_destinos_internos_caen_antes_de_https(policy: Policy, address: str) -> None:
    target = ipaddress.IPv4Address(address)
    deny_index = next(
        index
        for index, rule in enumerate(policy.chain_rules())
        if rule[:1] == ("-d",) and target in ipaddress.IPv4Network(rule[1])
    )
    https_index = policy.chain_rules().index(
        ("-p", "tcp", "--dport", "443", "-j", "RETURN")
    )
    assert deny_index < https_index
