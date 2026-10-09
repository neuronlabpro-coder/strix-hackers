"""Política de Docker para los sandboxes efímeros de Fenix Team."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID


class PolicyDeniedError(ValueError):
    """La petición no cumple la política."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Falta {name}")
    return value


@dataclass(frozen=True)
class Config:
    upstream_socket: Path
    downstream_socket: Path
    image: str
    image_id: str
    network: str
    subnet: ipaddress.IPv4Network
    workspace_root: Path
    immutable_image: bool = False

    @classmethod
    def from_env(cls) -> Config:
        image_id = _required("FENIX_STRIX_IMAGE_ID")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("FENIX_STRIX_IMAGE_ID debe ser un image ID sha256")
        subnet = ipaddress.ip_network(_required("FENIX_STRIX_SUBNET"), strict=True)
        if not isinstance(subnet, ipaddress.IPv4Network):
            raise ValueError("FENIX_STRIX_SUBNET debe ser IPv4")
        network = _required("FENIX_STRIX_NETWORK")
        if network in {"bridge", "host", "none"} or not re.fullmatch(r"[a-zA-Z0-9_.-]+", network):
            raise ValueError("FENIX_STRIX_NETWORK no es una red dedicada válida")
        immutable_image = os.environ.get("FENIX_REQUIRE_IMMUTABLE_IMAGE") == "true"
        image = _required("FENIX_STRIX_IMAGE")
        if immutable_image and not re.fullmatch(
            r"ghcr\.io/usestrix/strix-sandbox@sha256:[0-9a-f]{64}", image
        ):
            raise ValueError("Producción requiere FENIX_STRIX_IMAGE por digest sha256")
        return cls(
            upstream_socket=Path(os.environ.get("FENIX_DOCKER_UPSTREAM", "/var/run/docker.sock")),
            downstream_socket=Path(
                os.environ.get("FENIX_DOCKER_SOCKET", "/run/fenix-docker/docker.sock")
            ),
            image=image,
            image_id=image_id,
            network=network,
            subnet=subnet,
            workspace_root=Path(_required("FENIX_WORKSPACE_ROOT")),
            immutable_image=immutable_image,
        )


def parse_json(raw: bytes) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PolicyDeniedError("Clave JSON duplicada")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PolicyDeniedError("JSON inválido") from error
    if not isinstance(value, dict):
        raise PolicyDeniedError("El cuerpo debe ser un objeto JSON")
    return value


def _keys(value: dict[str, Any], allowed: set[str]) -> None:
    if set(value) - allowed:
        raise PolicyDeniedError("Campo Docker no autorizado")


def _run_id(labels: Any) -> str:
    if not isinstance(labels, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
    ):
        raise PolicyDeniedError("Labels inválidas")
    raw = labels.get("strix-run-id", "")
    try:
        parsed = UUID(raw)
    except (ValueError, TypeError, AttributeError) as error:
        raise PolicyDeniedError("strix-run-id debe ser un UUID") from error
    if str(parsed) != raw:
        raise PolicyDeniedError("strix-run-id no es canónico")
    if labels.get("com.mindguard.fenix") not in (None, "true"):
        raise PolicyDeniedError("Label de propiedad contradictoria")
    if labels.get("com.mindguard.fenix.run_id") not in (None, raw):
        raise PolicyDeniedError("Label de run contradictoria")
    return raw


def _safe_mount(source: str, target: str, run_id: str, root: Path) -> None:
    if not source.startswith("/") or not target.startswith("/workspace/"):
        raise PolicyDeniedError("Bind fuera del workspace")
    if any(part in {".", ".."} for part in target.split("/")):
        raise PolicyDeniedError("Destino de bind ambiguo")
    parts = Path(source).parts
    if ".." in parts or "." in parts:
        raise PolicyDeniedError("Ruta de bind ambigua")
    run_root = root / run_id
    path = Path(source)
    try:
        if not path.is_relative_to(run_root) or path == run_root:
            raise PolicyDeniedError("Bind de host fuera del run")
        if not run_root.is_dir() or not path.exists():
            raise PolicyDeniedError("Bind de host inexistente")
        if not path.resolve(strict=True).is_relative_to(run_root.resolve(strict=True)):
            raise PolicyDeniedError("Bind con enlace fuera del run")
        for parent in (path, *path.parents):
            if parent == run_root.parent:
                break
            if parent.is_symlink():
                raise PolicyDeniedError("Bind con enlace simbólico")
    except OSError as error:
        raise PolicyDeniedError("No se pudo verificar el bind") from error


def _mounts(host: dict[str, Any], run_id: str, root: Path) -> None:
    if host.get("Binds") or host.get("VolumesFrom") or host.get("Tmpfs"):
        raise PolicyDeniedError("Bind o volumen no autorizado")
    mounts = host.get("Mounts", [])
    if not isinstance(mounts, list):
        raise PolicyDeniedError("Mounts inválidos")
    for mount in mounts:
        if not isinstance(mount, dict):
            raise PolicyDeniedError("Mount inválido")
        _keys(mount, {"Type", "Source", "Target", "ReadOnly", "BindOptions"})
        if (
            mount.get("Type") != "bind"
            or not isinstance(mount.get("Source"), str)
            or not isinstance(mount.get("Target"), str)
        ):
            raise PolicyDeniedError("Solo se permiten bind mounts del workspace")
        if mount.get("BindOptions") not in (None, {}, {"Propagation": "rprivate"}):
            raise PolicyDeniedError("Opciones de bind no autorizadas")
        _safe_mount(mount["Source"], mount["Target"], run_id, root)


