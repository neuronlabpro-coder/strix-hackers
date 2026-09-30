"""El filtro de destino del agente, que es lo que cierra tres de los cuatro hallazgos altos.

## Qué se comprueba aquí y por qué necesita pruebas propias

`red.py` no es un detalle: es **la frontera entre el componente menos confiable del sistema y una
red que el cliente controla**. Y sus tres propiedades —esquema acotado, destino no privado, sin
redirecciones— se pueden romper sin que nada falle visiblemente: un `urlopen` global vuelve a
traer `FileHandler`, una comprobación de IP mal puesta deja pasar `169.254.169.254`, y un
`HTTPRedirectHandler` por defecto reenvía el `Authorization` del agente a quien mande un `302`.

Ninguna de las tres produce una excepción cuando se rompe. Por eso se prueban directamente.

## El patrón de estas pruebas

No se comprueba que «no haya excepción»: se comprueba que la excepción que se produce **es la
correcta** y que el nombre de la prueba dice qué se rompió. Un test que pasa porque no Passó nada
—porque el destino se resolvió bien y la respuesta fue válida— no está probando el filtro, está
probando el DNS.
"""

import ipaddress
import socket
import unittest
import unittest.mock
import urllib.error

from fenix_agent import red


class _RespuestaQueRedirige:
    """Una respuesta con `Location`, que es lo que activa el `HTTPRedirectHandler` por defecto."""

    def __init__(self, destino: str) -> None:
        self.headers = {"Location": destino}
        self.status = 302

    def read(self, limite: int = -1) -> bytes:
        return b""

    def close(self) -> None:
        return None

    def __enter__(self) -> "_RespuestaQueRedirige":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class TestEsquemaAcotado(unittest.TestCase):
    """`file://` y `ftp://` no se abren.

    ## Por qué esto importa más de lo que parece

    Porque el opener global de `urllib` **los tiene registrados**. `urlopen("file:///etc/passwd")`
    intenta abrir el fichero local, y `ftp://` permite exfiltrar por FTP. Un agente que corre
    dentro de la red del cliente y que acepta un esquema elegido por quien encola el escaneo
    puede leer el disco de la máquina del cliente.
    """

    def test_file_no_se_abre(self) -> None:
        with self.assertRaises(red.DestinoNoPermitido) as error:
            red._comprobar_destino("file:///etc/passwd")
        self.assertIn("esquema", str(error.exception))

    def test_ftp_no_se_abre(self) -> None:
        with self.assertRaises(red.DestinoNoPermitido):
            red._comprobar_destino("ftp://ejemplo/fichero")

    def test_https_si_se_acepta(self) -> None:
        """El caso positivo, para que el test anterior no pasara por un motivo equivocado."""

        red.declarar_entidades_permitidas("api.ejemplo.com")
        self.addCleanup(red.declarar_entidades_permitidas, [])
        host, puerto = red._comprobar_destino("https://api.ejemplo.com/ruta")
        self.assertEqual(host, "api.ejemplo.com")
        self.assertEqual(puerto, 443)

    def test_una_url_con_credenciales_incrustadas_no_se_abre(self) -> None:
        """`https://user:clave@host` pone la credencial en la URL, que es lo que el agente evita
        en todas partes. No hay ningún caso legítimo aquí: no trae credenciales, a propósito."""

        with self.assertRaises(red.DestinoNoPermitido) as error:
            red._comprobar_destino("https://usuario:clave@api.ejemplo.com/x")
        self.assertIn("credenciales", str(error.exception))


