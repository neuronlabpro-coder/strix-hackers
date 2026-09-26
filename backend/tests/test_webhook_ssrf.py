"""Pruebas de la guarda anti-SSRF.

Es la pieza más importante de este bloque. Un webhook saliente convierte la API en un
scáner de red si no está protegido: la plataforma tiene salida a la red interna, y cualquiera
con permiso de crear un endpoint podría usarla para leer los metadatos de la instancia o
la base de datos.

La batería cubre los tres caminos por los que se intenta llegar a la red interna:

1. **IP literal.** El atacante escribe la dirección directamente.
2. **Nombre.** El atacante registra un nombre público que resuelve a una dirección interna.
3. **Redirección.** El receptor público responde `302` a una URL interna.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from backend.core.ssrf import (
    BLOCKED_HOSTNAMES,
    MAX_RESPONSE_BODY_CHARS,
    DnsResolutionError,
    SsrfBlockedError,
    ensure_delivery_url_allowed,
    is_redirect,
    resolve_and_validate,
    truncate_response,
)

#: Direcciones que tienen que quedar fuera, cada una por un motivo distinto. No es una
#: lista decorativa: cada entrada es una técnica distinta de bypass.
BLOQUEADAS_POR_LITERAL = [
    "127.0.0.1",
    "127.1.2.3",  # 127/8 entero, no solo 127.0.0.1
    "10.0.0.1",
    "10.255.255.254",
    "172.16.0.1",
    "172.31.255.254",  # 172.16/12 entero
    "192.168.1.1",
    "169.254.169.254",  # metadatos de AWS y GCP: el objetivo principal
    "169.254.170.2",  # metadatos de tarea de ECS
    "0.0.0.0",  # noqa: S104 - es justo la direccion que se quiere rechazar
    "::1",
    "fc00::1",  # ULA, el equivalente IPv6 de las redes privadas
    "fd00::1",
    "::ffff:127.0.0.1",  # IPv4 mapeado en IPv6: el bypass clasico
    "::ffff:169.254.169.254",  # metadatos alcanzados por el mismo bypass
    "100.100.100.200",  # metadatos de Alibaba
    "192.0.0.192",  # metadatos de Oracle
]


@pytest.mark.parametrize("direccion", BLOQUEADAS_POR_LITERAL)
def test_una_ip_literal_bloqueada_se_rechaza(direccion: str) -> None:
    # Las IPv6 literales van entre corchetes en una URL, que es la forma correcta y la
    # que hace `urlsplit` devolver `::1` como host y no `::` con puerto `1`.
    envoltura = f"[{direccion}]" if ":" in direccion else direccion
    with pytest.raises(SsrfBlockedError):
        resolve_and_validate(f"https://{envoltura}/hook")


def test_una_ipv6_sin_corchetes_se_rechaza_como_ambigua() -> None:
    """`https://fc00::1/` se parsea como host `fc00` con puerto `:1`.

    No es un bypass —el nombre acabaria en el DNS y fallaria— pero deja sin decidir si lo
    que se escribio era una direccion o un dominio. Un validador de seguridad no puede
    dejar esa ambiguedad: se rechaza.
    """

    with patch("backend.core.ssrf.socket.getaddrinfo") as resolver:
        with pytest.raises(SsrfBlockedError, match="corchetes"):
            resolve_and_validate("https://fc00::1/hook")
    resolver.assert_not_called()


@pytest.mark.parametrize("nombre", sorted(BLOCKED_HOSTNAMES))
def test_los_nombres_reservados_no_llegan_a_resolverse(nombre: str) -> None:
    """Se rechazan sin intentar el DNS.

    Resolverlos ya es un comportamiento de red innecesario, y algunos barreñan una red
    entera. La prueba comprueba que no se llama a `getaddrinfo`, no solo que falla.
    """

    with patch("backend.core.ssrf.socket.getaddrinfo") as resolver:
        with pytest.raises(SsrfBlockedError):
            resolve_and_validate(f"https://{nombre}/hook")
    resolver.assert_not_called()


def test_un_nombre_que_resuelve_a_ip_privada_se_rechaza() -> None:
    """El caso que un filtro de sintaxis no pilla.

    La URL es perfectamente válida, el esquema es el correcto y el nombre parece normal.
    Lo único que la delata es lo que devuelve el DNS, y por eso la validación tiene que
    happenerse **después** de resolver, no antes.
    """

    with patch(
        "backend.core.ssrf.socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0)),
        ],
    ):
        with pytest.raises(SsrfBlockedError) as error:
            resolve_and_validate("https://mi-webhook.example.com/hook")

    detalle = str(error.value)
    assert "10.1.2.3" in detalle, "el mensaje debe nombrar la direccion que lo delata"
    assert "mi-webhook.example.com" in detalle


def test_una_ip_entre_varias_que_resuelve_a_privada_bloquea_el_host() -> None:
    """Un nombre con varias entradas A puede devolver una pública y otra privada.

    Aceptarlo porque la primera es válida deja la decisión al cliente, que es el que puede
    acabar eligiendo la interna. Se rechaza si **alguna** lo es, que es la única lectura
    segura cuando no se controla cuál se usa.
    """

    with patch(
        "backend.core.ssrf.socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.5", 0)),
        ],
    ):
        with pytest.raises(SsrfBlockedError):
            resolve_and_validate("https://dual.example.com/hook")


def test_un_nombre_publico_valido_devuelve_sus_direcciones() -> None:
    with patch(
        "backend.core.ssrf.socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ],
    ):
        direcciones = resolve_and_validate("https://hooks.example.com/entrada")

    assert direcciones == ["93.184.216.34"]


def test_un_nombre_que_no_resuelve_es_error_distinto_del_bloqueo() -> None:
    """Un nombre inexistente es un error de tecleo, y merece un `422` distinto.

    Confundir los dos hace que el usuario revise la sintaxis de una URL correcta en lugar
    de mirar si el dominio existe.
    """

    with patch(
        "backend.core.ssrf.socket.getaddrinfo", side_effect=socket.gaierror("no such host")
    ):
        with pytest.raises(DnsResolutionError):
            resolve_and_validate("https://no-existe.example.invalid/hook")


def test_un_nombre_que_no_resuelve_a_nada_es_error_de_resolucion() -> None:
    with patch("backend.core.ssrf.socket.getaddrinfo", return_value=[]):
        with pytest.raises(DnsResolutionError):
            resolve_and_validate("https://vacio.example.com/hook")


def test_una_url_sin_host_se_rechaza() -> None:
    with pytest.raises(SsrfBlockedError, match="host"):
        resolve_and_validate("https:///sin-host")


def test_un_esquema_distinto_de_http_se_rechaza() -> None:
    for url in (
        "file:///etc/passwd",
        "gopher://127.0.0.1:11211/",
        "ftp://example.com/hook",
        "data:text/plain,hola",
    ):
        with pytest.raises(SsrfBlockedError, match="http"):
            resolve_and_validate(url)


def _entorno(valor: str) -> Any:
    """Sustituye el `settings` del modulo con uno que solo declara el entorno.

    Se reemplaza el objeto entero y no el atributo con `patch.object`: `Settings` tiene
    validadores que comprueban la coherencia entre `environment` y otras variables, y
    cambiar solo el entorno dispara esa comprobacion entera y falla por motivos que no
    tienen que ver con lo que se esta probando.
    """


    return SimpleNamespace(environment=valor)


def test_http_en_produccion_se_rechaza() -> None:
    """El cifrado no es negociable fuera de local.

    Un webhook en http:// entrega la firma y el payload en claro por la red, y la firma
    sirve para **reproducir** entregas. Es el mismo motivo por el que el cliente de Git
    exige `https` en staging y producción.
    """

    with patch("backend.core.ssrf.settings", _entorno("production")):
        with pytest.raises(SsrfBlockedError, match="cifrado"):
            resolve_and_validate("http://hooks.example.com/hook")


def test_http_en_desarrollo_se_admite() -> None:
    """En local sí, porque el receptor de pruebas casi siempre va por http.

    Sin esto, probar la firma de un webhook en la máquina del desarrollador exigiría un
    certificado, y la consecuencia sería no probarlo nunca.
    """

    with patch("backend.core.ssrf.settings", _entorno("development")):
        with patch(
            "backend.core.ssrf.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
            ],
        ):
            assert resolve_and_validate("http://localhost.dev/hook") == ["93.184.216.34"]


def test_ensure_delivery_url_allowed_revalida_en_el_momento_del_envio() -> None:
    """La comprobación existe para el envío, no solo para el registro.

    Es la razón por la que `ensure_delivery_url_allowed` es una función aparte del
    registro: una URL aceptada hace meses puede haber cambiado de DNS, y si solo se
    valida al registrarse, la plataforma estaría haciendo peticiones a un destino que nadie
    revisó.
    """

    # El DNS se resuelve de verdad: se comprueba que la guarda **llama** al resolutor en
    # el momento del envío, y no que se apoye en un nombre que casualmente no existe.
    with patch(
        "backend.core.ssrf.socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 0))
        ],
    ) as resolver:
        with pytest.raises(SsrfBlockedError, match=r"10\.0\.0\.9"):
            ensure_delivery_url_allowed("https://hooks.example.com/hook")
    resolver.assert_called_once()


def test_el_cuerpo_se_recorta_con_marcador() -> None:
    """Se marca el recorte para que un cuerpo incompleto no se lea como completo.

    Sin el marcador, el usuario busca en el JSON un mensaje que se comió el límite y no
    encuentra nada, sin ninguna pista de por qué.
    """

    largo = "x" * (MAX_RESPONSE_BODY_CHARS + 500)
    recortado = truncate_response(largo)

    assert len(recortado) < len(largo)
    assert recortado.endswith("[truncado]")
    assert recortado.startswith("x" * 10)


def test_un_cuerpo_corto_no_se_toca() -> None:
    assert truncate_response("respuesta corta") == "respuesta corta"


def test_un_cuerpo_justo_en_el_limite_no_se_marca() -> None:
    exacto = "y" * MAX_RESPONSE_BODY_CHARS
    assert truncate_response(exacto) == exacto


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        (200, False),
        (204, False),
        (301, True),
        (302, True),
        (307, True),
        (308, True),
        (400, False),
        (500, False),
    ],
)
def test_las_redirecciones_se_reconocen(codigo: int, esperado: bool) -> None:
    assert is_redirect(codigo) is esperado


def test_una_url_con_credenciales_en_el_usuario_se_rechaza() -> None:
    """`https://usuario:clave@host/` es un patrón de exfiltración clásico.

    El usuario de la URL acaba en los logs del receptor y en su propio historial. No es un
    destino válido para un webhook.
    """

    with pytest.raises((SsrfBlockedError, DnsResolutionError)):
        resolve_and_validate("https://usuario:clave@hooks.example.com/hook")


def test_el_mensaje_de_bloqueo_no_filtra_la_resolucion_completa_de_otro_host() -> None:
    """El mensaje dice la dirección y el host, y solo esos dos.

    Es lo justo para que el usuario sepa qué corregir. Incluir el contenido de la consulta
    DNS o el resto de la configuración de red sería información que no le hace falta para
    arreglar su URL.
    """

    with patch(
        "backend.core.ssrf.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.50", 0))],
    ):
        with pytest.raises(SsrfBlockedError) as error:
            resolve_and_validate("https://interno.example.com/hook")

    detalle = str(error.value)
    assert "192.168.1.50" in detalle
    assert "interno.example.com" in detalle
    assert detalle.count(".") <= 6, "el detalle deberia ser corto y accionable"
