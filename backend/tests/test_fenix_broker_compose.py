"""El worker no recibe el socket host ni secretos del backend en el broker."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _service(name: str) -> str:
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    lines = compose.splitlines()
    start = lines.index(f"  {name}:")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("  ")
            and not lines[index].startswith("    ")
            and lines[index].endswith(":")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_worker_solo_monta_socket_privado() -> None:
    worker = _service("celery_worker")
    assert "/var/run/docker.sock" not in worker
    assert "unix:///run/fenix-docker/docker.sock" in worker
    assert "fenix_docker_broker_socket:/run/fenix-docker:ro" in worker
    assert "/run/mindguard-fenix:/run/mindguard-fenix:ro" in worker
    assert "FENIX_EGRESS_POLICY_HASH:" in worker
    assert "STRIX_NETWORK_POOL: ${FENIX_STRIX_SUBNET:?" in worker
    assert "group_add:" not in worker
    assert "STRIX_IMAGE: ${FENIX_STRIX_IMAGE:?" in worker
    for name in ("backend", "frontend", "celery_beat"):
        service = _service(name)
        assert "fenix_docker_broker_socket" not in service
        assert "/var/run/docker.sock" not in service
        assert "/run/mindguard-fenix" not in service
        assert "fenix-team-strix-prod" not in service


def test_solo_broker_monta_socket_host() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    broker = _service("fenix-docker-broker")
    assert compose.count("/var/run/docker.sock:/var/run/docker.sock:ro") == 1
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in broker
    assert "fenix_docker_broker_socket:/run/fenix-docker\n" in broker
    assert "network_mode: none" in broker
    assert "read_only: true" in broker
    assert "cap_drop:\n      - ALL" in broker
    assert "no-new-privileges:true" in broker
    assert "mem_limit: 128m" in broker
    assert "cpus: 0.5" in broker
    assert "pids_limit: 64" in broker
    assert "env_file:" not in broker
    assert 'FENIX_REQUIRE_IMMUTABLE_IMAGE: "true"' in broker
    assert 'FENIX_REQUIRE_PROD_NETWORK: "true"' in broker
    assert 'FENIX_REQUIRE_EGRESS_ATTESTATION: "true"' in broker
    assert "FENIX_DNS_RESOLVERS:" in broker
    assert "FENIX_EGRESS_POLICY_HASH:" in broker
    assert "/run/mindguard-fenix:/run/mindguard-fenix:ro" in broker
    assert "FENIX_STRIX_IMAGE_ID:" in broker
    assert "FENIX_STRIX_SUBNET:" in broker
