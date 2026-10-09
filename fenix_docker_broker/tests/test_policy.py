"""Autorización del broker en ambos lados de containers/create."""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fenix_docker_broker.policy import Config, PolicyDeniedError, parse_json
from fenix_docker_broker.server import Broker, secure_socket

RUN_ID = "11111111-1111-4111-8111-111111111111"
CONTAINER_ID = "c" * 64
NETWORK_ID = "d" * 64
IMAGE_ID = "sha256:" + "e" * 64
IMAGE = "ghcr.io/usestrix/strix-sandbox:1.3.0"
NETWORK = "fenix-strix-test"
SUBNET = "172.31.240.0/24"


class FakeUpstream:
    def __init__(self, owner: PolicyTests) -> None:
        self.owner = owner
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.inspect = owner.inspect()
        self.image_id = IMAGE_ID
        self.drift_after_start = None

    def json(self, method: str, path: str) -> dict:
        self.calls.append((method, path, None))
        if "/images/" in path:
            return {"Id": self.image_id, "RepoDigests": []}
        if "/networks/" in path:
            return {
                "Id": NETWORK_ID,
                "Name": NETWORK,
                "Driver": "bridge",
                "EnableIPv6": False,
                "IPAM": {"Config": [{"Subnet": SUBNET}]},
            }
        if "/containers/" in path and path.endswith("/json"):
            return copy.deepcopy(self.inspect)
        raise AssertionError(path)

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, dict, bytes]:
        self.calls.append((method, path, body))
        if path.endswith("/version"):
            return 200, {}, b'{"ApiVersion":"1.48"}'
        if path.endswith("/_ping"):
            return 200, {}, b"OK"
        if path.endswith("/containers/create"):
            self.inspect["Config"]["Labels"] = json.loads(body or b"{}")["Labels"]
            return 201, {}, json.dumps({"Id": CONTAINER_ID}).encode()
        if path.endswith("/start"):
            self.inspect["State"] = {"Running": True}
            if self.drift_after_start:
                self.drift_after_start(self.inspect)
            return 204, {}, b""
        if path.endswith("/kill"):
            return 204, {}, b""
        if method == "DELETE":
            return 204, {}, b""
        raise AssertionError(path)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / RUN_ID).mkdir()
        (root / RUN_ID / "workspace").mkdir()
        self.config = Config(
            upstream_socket=Path("/var/run/docker.sock"),
            downstream_socket=Path("/run/fenix-docker/docker.sock"),
            image=IMAGE,
            image_id=IMAGE_ID,
            network=NETWORK,
            subnet=ipaddress.ip_network(SUBNET),
            workspace_root=root,
        )
        self.upstream = FakeUpstream(self)
        self.broker = Broker(self.config, self.upstream)

    def create_body(self) -> dict:
        return {
            "Image": IMAGE,
            "Labels": {"strix-run-id": RUN_ID},
            "HostConfig": {"NetworkMode": NETWORK, "CapAdd": ["NET_ADMIN", "NET_RAW"]},
            "NetworkingConfig": {"EndpointsConfig": {NETWORK: {}}},
            "Env": ["PYTHONUNBUFFERED=1", "http_proxy=http://127.0.0.1:48080"],
        }

    def inspect(self) -> dict:
        return {
            "Id": CONTAINER_ID,
            "Image": IMAGE_ID,
            "Config": {
                "Image": IMAGE,
                "Labels": {
                    "strix-run-id": RUN_ID,
                    "com.mindguard.fenix": "true",
                    "com.mindguard.fenix.run_id": RUN_ID,
                },
                "Env": ["PYTHONUNBUFFERED=1"],
            },
            "HostConfig": {
                "NetworkMode": NETWORK,
                "CapAdd": ["NET_ADMIN", "NET_RAW"],
                "Privileged": False,
            },
            "NetworkSettings": {
                "Networks": {NETWORK: {"NetworkID": NETWORK_ID, "IPAddress": "172.31.240.3"}}
            },
            "Mounts": [],
        }

    def route(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict, bytes]:
        raw = json.dumps(body).encode() if body is not None else b""
        return self.broker.route(method, path, raw)

    def test_ciclo_positivo(self) -> None:
        self.assertEqual(self.route("GET", "/version")[0], 200)
        self.assertEqual(self.route("GET", "/v1.48/_ping")[0], 200)
        self.assertEqual(self.route("GET", f"/v1.48/images/{IMAGE}/json")[0], 200)
        self.assertEqual(self.route("POST", "/v1.48/containers/create", self.create_body())[0], 201)
        self.assertEqual(self.route("GET", f"/v1.48/containers/{CONTAINER_ID}/json")[0], 200)
        self.assertEqual(self.route("POST", f"/v1.48/containers/{CONTAINER_ID}/start")[0], 204)
        self.assertEqual(
            self.route("DELETE", f"/v1.48/containers/{CONTAINER_ID}?v=False&link=False&force=True")[
                0
            ],
            204,
        )
        self.assertEqual(self.upstream.inspect["Config"]["Labels"]["com.mindguard.fenix"], "true")

    def test_create_rechaza_variantes_peligrosas(self) -> None:
        cases = {
            "bridge": lambda b: b["HostConfig"].update(NetworkMode="bridge"),
            "host": lambda b: b["HostConfig"].update(NetworkMode="host"),
            "red ajena": lambda b: b["NetworkingConfig"]["EndpointsConfig"].update(otra={}),
            "privileged": lambda b: b["HostConfig"].update(Privileged=True),
            "bind raíz": lambda b: b["HostConfig"].update(Binds=["/:/workspace/root"]),
            "bind socket": lambda b: b["HostConfig"].update(
                Binds=["/var/run/docker.sock:/workspace/socket"]
            ),
            "mount host": lambda b: b["HostConfig"].update(
                Mounts=[{"Type": "bind", "Source": "/etc", "Target": "/workspace/etc"}]
            ),
            "mount destino traversal": lambda b: b["HostConfig"].update(
                Mounts=[
                    {
                        "Type": "bind",
                        "Source": str(self.config.workspace_root / RUN_ID / "workspace"),
                        "Target": "/workspace/../proc",
                    }
                ]
            ),
            "dispositivo": lambda b: b["HostConfig"].update(Devices=[{"PathOnHost": "/dev/sda"}]),
            "capability": lambda b: b["HostConfig"].update(CapAdd=["SYS_ADMIN"]),
            "imagen": lambda b: b.update(Image="mindguard/backend:latest"),
            "run_id": lambda b: b.update(Labels={}),
            "label ajena": lambda b: b["Labels"].update(
                {"com.docker.compose.project": "mindguard"}
            ),
            "campo desconocido": lambda b: b["HostConfig"].update(UsernsMode="host"),
            "docker env": lambda b: b.update(Env=["DOCKER_HOST=unix:///var/run/docker.sock"]),
        }
        for name, mutation in cases.items():
            with self.subTest(name=name):
                body = self.create_body()
                mutation(body)
                with self.assertRaises(PolicyDeniedError):
                    self.route("POST", "/v1.48/containers/create", body)

    def test_image_id_distinto_no_crea(self) -> None:
        self.upstream.image_id = "sha256:" + "a" * 64
        with self.assertRaises(PolicyDeniedError):
            self.route("POST", "/v1.48/containers/create", self.create_body())
        self.assertFalse(
            any(path.endswith("/containers/create") for _, path, _ in self.upstream.calls)
        )

    def test_start_revalida_estado(self) -> None:
        cases = {
            "ajeno": lambda i: i["Config"]["Labels"].update({"com.mindguard.fenix": "false"}),
            "sin label": lambda i: i["Config"]["Labels"].pop("com.mindguard.fenix"),
            "red alterada": lambda i: i["NetworkSettings"]["Networks"].update({"bridge": {}}),
            "config alterada": lambda i: i["HostConfig"].update(Privileged=True),
            "mount alterado": lambda i: i["Mounts"].append(
                {"Type": "bind", "Source": "/etc", "Destination": "/workspace/etc"}
            ),
            "imagen alterada": lambda i: i.update(Image="sha256:" + "b" * 64),
        }
        for name, mutation in cases.items():
            with self.subTest(name=name):
                self.upstream.inspect = self.inspect()
                self.upstream.calls.clear()
                mutation(self.upstream.inspect)
                with self.assertRaises(PolicyDeniedError):
                    self.route("POST", f"/v1.48/containers/{CONTAINER_ID}/start")
                self.assertFalse(any(path.endswith("/start") for _, path, _ in self.upstream.calls))

    def test_ids_ajenos_denegados(self) -> None:
        for method, path in (
            ("GET", f"/containers/{CONTAINER_ID}/json"),
            ("POST", f"/containers/{CONTAINER_ID}/start"),
            ("DELETE", f"/containers/{CONTAINER_ID}"),
        ):
            with self.subTest(method=method):
                self.upstream.inspect = self.inspect()
                self.upstream.inspect["Config"]["Labels"] = {"com.dokploy.service": "mindguard"}
                with self.assertRaises(PolicyDeniedError):
                    self.route(method, path)

    def test_api_no_observada_denegada(self) -> None:
        for method, path in (
            ("POST", f"/containers/{CONTAINER_ID}/exec"),
            ("POST", "/networks/fenix/connect"),
            ("POST", "/images/create"),
            ("GET", "/containers/json"),
            ("GET", "/info"),
            ("POST", f"/containers/{CONTAINER_ID}/kill"),
            ("POST", f"/containers/{CONTAINER_ID}/wait"),
            ("GET", "/desconocido"),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(PolicyDeniedError):
                self.route(method, path)

    def test_json_duplicado_denegado(self) -> None:
        with self.assertRaises(PolicyDeniedError):
            parse_json(b'{"Image":"x","Image":"y"}')

    def test_inspect_real_antes_de_start(self) -> None:
        inspect = self.upstream.inspect
        inspect["HostConfig"].update(
            IpcMode="private",
            CgroupnsMode="private",
            Memory=0,
            NanoCpus=0,
            PidsLimit=0,
            PortBindings={},
        )
        inspect["NetworkSettings"]["Networks"][NETWORK]["NetworkID"] = ""
        inspect["NetworkSettings"]["Networks"][NETWORK]["IPAddress"] = ""
        def assign_network(info: dict) -> None:
            info["NetworkSettings"]["Networks"][NETWORK].update(
                NetworkID=NETWORK_ID, IPAddress="172.31.240.3"
            )

        self.upstream.drift_after_start = assign_network
        self.assertEqual(self.route("POST", f"/containers/{CONTAINER_ID}/start")[0], 204)

    def test_drift_despues_de_start_se_mata_y_elimina(self) -> None:
        cases = {
            "red": lambda i: i["NetworkSettings"]["Networks"].update(bridge={}),
            "ownership": lambda i: i["Config"]["Labels"].update(
                {"com.mindguard.fenix": "false"}
            ),
            "imagen": lambda i: i.update(Image="sha256:" + "a" * 64),
            "security": lambda i: i["HostConfig"].update(Privileged=True),
        }
        for name, drift in cases.items():
            with self.subTest(name=name):
                self.upstream.inspect = self.inspect()
                self.upstream.calls.clear()
                self.upstream.drift_after_start = drift
                with self.assertRaises(PolicyDeniedError):
                    self.route("POST", f"/containers/{CONTAINER_ID}/start")
                calls = [(method, path) for method, path, _ in self.upstream.calls]
                self.assertIn(("POST", f"/containers/{CONTAINER_ID}/kill"), calls)
                self.assertIn(("DELETE", f"/containers/{CONTAINER_ID}?force=True"), calls)

    def test_production_rechaza_tag_mutable(self) -> None:
        values = {
            "FENIX_STRIX_IMAGE_ID": IMAGE_ID,
            "FENIX_STRIX_IMAGE": IMAGE,
            "FENIX_REQUIRE_IMMUTABLE_IMAGE": "true",
            "FENIX_STRIX_NETWORK": NETWORK,
            "FENIX_STRIX_SUBNET": SUBNET,
            "FENIX_WORKSPACE_ROOT": str(self.config.workspace_root),
        }
        with patch.dict(os.environ, values), self.assertRaises(ValueError):
            Config.from_env()

    def test_immutable_image_requires_digest_present(self) -> None:
        self.broker.config = self.config.__class__(
            **{**self.config.__dict__, "immutable_image": True}
        )
        with self.assertRaises(PolicyDeniedError):
            self.route("GET", f"/images/{IMAGE}/json")

    def test_mount_valido_del_run(self) -> None:
        body = self.create_body()
        body["HostConfig"]["Mounts"] = [
            {
                "Type": "bind",
                "Source": str(self.config.workspace_root / RUN_ID / "workspace"),
                "Target": "/workspace/target",
                "ReadOnly": True,
            }
        ]
        self.assertEqual(self.route("POST", "/containers/create", body)[0], 201)

    @unittest.skipUnless(hasattr(os, "getuid"), "Socket Unix solo en Linux")
    def test_socket_privado_tiene_owner_y_modo_0600(self) -> None:
        path = Path(self.temp.name) / "broker.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            secure_socket(path)
            info = path.stat()
            self.assertEqual(info.st_uid, os.getuid())
            self.assertEqual(info.st_gid, os.getgid())
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
