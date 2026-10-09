"""Smoke local de cleanup parcial con broker real y daemon Docker, sin LLM."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import tempfile
from pathlib import Path
from uuid import uuid4

import docker
from docker.errors import NotFound

from fenix_docker_broker.policy import Config, PolicyDeniedError
from fenix_docker_broker.server import Broker, Upstream
from fenix_egress_guard.guard import attest
from fenix_egress_guard.policy import APPROVED_DNS, Policy
from scripts.strix_smoke_cleanup import SmokeCleanup

LOG = logging.getLogger(__name__)


class DockerResources:
    def __init__(self, client: docker.DockerClient, broker: Broker, run_id: str) -> None:
        self.client = client
        self.broker = broker
        self.run_id = run_id
        self.remove_failures = 0

    def inventory(self) -> dict[str, set[str]]:
        containers = self.client.containers.list(
            all=True, filters={"label": f"com.mindguard.fenix.run_id={self.run_id}"}
        )
        networks = self.client.networks.list(
            filters={"label": f"com.mindguard.fenix.smoke={self.run_id}"}
        )
        return {
            "containers": {str(item.id) for item in containers},
            "networks": {str(item.id) for item in networks},
        }

    def remove_container(self, container_id: str) -> None:
        try:
            self.client.containers.get(container_id)
        except NotFound:
            return
        if self.remove_failures:
            self.remove_failures -= 1
            raise OSError("Fallo transitorio inyectado en cleanup")
        status, _, _ = self.broker.route(
            "DELETE", f"/containers/{container_id}?v=False&link=False&force=True", b""
        )
        if status not in (204, 404):
            raise RuntimeError(f"Docker DELETE devolvió {status}")

    def remove_network(self, network_id: str) -> None:
        self.client.networks.get(network_id).remove()


def run_case(client: docker.DockerClient, image: str, scenario: str) -> dict[str, object]:
    run_id = str(uuid4())
    network_name = f"fenix-cleanup-smoke-{run_id}"
    with tempfile.TemporaryDirectory(prefix="fenix-cleanup-") as temporary:
        root = Path(temporary)
        # El broker se configura tras crear la red; el adapter inventaría por run ID.
        provisional = Config(
            upstream_socket=Path("/var/run/docker.sock"),
            downstream_socket=root / "broker.sock",
            image=image,
            image_id=client.images.get(image).id,
            network=network_name,
            subnet=ipaddress.IPv4Network("172.31.250.0/24"),
            workspace_root=root,
        )
        broker = Broker(provisional, Upstream(Path("/var/run/docker.sock")))
        resources = DockerResources(client, broker, run_id)
        scope = SmokeCleanup(resources)
        expected_failure = False
        try:
            network = client.networks.create(
                network_name,
                driver="bridge",
                enable_ipv6=False,
                options={"com.docker.network.bridge.enable_icc": "false"},
                labels={
                    "com.mindguard.fenix": "true",
                    "com.mindguard.fenix.smoke": run_id,
                },
            )
            scope.track_network(str(network.id))
            network.reload()
            subnet = ipaddress.IPv4Network(network.attrs["IPAM"]["Config"][0]["Subnet"])
            policy = Policy(network_name, subnet, APPROVED_DNS, root / "egress-status.json")
            attest(policy)
            broker.config = Config(
                **{
                    **provisional.__dict__,
                    "subnet": subnet,
                    "require_attestation": True,
                    "attestation_path": policy.status_path,
                    "policy_hash": policy.policy_hash(),
                }
            )
            request = {
                "Image": image,
                "Cmd": ["sleep", "30"],
                "Labels": {"strix-run-id": run_id},
                "HostConfig": {
                    "NetworkMode": network_name,
                    "CapAdd": ["NET_ADMIN", "NET_RAW"],
                },
            }
            status, _, payload = broker.route(
                "POST", "/containers/create", json.dumps(request).encode()
            )
            if status != 201:
                raise RuntimeError(f"Docker create devolvió {status}")
            container_id = json.loads(payload)["Id"]
            scope.track_container(container_id)
            if scenario == "start_deny":
                policy.status_path.unlink()
                try:
                    broker.route("POST", f"/containers/{container_id}/start", b"")
                except PolicyDeniedError:
                    expected_failure = True
                if not expected_failure:
                    raise AssertionError("Broker no rechazó start")
            elif scenario == "after_create":
                expected_failure = True
                raise ValueError("Fallo/parsing inyectado inmediatamente tras create")
            elif scenario in {"post_start", "transient_cleanup"}:
                start_status, _, _ = broker.route(
                    "POST", f"/containers/{container_id}/start", b""
                )
                if start_status != 204:
                    raise AssertionError(f"Docker start devolvió {start_status}")
                if scenario == "transient_cleanup":
                    resources.remove_failures = 1
                expected_failure = True
                raise AssertionError("Fallo del test posterior a start inyectado")
            else:
                raise ValueError(f"Escenario desconocido: {scenario}")
        except (ValueError, AssertionError) as error:
            if not expected_failure:
                raise
            LOG.info("Fallo esperado del escenario %s: %s", scenario, error)
        finally:
            report = scope.close()
            print(json.dumps({"SCENARIO": scenario, **report.as_dict()}, sort_keys=True))
        return report.as_dict()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    client = docker.from_env()
    try:
        for scenario in ("start_deny", "after_create", "post_start", "transient_cleanup"):
            result = run_case(client, args.image, scenario)
            if result["CLEANUP"] != "PASS":
                raise RuntimeError(f"Smoke {scenario} dejó huérfanos")
    finally:
        client.close()


if __name__ == "__main__":
    main()