class TestRedesPrivadas(unittest.TestCase):
    """El destino se resuelve y **todas** sus direcciones se comprueban."""

    def setUp(self) -> None:
        red.declarar_entidades_permitidas([])
        self.addCleanup(red.declarar_entidades_permitidas, [])

    def _fingir_una_ip(self, direccion: str) -> None:
        """Hace que `getaddrinfo` devuelva una sola dirección, sin tocar la red de verdad."""

        familia = socket.AF_INET6 if ":" in direccion else socket.AF_INET
        informacion = [(familia, 1, 6, "", (direccion, 443))]
        unittest.mock.patch.object(
            socket, "getaddrinfo", return_value=informacion
        ).start()
        self.addCleanup(unittest.mock.patch.stopall)

    def test_loopback_no_se_abre(self) -> None:
        self._fingir_una_ip("127.0.0.1")
        with self.assertRaises(red.DestinoNoPermitido) as error:
            red._comprobar_destino("https://intranet.ejemplo.com/x")
        self.assertIn("no publica", str(error.exception))

    def test_una_ip_literal_privada_no_se_abre_sin_resolver(self) -> None:
        """Una IP literal se comprueba sin pasar por DNS.

        Importa que sea un camino **distinto**: si una IP pasara por el resolvedor y un nombre
        por la comprobación literal, habría dos rutas y la segunda se olvidaría antes o después.
        """

        with self.assertRaises(red.DestinoNoPermitido):
            red._comprobar_destino("https://10.20.30.40/x")

    def test_metadatos_de_nube_no_se_abren(self) -> None:
        """`169.254.169.254` es enlace-local, y `is_private` **no** lo cubre.

        Es la razón de que el enlace-local se compruebe aparte: `ipaddress` dice que
        `169.254.0.0/16` no es privado, y sin esa comprobación explícita el filtro dejaría pasar
        justo la dirección que roba credenciales de una máquina en la nube.
        """

        self.assertTrue(
            ipaddress.ip_address("169.254.169.254").is_link_local,
            "esto es lo que hace necesaria la comprobacion: is_private NO lo cubre",
        )
        with self.assertRaises(red.DestinoNoPermitido):
            red._comprobar_destino("http://169.254.169.254/latest/meta-data/")

    def test_un_nombre_que_resuelve_a_dos_y_uno_es_privado_se_rechaza(self) -> None:
        """DNS rebinding, el caso que un filtro de «la primera IP» deja abierto.

        El atacante devuelve una IP pública en la primera consulta —la que ve el filtro— y una
        privada en la segunda, que es la que usa la conexión. Rechazar el conjunto entero lo
        cierra.
        """

        informacion = [
            (socket.AF_INET, 1, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, 1, 6, "", ("10.0.0.5", 443)),
        ]
        unittest.mock.patch.object(socket, "getaddrinfo", return_value=informacion).start()
        self.addCleanup(unittest.mock.patch.stopall)
        with self.assertRaises(red.DestinoNoPermitido):
            red._comprobar_destino("https://rebinding.ejemplo.com/x")

    def test_un_nombre_enteramente_publico_si_pasa(self) -> None:
        """El caso positivo. Sin él, los cinco tests anteriores podrían pasar porque el filtro
        rechazara todo, y eso no sería un filtro sino un `raise` incondicional."""

        self._fingir_una_ip("93.184.216.34")
        host, _puerto = red._comprobar_destino("https://publico.ejemplo.com/x")
        self.assertEqual(host, "publico.ejemplo.com")

    def test_la_plataforma_si_puede_estar_en_una_red_privada(self) -> None:
        """Un despliegue con la API en `10.x` es legítimo, y el filtro lo tiene que admitir.

        Se declara **por host**, no desactivando el filtro. Por eso este test y los anteriores
        pueden convivir: la lista es de un host y el filtro sigue applying a todos los demás.
        """

        red.declarar_entidades_permitidas("api.interno.cliente")
        # Con la lista declarada, un `10.x` de la plataforma no se rechaza.
        self.assertEqual(
            red._comprobar_destino("https://api.interno.cliente/x")[0],
            "api.interno.cliente",
        )
        # Y uno que no está en la lista, sí.
        self._fingir_una_ip("10.1.2.3")
        with self.assertRaises(red.DestinoNoPermitido):
            red._comprobar_destino("https://otro.interno.cliente/x")


class TestSinRedirecciones(unittest.TestCase):
    """Un `3xx` no produce una segunda petición.

    Es lo que impide que el `Authorization` del agente viaje al host de destino de una
    redirección: en CPython, `HTTPRedirectHandler.redirect_request` copia todas las cabeceras
    salvo `content-length` y `content-type`.
    """

    def setUp(self) -> None:
        red.declarar_entidades_permitidas("api.ejemplo.com")
        self.addCleanup(red.declarar_entidades_permitidas, [])

    def test_una_redireccion_se_rechaza_en_vez_de_seguirse(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.ejemplo.com/x",
            302,
            "Found",
            {"Location": "https://atacante.ejemplo/robo"},
            None,  # type: ignore[arg-type]
        )
        with unittest.mock.patch.object(red._OPENER, "open", side_effect=error):
            with self.assertRaises(red.DestinoNoPermitido) as capturado:
                red.abrir("https://api.ejemplo.com/x")
        # El mensaje nombra el destino de la redirección, para que el operador vea qué pasó.
        self.assertIn("atacante.ejemplo", str(capturado.exception))

    def test_el_opener_no_tiene_file_handler(self) -> None:
        """La defensa de fondo, sobre el objeto y no sobre el comportamiento.

        Que `file://` no funcione es una consecuencia de que no haya `FileHandler`. Si alguien
       construye el opener de otra manera, este test lo dice aunque el otro no llegue a
        ejecutarse.
        """

        manejadores = {type(h).__name__ for h in red._OPENER.handlers}
        self.assertNotIn("FileHandler", manejadores)
        self.assertNotIn("FTPHandler", manejadores)


class TestCuerpoAcotado(unittest.TestCase):
    """El cuerpo se lee por trozos y **falla** si hay más.

    ## Por qué falla y no corta en silencio

    Porque un cuerpo truncado que se acepta como completo produce un resultado que parece un
    manifiesto entero y no lo es. El escaneo continuaría con una lista de capas incompleta y
    publicaría un digest y un recuento de paquetes falsos: un dato de evidencia con apariencia de
    correcto. Un error es preferible a eso.
    """

    def test_un_cuerpo_dentro_del_tope_se_lee_entero(self) -> None:
        respuesta = _RespuestaConCuerpo(b"a" * 1000)
        self.assertEqual(len(red.leer_acotado(respuesta, tope=2000)), 1000)

    def test_un_cuerpo_que_supera_el_tope_falla(self) -> None:
        respuesta = _RespuestaConCuerpo(b"a" * 5000)
        with self.assertRaises(red.DestinoNoPermitido) as error:
            red.leer_acotado(respuesta, tope=1000)
        self.assertIn("maximo", str(error.exception))

    def test_una_respuesta_vacia_no_falla(self) -> None:
        self.assertEqual(red.leer_acotado(_RespuestaConCuerpo(b""), tope=1000), b"")


class _RespuestaConCuerpo:
    """Un `read(n)` que consume, como el de verdad."""

    def __init__(self, cuerpo: bytes) -> None:
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


if __name__ == "__main__":
    unittest.main()
