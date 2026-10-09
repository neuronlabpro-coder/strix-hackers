"""Servidor HTTP sobre socket Unix con una superficie Docker cerrada."""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import socketserver
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from fenix_docker_broker.policy import (
    Config,
    PolicyDeniedError,
    parse_json,
    validate_create,
    validate_identity,
    validate_network,
    validate_start,
)

LOG = logging.getLogger("fenix_docker_broker")
MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
CONTAINER_PATH = re.compile(r"^/containers/([0-9a-f]{64})(?:/(json|start))?$")
VERSION_PATH = re.compile(r"^/v[0-9]+\.[0-9]+(?=/)")


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost", timeout=10)
        self.socket_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.socket_path))


class Upstream:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def request(
        self, method: str, path: str, body: bytes | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        connection = UnixHTTPConnection(self.socket_path)
        try:
            headers = {"Host": "docker", "Connection": "close"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(MAX_RESPONSE + 1)
            if len(payload) > MAX_RESPONSE:
                raise PolicyDeniedError("Respuesta Docker demasiado grande")
            return (
                response.status,
                {key.lower(): value for key, value in response.getheaders()},
                payload,
            )
        finally:
            connection.close()

    def json(self, method: str, path: str) -> dict[str, Any]:
        status, _, payload = self.request(method, path)
        if status != 200:
            raise PolicyDeniedError("Recurso Docker no verificable")
        return parse_json(payload)


class Broker:
    def __init__(self, config: Config, upstream: Upstream) -> None:
        self.config = config
        self.upstream = upstream

    def _image(self, prefix: str) -> dict[str, Any]:
        path = f"{prefix}/images/{quote(self.config.image, safe='/:@')}/json"
        info = self.upstream.json("GET", path)
        if info.get("Id") != self.config.image_id:
            raise PolicyDeniedError("La imagen Strix no resuelve al image ID autorizado")
        if self.config.immutable_image and self.config.image not in info.get("RepoDigests", []):
            raise PolicyDeniedError("El digest Strix no está presente en la imagen local")
        return info

    def _network_id(self, prefix: str) -> str:
        path = f"{prefix}/networks/{quote(self.config.network, safe='')}"
        return validate_network(self.upstream.json("GET", path), self.config)

    def _container(self, prefix: str, container_id: str) -> dict[str, Any]:
        info = self.upstream.json("GET", f"{prefix}/containers/{container_id}/json")
        if info.get("Id") != container_id:
            raise PolicyDeniedError("Container ID incoherente")
        validate_identity(info, self.config)
        return info

    def route(self, method: str, raw_path: str, body: bytes) -> tuple[int, dict[str, str], bytes]:
        split = urlsplit(raw_path)
        if split.fragment or not split.path.startswith("/") or "//" in split.path:
            raise PolicyDeniedError("Ruta Docker inválida")
        match = VERSION_PATH.match(split.path)
        prefix = match.group() if match else ""
        path = unquote(split.path[len(prefix) :])
        if not path.startswith("/") or ".." in path or "\x00" in path:
            raise PolicyDeniedError("Ruta Docker inválida")
        if method == "GET" and path in {"/version", "/_ping"} and not split.query and not body:
            return self.upstream.request(method, raw_path)
        if (
            method == "GET"
            and path.startswith("/images/")
            and path.endswith("/json")
            and not split.query
            and not body
        ):
            image = path[len("/images/") : -len("/json")]
            if image != self.config.image:
                raise PolicyDeniedError("Imagen ajena")
            info = self._image(prefix)
            return 200, {"content-type": "application/json"}, json.dumps(info).encode()
        if method == "POST" and path == "/containers/create" and not split.query:
            data, _ = validate_create(parse_json(body), self.config)
            self._image(prefix)
            self._network_id(prefix)
            payload = json.dumps(data, separators=(",", ":")).encode()
            status, headers, response = self.upstream.request(method, raw_path, payload)
            if status != 201:
                return status, headers, response
            created = parse_json(response).get("Id")
            if not isinstance(created, str) or not re.fullmatch(r"[0-9a-f]{64}", created):
                raise PolicyDeniedError("Docker devolvió un ID inválido")
            try:
                self._container(prefix, created)
            except PolicyDeniedError:
                self.upstream.request("DELETE", f"{prefix}/containers/{created}?force=True")
                raise
            return status, headers, response
        container_match = CONTAINER_PATH.fullmatch(path)
        if container_match:
            container_id, suffix = container_match.groups()
            if method == "GET" and suffix == "json" and not split.query and not body:
                info = self._container(prefix, container_id)
                return 200, {"content-type": "application/json"}, json.dumps(info).encode()
            if method == "POST" and suffix == "start" and not split.query and not body:
                info = self._container(prefix, container_id)
                self._image(prefix)
                validate_start(info, self.config, self._network_id(prefix))
                result = self.upstream.request(method, raw_path)
                if result[0] != 204:
                    return result
                try:
                    started = self._container(prefix, container_id)
                    self._image(prefix)
                    validate_start(started, self.config, self._network_id(prefix), started=True)
                except (PolicyDeniedError, OSError, http.client.HTTPException):
                    for cleanup_method, cleanup_path in (
                        ("POST", f"{prefix}/containers/{container_id}/kill"),
                        ("DELETE", f"{prefix}/containers/{container_id}?force=True"),
                    ):
                        try:
                            cleanup_status, _, _ = self.upstream.request(
                                cleanup_method, cleanup_path
                            )
                            if cleanup_status >= 400:
                                LOG.error(
                                    "Limpieza Docker %s devolvió %s",
                                    cleanup_method,
                                    cleanup_status,
                                )
                        except (OSError, http.client.HTTPException) as error:
                            LOG.error("Limpieza Docker %s falló: %s", cleanup_method, error)
                    raise
                return result
            if method == "DELETE" and suffix is None and not body:
                query = parse_qs(split.query, strict_parsing=True)
                if query and query != {"v": ["False"], "link": ["False"], "force": ["True"]}:
                    raise PolicyDeniedError("Opciones de eliminación no autorizadas")
                self._container(prefix, container_id)
                return self.upstream.request(method, raw_path)
        raise PolicyDeniedError("Operación Docker no autorizada")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    broker: Broker

    def log_message(self, format_string: str, *args: object) -> None:
        LOG.info(format_string, *args)

    def _reply(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", headers.get("content-type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _handle(self) -> None:
        try:
            if (
                self.headers.get("Transfer-Encoding")
                or len(self.headers.get_all("Content-Length", [])) > 1
            ):
                raise PolicyDeniedError("Encabezados HTTP ambiguos")
            length = self.headers.get("Content-Length", "0")
            if not length.isdecimal() or int(length) > MAX_REQUEST:
                raise PolicyDeniedError("Tamaño de petición inválido")
            body = self.rfile.read(int(length))
            status, headers, response = self.broker.route(self.command, self.path, body)
            self._reply(status, headers, response)
        except (PolicyDeniedError, ValueError) as error:
            LOG.warning("DENY %s %s: %s", self.command, self.path, error)
            self._reply(403, {}, json.dumps({"message": str(error)}).encode())
        except (OSError, http.client.HTTPException) as error:
            LOG.error("Docker upstream falló: %s", error)
            self._reply(502, {}, b'{"message":"Docker upstream no disponible"}')

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def secure_socket(path: Path) -> None:
    path.chmod(0o600)
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_gid != os.getgid() or stat.st_mode & 0o777 != 0o600:
        raise RuntimeError("Permisos del socket broker inseguros")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()
    path = config.downstream_socket
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise RuntimeError("Directorio del socket broker no válido")
    if path.exists() or path.is_symlink():
        if not path.is_socket() or path.stat().st_uid != os.getuid():
            raise RuntimeError("Ruta del socket broker ocupada")
        path.unlink()
    Handler.broker = Broker(config, Upstream(config.upstream_socket))
    with UnixServer(str(path), Handler) as server:
        secure_socket(path)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