def _host_config(host: Any, config: Config, run_id: str, *, inspected: bool = False) -> None:
    if not isinstance(host, dict):
        raise PolicyDeniedError("HostConfig ausente")
    _keys(
        host,
        {
            "NetworkMode",
            "CapAdd",
            "CapDrop",
            "ExtraHosts",
            "LogConfig",
            "Mounts",
            "Binds",
            "PortBindings",
            "Memory",
            "MemorySwap",
            "NanoCpus",
            "PidsLimit",
            "ShmSize",
            "Privileged",
            "PidMode",
            "IpcMode",
            "UTSMode",
            "Devices",
            "SecurityOpt",
            "ReadonlyRootfs",
            "AutoRemove",
            "VolumesFrom",
            "Tmpfs",
        },
    )
    if host.get("NetworkMode") != config.network:
        raise PolicyDeniedError("NetworkMode fuera de la red Fenix")
    if host.get("Privileged") not in (None, False):
        raise PolicyDeniedError("Privileged prohibido")
    if any(host.get(name) for name in ("PidMode", "UTSMode", "Devices", "SecurityOpt")) or host.get(
        "IpcMode"
    ) not in (None, "", "private"):
        raise PolicyDeniedError("Namespace, dispositivo o SecurityOpt prohibido")
    if host.get("CapAdd", []) != ["NET_ADMIN", "NET_RAW"]:
        raise PolicyDeniedError("CapAdd no autorizadas")
    if host.get("CapDrop") not in (None, [], ["ALL"]):
        raise PolicyDeniedError("CapDrop inválidas")
    if host.get("ExtraHosts") not in (None, [], ["host.docker.internal:host-gateway"]):
        raise PolicyDeniedError("ExtraHosts no autorizados")
    if host.get("AutoRemove") not in (None, False):
        raise PolicyDeniedError("AutoRemove no autorizado")
    if host.get("ReadonlyRootfs") not in (None, False, True):
        raise PolicyDeniedError("ReadonlyRootfs inválido")
    bindings = host.get("PortBindings", {})
    if bindings not in (None, {}) and bindings != {
        "48080/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]
    }:
        raise PolicyDeniedError("PortBindings no autorizados")
    log = host.get("LogConfig", {})
    if log not in (None, {}) and (
        not isinstance(log, dict) or log.get("Type") != "json-file" or set(log) - {"Type", "Config"}
    ):
        raise PolicyDeniedError("LogConfig no autorizado")
    for key in ("Memory", "MemorySwap", "NanoCpus", "PidsLimit", "ShmSize"):
        if key in host and (
            type(host[key]) is not int or host[key] < 0 or (host[key] == 0 and not inspected)
        ):
            raise PolicyDeniedError("Límite de recursos inválido")
    _mounts(host, run_id, config.workspace_root)


def validate_create(body: dict[str, Any], config: Config) -> tuple[dict[str, Any], str]:
    _keys(
        body,
        {
            "Tty",
            "OpenStdin",
            "StdinOnce",
            "AttachStdin",
            "AttachStdout",
            "AttachStderr",
            "Cmd",
            "Image",
            "NetworkDisabled",
            "HostConfig",
            "Labels",
            "Env",
            "NetworkingConfig",
            "ExposedPorts",
            "WorkingDir",
            "User",
            "StopSignal",
        },
    )
    if body.get("Image") != config.image:
        raise PolicyDeniedError("Imagen no autorizada")
    labels = body.get("Labels")
    run_id = _run_id(labels)
    if set(labels) - {
        "strix-run-id",
        "strix-run-type",
        "com.mindguard.fenix",
        "com.mindguard.fenix.run_id",
    }:
        raise PolicyDeniedError("Label no autorizada")
    if body.get("NetworkDisabled") not in (None, False):
        raise PolicyDeniedError("NetworkDisabled inválido")
    endpoints = body.get("NetworkingConfig")
    if endpoints not in (None, {}):
        if not isinstance(endpoints, dict):
            raise PolicyDeniedError("NetworkingConfig inválida")
        # docker-py 7 envía {"nombre": null}; la API cruda puede enviar
        # {"EndpointsConfig": {"nombre": {}}}. Ambas formas se validan.
        entries = (
            endpoints.get("EndpointsConfig") if set(endpoints) == {"EndpointsConfig"} else endpoints
        )
        if not isinstance(entries, dict) or set(entries) != {config.network}:
            raise PolicyDeniedError("Endpoint de red ajena")
        if entries[config.network] not in ({}, None):
            raise PolicyDeniedError("Opciones de endpoint no autorizadas")
    _host_config(body.get("HostConfig"), config, run_id)
    env = body.get("Env", [])
    if not isinstance(env, list) or not all(isinstance(item, str) and "=" in item for item in env):
        raise PolicyDeniedError("Env inválido")
    if any(
        item.split("=", 1)[0]
        in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"}
        for item in env
    ):
        raise PolicyDeniedError("Transporte Docker heredado")
    if body.get("ExposedPorts") not in (None, {}, {"48080/tcp": {}}):
        raise PolicyDeniedError("Puerto expuesto no autorizado")
    for key in ("Tty", "OpenStdin", "StdinOnce", "AttachStdin", "AttachStdout", "AttachStderr"):
        if key in body and type(body[key]) is not bool:
            raise PolicyDeniedError("Flag Docker inválido")
    body["Labels"] = dict(labels)
    body["Labels"]["com.mindguard.fenix"] = "true"
    body["Labels"]["com.mindguard.fenix.run_id"] = run_id
    return body, run_id


