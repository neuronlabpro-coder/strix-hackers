"""Pruebas del agente: lo que se puede comprobar sin red y sin Docker.

## Por qué el agente tiene sus propias pruebas y no se apoya en las del backend

Porque corre en **otra** máquina, en la red del cliente, y porque lo que más se rompe aquí es el
*razonamiento sobre el resultado*: inventar una versión de paquete, afirmar una vulnerabilidad
que no se puede calcular, devolver «no hay nada abierto» sin haber mirado nada. Esos fallos no se
ven en el backend porque el backend no ejecuta el escaneo.

Y hay una limitación que conviene decir de entrada: estas pruebas **no** comprueban que el agente
funcione contra una plataforma real. Comprueban las decisiones —qué devuelve, qué se niega a
afirmar, qué hace con un error— y el recorrido completo se comprueba con `--una-vez` contra un
despliegue.
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

# El paquete vive un nivel arriba, y añadirlo a `sys.path` es lo que permite ejecutar las
# pruebas desde el propio directorio del agente sin instalar nada.
RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from fenix_agent import escaneo  # noqa: E402
from fenix_agent import red  # noqa: E402
from fenix_agent.config import (  # noqa: E402
    AgentConfig,
    ConfigError,
    PREFIJO_TOKEN,
    cargar_configuracion,
)
from fenix_agent.plataforma import (  # noqa: E402
    ClientePlataforma,
    ErrorDeAutenticacion,
    ErrorDePlataforma,
    ErrorTransitorio,
)


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #


class TestConfiguracion(unittest.TestCase):
    """Lo que el agente acepta y lo que se niega a aceptar antes de arrancar."""

    def _fichero(self, tmp: Path, cuerpo: str) -> Path:
        ruta = tmp / "agente.ini"
        ruta.write_text(cuerpo, encoding="utf-8")
        return ruta

    def test_una_configuracion_valida_se_carga(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directorio:
            ruta = self._fichero(
                Path(directorio),
                "[plataforma]\n"
                "url = https://api.example.com\n"
                f"token = {PREFIJO_TOKEN}aabbccdd\n",
            )
            config = cargar_configuracion(ruta)
        self.assertEqual(config.url, "https://api.example.com")
        self.assertEqual(config.intervalo_sondeo, 15)

    def test_rechaza_una_url_que_no_es_https(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directorio:
            ruta = self._fichero(
                Path(directorio),
                "[plataforma]\nurl = http://api.example.com\n" f"token = {PREFIJO_TOKEN}abcd\n",
            )
            with self.assertRaises(ConfigError) as error:
                cargar_configuracion(ruta)
        # El mensaje tiene que decir por qué importa el https, no solo que está mal.
        self.assertIn("https", str(error.exception))

    def test_rechaza_un_token_de_panel(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directorio:
            ruta = self._fichero(
                Path(directorio),
                "[plataforma]\nurl = https://api.example.com\ntoken = mgf_live_aabbcc\n",
            )
            with self.assertRaises(ConfigError) as error:
                cargar_configuracion(ruta)
        self.assertIn(PREFIJO_TOKEN, str(error.exception))

    def test_rechaza_un_intervalo_demasiado_corto(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directorio:
            ruta = self._fichero(
                Path(directorio),
                "[plataforma]\n"
                "url = https://api.example.com\n"
                f"token = {PREFIJO_TOKEN}abcd\n"
                "intervalo_sondeo = 1\n",
            )
            with self.assertRaises(ConfigError):
                cargar_configuracion(ruta)


# --------------------------------------------------------------------------- #
# Referencias de imagen
# --------------------------------------------------------------------------- #


class TestReferencias(unittest.TestCase):
    """Cómo se parte una referencia de imagen."""

    def test_una_imagen_sin_registro_es_docker_hub(self) -> None:
        referencia = escaneo.partir_referencia("alpine:3.20")
        self.assertEqual(referencia.registro, "registry-1.docker.io")
        self.assertEqual(referencia.repositorio, "library/alpine")
        self.assertEqual(referencia.etiqueta, "3.20")

    def test_un_namespace_de_docker_hub_no_se_trata_como_registro(self) -> None:
        # Sin punto y sin puerto, el primer componente es el namespace y no el registro. Si se
        # tratara como registro, `bitnami/nginx` apuntaria a un host llamado `bitnami`.
        referencia = escaneo.partir_referencia("bitnami/nginx:1.27")
        self.assertEqual(referencia.registro, "registry-1.docker.io")
        self.assertEqual(referencia.repositorio, "bitnami/nginx")

    def test_un_registro_propio_se_respeta(self) -> None:
        referencia = escaneo.partir_referencia("registry.example.com:5000/equipo/servicio:v1")
        self.assertEqual(referencia.registro, "registry.example.com:5000")
        self.assertEqual(referencia.repositorio, "equipo/servicio")

    def test_sin_etiqueta_se_usa_latest(self) -> None:
        self.assertEqual(escaneo.partir_referencia("alpine").etiqueta, None)

    def test_una_referencia_basura_da_un_error_utilizable(self) -> None:
        with self.assertRaises(escaneo.ErrorDeEscaneo) as error:
            escaneo.partir_referencia("no es una imagen!!")
        self.assertIn("alpine", str(error.exception))


# --------------------------------------------------------------------------- #
# Paquetes
# --------------------------------------------------------------------------- #


class TestPaquetes(unittest.TestCase):
    """El parsing de las bases de datos de paquetes, que es donde se podría inventar algo."""

    def test_lee_paquetes_de_apk(self) -> None:
        texto = (
            "C:Q18Pz9wvTaBYr2RzvEXE6rYpFiJbI=\n"
            "P:alpine-baselayout\n"
            "V:3.7.2-r1\n"
            "A:x86_64\n"
            "L:MIT\n"
            "\n"
            "C:Q1abc=\n"
            "P:busybox\n"
            "V:1.37.0-r5\n"
            "A:x86_64\n"
        )
        paquetes, motivo = escaneo.recorrer_capa_desde_texto(texto, "alpine")
        self.assertIsNone(motivo)
        self.assertEqual(len(paquetes), 2)
        self.assertEqual(paquetes[0]["name"], "alpine-baselayout")
        self.assertEqual(paquetes[0]["version"], "3.7.2-r1")
        self.assertEqual(paquetes[0]["ecosystem"], "apk")

    def test_lee_paquetes_de_dpkg_y_descarta_los_desinstalados(self) -> None:
        texto = (
            "Package: openssl\n"
            "Status: install ok installed\n"
            "Version: 3.1.4-r5\n"
            "Architecture: x86_64\n"
            "\n"
            "Package:Removed\n"
            "Status: deinstall ok config-files\n"
            "Version: 1.0\n"
            "\n"
            "Package: nginx\n"
            "Status: install ok installed\n"
            "Version: 1.26.0\n"
        )
        paquetes, motivo = escaneo.recorrer_capa_desde_texto(texto, "debian")
        self.assertIsNone(motivo)
        # El segundo tiene `deinstall ok config-files`: deja la entrada pero no esta en la
        # imagen, y reportarlo seria un falso positivo.
        self.assertEqual([p["name"] for p in paquetes], ["openssl", "nginx"])

    def test_una_imagen_sin_base_de_datos_lo_dice(self) -> None:
        paquetes, motivo = escaneo.recorrer_capa_desde_texto("", "debian")
        self.assertEqual(paquetes, [])
        # El motivo no es opcional: sin el, «cero paquetes» es indistinguible de «la imagen esta
        # vacia», y quien lee el resultado no puede diferenciar los dos casos.
        self.assertIsNotNone(motivo)
        self.assertIn("apk", motivo or "")


# --------------------------------------------------------------------------- #
# El escaneo de red
# --------------------------------------------------------------------------- #


class TestRed(unittest.TestCase):
    """Las decisiones del barrido que se pueden comprobar sin tocar la red."""

    def test_rechaza_un_cidr_mal_formado(self) -> None:
        with self.assertRaises(escaneo.ErrorDeEscaneo) as error:
            escaneo.escanear_red("10.10.0.0", [80])
        self.assertIn("CIDR", str(error.exception))

    def test_un_32_es_una_peticion_legitima_de_una_sola_maquina(self) -> None:
        # «Mira estos puertos de esta maquina» es una peticion que tiene sentido, y un /32 es
        # exactamente eso. Devolver «no hay nada abierto» sin mirar seria afirmar un resultado
        # falso, y rechazarlo obligaria a inventar una excepcion en otro sitio.
        preparacion = escaneo.preparar_direcciones("10.10.0.5/32", escaneo.MAX_HOSTS)
        self.assertEqual(preparacion["direcciones"], ["10.10.0.5"])
        self.assertFalse(preparacion["recortado"])

    def test_un_cidr_demasiado_grande_se_recorta_y_lo_dice(self) -> None:
        # No se ejecuta el barrido: lo que se comprueba aqui es la forma del recorte, que es la
        # parte que decide. Un recorte silencioso seria un inventario incompleto que parece
        # completo, y en un informe de postura eso es peor que no tener informe.
        con_prefijo = escaneo.preparar_direcciones("10.0.0.0/16", escaneo.MAX_HOSTS)
        self.assertTrue(con_prefijo["recortado"])
        self.assertEqual(len(con_prefijo["direcciones"]), escaneo.MAX_HOSTS)
        self.assertIn("motivo", con_prefijo)

    def test_un_prefijo_gestionable_no_se_recorta(self) -> None:
        sin_prefijo = escaneo.preparar_direcciones("10.10.10.0/28", escaneo.MAX_HOSTS)
        self.assertFalse(sin_prefijo["recortado"])
        # La red y la difusión no son hosts y no se sondean. En un /28 la difusión es
        # `10.10.10.15`, no `10.10.10.255`: un filtro por sufijo `.255` se cargaría un host real
        # y dejaría pasar la dirección de difusión, que es el error que esta prueba caza.
        self.assertNotIn("10.10.10.0", sin_prefijo["direcciones"])
        self.assertNotIn("10.10.10.15", sin_prefijo["direcciones"])
        self.assertIn("10.10.10.1", sin_prefijo["direcciones"])
        self.assertEqual(len(sin_prefijo["direcciones"]), 14)


# --------------------------------------------------------------------------- #
# El cliente de la plataforma
# --------------------------------------------------------------------------- #


class _RespuestaFalsa:
    """Una respuesta HTTP mínima, para probar el cliente sin red.

    ## Por qué lleva un cursor y antes no

    Porque `read(n)` sobre un objeto real **consume**: cada llamada devuelve los siguientes `n`
    bytes y avanza. Este devolvia siempre los primeros `n` bytes sin avanzar, y eso es un doble
    que miente.

    No se notaba mientras el cuerpo se leia con un solo `read()`, pero en cuanto la lectura se
    hizo por trozos —que es lo que permite no materializar un cuerpo de 4 GB— el bucle de
    lectura no terminaba nunca y el test fallaba por el motivo equivocado. Un doble que no
    reproduce el contrato de lo que dobla no es un atajo: es una trampa que solo se ve cuando el
    codigo que dobla cambia.
    """

    def __init__(self, cuerpo: bytes = b"") -> None:
        self._cuerpo = cuerpo
        self._cursor = 0

    def read(self, limite: int = -1) -> bytes:
        if limite is None or limite < 0:
            trozo = self._cuerpo[self._cursor :]
            self._cursor = len(self._cuerpo)
            return trozo
        trozo = self._cuerpo[self._cursor : self._cursor + limite]
        self._cursor += len(trozo)
        return trozo

    def __enter__(self) -> "_RespuestaFalsa":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class TestClientePlataforma(unittest.TestCase):
    """El trato de los errores de la plataforma, que es donde un agente se porta mal."""

    def setUp(self) -> None:
        # El host de la plataforma se declara alcanzable, **igual que lo hace el bucle al
        # arrancar** con la configuracion real. No es un atajo de pruebas: es el mismo camino
        # que production, y por eso estos tests siguen significando algo sobre el filtro de red.
        #
        # Y no se desactiva el filtro: `api.example.com` no resuelve en un entorno de pruebas, y
        # declararlo es lo que permite que la comprobacion de esquema, de IP y de redirecciones
        # siga ejecutandose en cada uno de estos cuatro casos.
        red.declarar_entidades_permitidas("api.example.com")
        self.addCleanup(red.declarar_entidades_permitidas, [])

    def _cliente(self) -> ClientePlataforma:
        return ClientePlataforma(
            AgentConfig(
                url="https://api.example.com",
                token=f"{PREFIJO_TOKEN}aabbccdd",
                intervalo_sondeo=15,
                connect_timeout=5.0,
                read_timeout=5.0,
            )
        )

    def test_un_401_no_se_trata_como_transitorio(self) -> None:
        # Si un 401 se tratara como transitorio, el agente reintentaria veinte veces con un token
        # muerto y el log llenandose de lineas identicas. Lo que necesita es que alguien mire.
        import urllib.error

        cliente = self._cliente()
        with unittest.mock.patch(
            "fenix_agent.red._OPENER.open",
            side_effect=urllib.error.HTTPError(
                "https://api.example.com", 401, "Unauthorized", {}, None
            ),
        ):
            with self.assertRaises(ErrorDeAutenticacion):
                cliente.pedir_trabajo()

    def test_un_500_si_es_transitorio(self) -> None:
        import urllib.error

        cliente = self._cliente()
        with unittest.mock.patch(
            "fenix_agent.red._OPENER.open",
            side_effect=urllib.error.HTTPError(
                "https://api.example.com", 503, "Service Unavailable", {}, None
            ),
        ):
            with self.assertRaises(ErrorTransitorio):
                cliente.pedir_trabajo()

    def test_una_respuesta_vacia_significa_que_no_hay_trabajo(self) -> None:
        # Y no un error. «No hay nada que hacer» es la respuesta mas frecuente del ciclo, y
        # tratarla como fallo haria que el agente gastase reintentos en ella.
        cliente = self._cliente()
        with unittest.mock.patch(
            "fenix_agent.red._OPENER.open",
            return_value=_RespuestaFalsa(b""),
        ):
            self.assertIsNone(cliente.pedir_trabajo())

    def test_una_respuesta_que_no_es_json_avisa_de_la_url(self) -> None:
        cliente = self._cliente()
        with unittest.mock.patch(
            "fenix_agent.red._OPENER.open",
            return_value=_RespuestaFalsa(b"<html>portal</html>"),
        ):
            with self.assertRaises(ErrorDePlataforma) as error:
                cliente.pedir_trabajo()
        # El mensaje tiene que señalar la causa más probable: una url que apunta al portal.
        self.assertIn("url", str(error.exception).lower())


if __name__ == "__main__":
    unittest.main()
