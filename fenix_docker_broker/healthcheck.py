"""Comprueba el socket privado y el upstream sin abrir puertos TCP."""

from fenix_docker_broker.policy import Config
from fenix_docker_broker.server import UnixHTTPConnection


def main() -> None:
    connection = UnixHTTPConnection(Config.from_env().downstream_socket)
    try:
        connection.request("GET", "/_ping")
        response = connection.getresponse()
        if response.status != 200 or response.read() != b"OK":
            raise SystemExit(1)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
