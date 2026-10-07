"""El clasificador de fallos de `scripts/ci_check.py`: qué declara «entorno» y qué no.

## Por qué este fichero existe

Porque el clasificador es lo único que decide si un rojo cuenta como **entrega no apta**. Un
clasificador que etiqueta un fallo de código como problema del entorno no falla de forma visible:
devuelve un resumen con `N/A` y un cero en la cuenta de rojos, y quien lo lee se marcha con la
creencia de que el proyecto está verificado.

## El defecto concreto que se vigila

`pytest` que falla con `assert 'strix.exe' not found in comando` contiene, literalmente, las
palabras de «falta una herramienta». El clasificador las veía, marcaba el gate como problema del
entorno, y el rojo desaparecía del resumen. Aquí se demuestra que **no** se vuelve a esconder, y
que un problema de entorno de verdad **sigue** reconociéndose.

## Por qué se prueba el módulo del script y no una copia

Porque una copia de la lógica probaría la copia. `scripts/ci_check.py` se importa directamente: si
alguien cambia el regex, estas pruebas cambian con él, que es lo que hace útil una prueba de este
tipo.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

RAIZ = Path(__file__).resolve().parents[2]
RUTA_DEL_SCRIPT = RAIZ / "scripts" / "ci_check.py"

#: Nombre con el que se registra el módulo mientras se carga. Hace falta porque `exec_module`
#: espera que el módulo esté en `sys.modules` para resolver su propio `__name__`, y quitarlo
#: después evita que se quede un duplicado con el mismo contenido y otro nombre.
NOMBRE_DEL_MODULO = "_ci_check_bajo_prueba"


def _cargar_ci_check() -> ModuleType:
    """Importa `scripts/ci_check.py` como módulo, sin tocar `sys.path` del proyecto.

    ## Por qué con `importlib` y no un `import scripts.ci_check`

    Porque `scripts/` no es un paquete y `backend/tests` corre con la raíz del backend en el
    `path`. Meter la raíz del repositorio en el `sys.path` de la batería sería cambiar el
    entorno de todas las pruebas de Python para poder leer un fichero, y eso puede cambiar cuál
    versión de un módulo se importa en otra prueba.
    """

    especificacion = importlib.util.spec_from_file_location(NOMBRE_DEL_MODULO, RUTA_DEL_SCRIPT)
    assert especificacion is not None
    assert especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    sys.modules[NOMBRE_DEL_MODULO] = modulo
    try:
        especificacion.loader.exec_module(modulo)
    finally:
        sys.modules.pop(NOMBRE_DEL_MODULO, None)
    return modulo


ci_check = _cargar_ci_check()


def _resultado(codigo: int, salida: str):
    return ci_check.Resultado(
        gate=ci_check.Gate(clave="pytest", titulo="x", comando=["x"]),
        codigo=codigo,
        segundos=1.0,
        salida=salida,
    )


# --------------------------------------------------------------------------- #
# El rojo se ve como rojo
# --------------------------------------------------------------------------- #


def test_una_asercion_que_dice_not_found_no_se_declara_problema_de_entorno() -> None:
    """El defecto exacto: el mensaje de una aserción containía las palabras del entorno.

    ## Por qué este caso y no otro cualquiera

    Porque es el que pasó. La expresión anterior buscaba `not found` a secas, y el mensaje de esta
    aserción —que afirma que un ejecutable **no** está en la lista de argumentos— la cumplía. El
    gate se marcaba `N/A`, la cuenta de rojos bajaba a cero, y el proyecto podía estar roto sin
    que nadie lo viera.
    """

    salida = (
        "____________________________ test_el_comando_no_lleva_run_name __________________\n"
        "\n"
        "    def test_el_comando_no_lleva_run_name():\n"
        ">       assert 'strix.exe' not found in comando\n"
        "E       AssertionError: assert 'strix.exe' not found in comando\n"
        "\n"
        "FAILED backend/tests/test_strix_host_runner.py::test_el_comando_no_lleva_run_name\n"
        "1 failed, 62 passed\n"
    )

    resultado = ci_check.clasificar_fallo(_resultado(1, salida))

    assert resultado.fallida_por_entorno is False
    assert resultado.codigo == 1
    assert [r for r in [resultado] if not r.fallida_por_entorno] == [resultado], (
        "un rojo tiene que contar como entrega no apta"
    )


def test_un_fallo_de_pytest_con_asercion_se_ve_como_rojo_aunque_diga_connection_refused() -> None:
    """Una palabra de entorno **dentro** de una aserción tampoco clasifica.

    Es el caso simétrico del anterior: `assert 'connection refused' not in mensaje` contiene la
    firma de red, y con el orden de comprobaciones equivocado taparía un fallo de código.
    """

    salida = (
        "    def test_el_mensaje_no_filta_secretos():\n"
        ">       assert 'connection refused' not in error\n"
        "E       AssertionError: assert 'connection refused' not in error\n"
        "E        +  where True = False\n"
        "FAILED backend/tests/test_diagnostico.py::test_el_mensaje_no_filta_secretos\n"
        "1 failed, 3 passed\n"
    )

    resultado = ci_check.clasificar_fallo(_resultado(1, salida))

    assert resultado.fallida_por_entorno is False


def test_un_error_de_tipos_se_ve_como_rojo() -> None:
    """Un `tsc` en rojo es un fallo de código, aunque hable de un fichero que no encuentra."""

    salida = (
        "src/features/pentests/PentestRunPage.tsx(210,7): error TS2339: Property 'x' does not "
        "exist on type 'Y'.\n"
        "Found 1 error in 1 file.\n"
    )

    resultado = ci_check.clasificar_fallo(_resultado(2, salida))

    assert resultado.fallida_por_entorno is False


def test_un_traceback_se_ve_como_rojo() -> None:
    """Un `Traceback` es siempre código: no hay ningún problema de entorno que lo produzca."""

    salida = (
        "Traceback (most recent call last):\n"
        '  File "scripts/x.py", line 3, in <module>\n'
        "    raise ValueError('algo roto')\n"
        "ValueError: algo roto\n"
    )

    resultado = ci_check.clasificar_fallo(_resultado(1, salida))

    assert resultado.fallida_por_entorno is False


def test_un_gate_en_verde_no_se_clasifica() -> None:
    """Con código 0 no se busca nada: un gate verde que menciona `not found` sigue verde."""

    resultado = ci_check.clasificar_fallo(_resultado(0, "1 passed\nnot found: nada que hacer\n"))

    assert resultado.fallida_por_entorno is False
    assert resultado.codigo == 0


# --------------------------------------------------------------------------- #
# El problema de entorno de verdad se sigue reconociendo
# --------------------------------------------------------------------------- #


def test_un_econnrefused_sin_aserciones_sigue_siendo_problema_de_entorno() -> None:
    """Lo que la clasificación existía para. Sin esta mitad, el arreglo sería tapar todo."""

    salida = (
        "sqlalchemy.exc.OperationalError: (psycopg.OperationalError)\n"
        "connection to server at \"100.89.59.70\", port 5433 failed: Connection refused\n"
    )

    resultado = ci_check.clasificar_fallo(_resultado(1, salida))

    assert resultado.fallida_por_entorno is True
    assert "base de datos" in resultado.motivo_entorno


def test_un_modulo_que_falta_sigue_siendo_problema_de_entorno() -> None:
    """`ModuleNotFoundError` con su forma real: es el arranque, no una aserción."""

    salida = "ModuleNotFoundError: No module named 'playwright'\n"

    resultado = ci_check.clasificar_fallo(_resultado(1, salida))

    assert resultado.fallida_por_entorno is True
    assert "herramienta" in resultado.motivo_entorno


@pytest.mark.parametrize(
    "salida",
    [
        "'npm' is not recognized as an internal or external command",
        "bash: uv: command not found",
        "[WinError 2] El sistema no puede encontrar el archivo especificado",
    ],
)
def test_las_tres_formas_de_ejecutable_ausente_se_reconocen(salida: str) -> None:
    """Las tres fórmulas que usa cada shell de esta máquina, cada una con su forma exacta."""

    resultado = ci_check.clasificar_fallo(_resultado(127, salida))

    assert resultado.fallida_por_entorno is True


def test_un_timeout_de_conexion_sin_aserciones_sigue_siendo_problema_de_entorno() -> None:
    """El margen Alto de la Tailscale es real, y su fallo tiene que seguir siendo del entorno."""

    resultado = ci_check.clasificar_fallo(
        _resultado(1, "asyncio.exceptions.TimeoutError: connection timed out\n")
    )

    assert resultado.fallida_por_entorno is True
    assert "tiempo" in resultado.motivo_entorno


# --------------------------------------------------------------------------- #
# El resumen: lo que se cuenta
# --------------------------------------------------------------------------- #


def test_el_resumen_cuenta_un_rojo_de_codigo_como_entrega_no_apta() -> None:
    """La consecuencia, medida sobre el resumen y no sobre la bandera.

    Es lo que importa: `Resumen.fallidos` es la lista que decide el código de salida del script.
    """

    codigo = ci_check.clasificar_fallo(
        _resultado(1, "E   assert 1 == 2\nFAILED test.py::test_algo\n1 failed\n")
    )
    entorno = ci_check.clasificar_fallo(_resultado(1, "ECONNREFUSED al conectar\n"))
    resumen = ci_check.Resumen(resultados=[codigo, entorno])

    assert len(resumen.fallidos) == 1
    assert len(resumen.por_entorno) == 1
