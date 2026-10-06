"""Las dos reglas nuevas de la auditoría de marca, probadas por separado.

## Por qué este fichero existe

Porque las dos reglas que se han añadido —la clave de código de error y el nombre de una variable
de entorno— son **exenciones**, y una excepción sin prueba es una excepción que se aplica sin
comprobar. Las dos se escribieron por un fallo concreto: la primera, porque 23 apariciones del
fichero de traducciones hicieron saltar el gate y la respuesta fácil —silenciar la marca— era
inaceptable; la segunda, porque el texto que nombra la variable que hay que cambiar es el único
texto accionable que puede escribir un operador.

Y una excepción que se ensancha sin que nadie lo note deja de ser una excepción. Por eso aquí se
comprueba **también el otro lado**: que una marca en prosa dentro de un fichero de traducciones
siga contando como fallo. Un test que solo comprobara que las excepciones funcionan pasaría con
un clasificador que no falla nunca, que es el peor estado posible para un gate.

## Por qué se prueba con `clasificar_marca` y no con el informe

Porque el informe es la salida y la regla es la función. Un test que lee el `.md` depende de que
alguien lo haya generado antes, que es un acoplamiento al orden de ejecución. La función se
puede llamar con una línea y un contexto, y eso es exactamente lo que decide.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# El módulo se carga por ruta porque está en `scripts/` y no es un paquete.
#
# Y se registra en `sys.modules` **antes** de ejecutarlo, que es lo que no está aquí y es lo que
# fallaba: `@dataclass` resuelve los tipos de sus campos mirando `sys.modules[cls.__module__]`,
# y un módulo cargado con `module_from_spec` sin registrar no está. El error sale como un
# `AttributeError: 'NoneType' object has no attribute '__dict__'` en la línea del decorador, que
# no dice nada de que el problema sea no haberse registrado.
_RAIZ = Path(__file__).resolve().parents[2]
_especificacion = importlib.util.spec_from_file_location(
    "audit_strix_parity", _RAIZ / "scripts" / "audit_strix_parity.py"
)
assert _especificacion is not None and _especificacion.loader is not None
auditoria = importlib.util.module_from_spec(_especificacion)
sys.modules["audit_strix_parity"] = auditoria
_especificacion.loader.exec_module(auditoria)

TRADUCCIONES = "frontend/src/locales/es/pentests.json"
COMPONENTE = "frontend/src/features/pentests/PentestRunPage.tsx"


class TestClavesDeCodigoDeError:
    """La clave es el contrato con el worker; el valor es lo que el usuario lee."""

    def test_la_clave_de_un_codigo_no_es_marca_de_usuario(self) -> None:
        linea = '      "STRIX_DOCKER_UNAVAILABLE": "El worker no pudo conectarse al demonio",'

        categoria, _ = auditoria.clasificar_marca(TRADUCCIONES, linea)

        assert categoria == "identidad tecnica"

    def test_el_valor_de_una_traduccion_sigue_siendo_marca_de_usuario(self) -> None:
        # La contraparte que hace falta: si esto pasara, la excepción se habría tragado el
        # fichero entero y el gate no volvería a avisar de nada.
        linea = '      "otro": "El motor Strix no responde",'

        categoria, _ = auditoria.clasificar_marca(TRADUCCIONES, linea)

        assert categoria == "usuario"

    def test_una_clave_que_no_lleva_la_marca_no_cambia_nada(self) -> None:
        linea = '      "otro": "Una frase normal",'

        categoria, _ = auditoria.clasificar_marca(TRADUCCIONES, linea)

        assert categoria != "identidad tecnica"


class TestNombresDeVariablesDeEntorno:
    """El nombre de la variable es la instrucción accionable para el operador."""

    def test_el_nombre_de_la_variable_no_es_marca_de_usuario(self) -> None:
        linea = (
            '      "EGRESS_FENCE_DISABLED": "El cerco esta desactivado (variable '
            'STRIX_REQUIRE_EGRESS_FENCE en false)",'
        )

        categoria, _ = auditoria.clasificar_marca(TRADUCCIONES, linea)

        assert categoria == "identidad tecnica"

    def test_la_marca_en_prosa_junto_a_una_variable_no_se_exime(self) -> None:
        """La excepción es estrecha a propósito.

        Una línea que dice «Strix» **y** menciona una variable son dos afirmaciones, y una de
        ellas es marca en texto de usuario. Se deja como fallo para que alguien la mire, que es
        lo que hace la categoría «por revisar» con el resto.
        """

        linea = (
            '      "otro": "Strix no arranca con STRIX_REQUIRE_EGRESS_FENCE en false",'
        )

        categoria, _ = auditoria.clasificar_marca(TRADUCCIONES, linea)

        assert categoria == "usuario"

    def test_la_variable_sin_la_marca_no_dispara_la_excepcion(self) -> None:
        linea = '      "otro": "Usa SCAN_CREDIT_COST para ajustar el precio",'

        categoria, _ = auditoria.clasificar_marca(TRADUCCIONES, linea)

        assert categoria != "identidad tecnica"


class TestComentariosEnComponentes:
    """Un comentario que explica por qué existe la marca no es la marca en pantalla."""

    def test_una_linea_de_comentario_es_comentario(self) -> None:
        # Antes, el gate contaba el cuerpo de un comentario de bloque como literal de componente,
        # que obligaba a no explicar las decisiones. Una decisión sin explicación es peor que la
        # marca que evita.
        categoria, _ = auditoria.clasificar_marca(COMPONENTE, "  * usa STRIX_DOCKER_UNAVAILABLE")

        assert categoria == "comentario"

    def test_el_cuerpo_de_un_comentario_de_bloque_es_comentario(self) -> None:
        # El caso real que motivó el estado por línea: este texto no empieza por delimitador
        # ninguno, así que sin el estado se contaba como código.
        cuerpo = "  una menciona STRIX_EXECUTION_FAILED en la terminal"

        categoria, _ = auditoria.clasificar_marca(
            COMPONENTE, cuerpo, dentro_de_comentario=True
        )

        assert categoria == "comentario"

    def test_la_marca_en_codigo_de_un_componente_sigue_siendo_fallo(self) -> None:
        # La contraparte del bloque de arriba, y la que importa: un literal que se renderiza.
        categoria, _ = auditoria.clasificar_marca(
            COMPONENTE, "const etiqueta = 'Analizar con STRIX ahora'", dentro_de_comentario=False
        )

        assert categoria == "usuario"


class TestElGateSigueFallando:
    """Un gate que no falla nunca es peor que no tener gate."""

    def test_el_archivo_entero_se_audita_y_no_hay_marca_de_usuario(self) -> None:
        """Se recorre el árbol real, no un caso inventado.

        Es el test que demuestra que las excepciones no se han comido nada por el camino: si
        hubiera una tercera aparición de la marca en un fichero de traducciones en prosa, este
        test caería.
        """

        hallazgos = auditoria.apariciones_de_la_marca()
        de_usuario = [h for h in hallazgos if h.categoria == "usuario"]

        assert de_usuario == [], "\n".join(f"{h.donde}: {h.texto}" for h in de_usuario)

    @pytest.mark.parametrize(
        "ruta_relativa",
        [TRADUCCIONES, COMPONENTE, "backend/workers/runner/sandbox.py"],
    )
    def test_la_funcion_no_revienta_con_una_ruta_conocida(self, ruta_relativa: str) -> None:
        # El clasificador se llama desde el recorrido con rutas de tres directorios distintos, y
        # un `KeyError` en la primera regla que sevierte de ruta sería un fallo que solo aparece
        # en el gate, no en las pruebas.
        categoria, razon = auditoria.clasificar_marca(ruta_relativa, "una linea con strix dentro")

        assert categoria in {"usuario", "comentario", "identidad tecnica", "por revisar"}
        assert razon