def validate_identity(inspect: dict[str, Any], config: Config) -> str:
    labels = inspect.get("Config", {}).get("Labels")
    run_id = _run_id(labels)
    if (
        labels.get("com.mindguard.fenix") != "true"
        or labels.get("com.mindguard.fenix.run_id") != run_id
    ):
        raise PolicyDeniedError("Container ajeno a Fenix")
    if (
        inspect.get("Image") != config.image_id
        or inspect.get("Config", {}).get("Image") != config.image
    ):
        raise PolicyDeniedError("Imagen del container distinta")
    return run_id


def validate_start(
    inspect: dict[str, Any], config: Config, network_id: str, *, started: bool = False
) -> None:
    run_id = validate_identity(inspect, config)
    if started and not inspect.get("State", {}).get("Running"):
        raise PolicyDeniedError("Sandbox no está ejecutándose tras start")
    host = inspect.get("HostConfig")
    if not isinstance(host, dict):
        raise PolicyDeniedError("HostConfig no inspeccionable")
    security = {
        key: host.get(key)
        for key in (
            "NetworkMode",
            "CapAdd",
            "CapDrop",
            "ExtraHosts",
            "LogConfig",
            "Mounts",
            "Binds",
            "PortBindings",
            "Memory",
            "MemorySwap",
            "NanoCpus",
            "PidsLimit",
            "ShmSize",
            "Privileged",
            "PidMode",
            "IpcMode",
            "UTSMode",
            "Devices",
            "SecurityOpt",
            "ReadonlyRootfs",
            "AutoRemove",
            "VolumesFrom",
            "Tmpfs",
        )
        if host.get(key) not in (None, [], {}, "")
    }
    _host_config(security, config, run_id, inspected=True)
    if host.get("CgroupnsMode") not in (None, "", "private"):
        raise PolicyDeniedError("CgroupnsMode no autorizado")
    for key in ("UsernsMode", "DeviceRequests", "Links", "OomKillDisable"):
        if host.get(key):
            raise PolicyDeniedError("Configuración de host no autorizada")
    networks = inspect.get("NetworkSettings", {}).get("Networks")
    if not isinstance(networks, dict) or set(networks) != {config.network}:
        raise PolicyDeniedError("Red real fuera de Fenix")
    endpoint = networks[config.network]
    allowed_ids = (network_id,) if started else ("", network_id)
    if not isinstance(endpoint, dict) or endpoint.get("NetworkID") not in allowed_ids:
        raise PolicyDeniedError("Network ID diferente")
    ip = endpoint.get("IPAddress")
    if started and not ip:
        raise PolicyDeniedError("Sandbox iniciado sin IP Fenix")
    if ip:
        if ipaddress.ip_address(ip) not in config.subnet:
            raise PolicyDeniedError("IP fuera de la subnet Fenix")
    if endpoint.get("GlobalIPv6Address"):
        raise PolicyDeniedError("IPv6 no autorizado")
    for mount in inspect.get("Mounts", []):
        if not isinstance(mount, dict) or mount.get("Type") != "bind":
            raise PolicyDeniedError("Mount real no autorizado")
        _safe_mount(
            mount.get("Source", ""), mount.get("Destination", ""), run_id, config.workspace_root
        )
    env = inspect.get("Config", {}).get("Env") or []
    if any(
        isinstance(item, str)
        and item.split("=", 1)[0]
        in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"}
        for item in env
    ):
        raise PolicyDeniedError("Docker heredado al sandbox")


def validate_network(info: dict[str, Any], config: Config) -> str:
    if (
        info.get("Name") != config.network
        or info.get("Driver") != "bridge"
        or info.get("EnableIPv6")
    ):
        raise PolicyDeniedError("Red Fenix no coincide")
    cidrs = info.get("IPAM", {}).get("Config", [])
    if len(cidrs) != 1 or cidrs[0].get("Subnet") != str(config.subnet):
        raise PolicyDeniedError("Subnet Fenix no coincide")
    network_id = info.get("Id")
    if not isinstance(network_id, str) or not re.fullmatch(r"[0-9a-f]{64}", network_id):
        raise PolicyDeniedError("Network ID inválido")
    return network_id
