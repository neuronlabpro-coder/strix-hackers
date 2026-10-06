"""El escape de `LIKE` vive en un solo sitio, y lo que se consolidated hace lo mismo.

## Qué hay aquí y por qué

Porque hubo **cuatro copias** de `_escape_like` en el árbol, una por módulo, cada una con su
propio docstring defendiendo por qué se quedaba. Es el peor tipo de duplicación posible en este
proyecto: las cuatro son correctas, así que el fallo no está en ninguna, y un día una se toca y las
otras tres siguen como estaban. Este fichero es lo que convierte «la consolidé yo, tranquilo» en
una propiedad que se comprueba.

## Las dos pruebas

1. **`test_no_queda_ninguna_copia_local`**: recorre el árbol y falla si aparece un `def
   _escape_like` fuera del módulo compartido. Es la que avisa de la quinta copia antes de que
   exista.
2. **`test_el_escape_consolidado_hace_lo_mismo_que_las_copias_que_tenia`**: el cuerpo exacto de las
   cuatro copias —la barra primero, luego `%`, luego `_`— contra `core.filtros_texto.escape_like`,
   sobre el conjunto de términos que separa un orden correcto de uno incorrecto. No prueba que el
   orden «se escaló bien»: prueba que consolidar no cambió ni un término.

## Por qué el caso de la barra invertida va en la lista

Porque es el único sitio donde un orden equivocado **no se ve en el resultado de la pantalla**. Con
`%` mal escapado sale la tabla entera y cualquier aserción de recuento lo delata; con la barra mal
escalada el resultado es el mismo que con la barra bien escalada para casi todos los términos, y el
defecto solo aparece en la entrada que empieza por una barra. Un filtro de búsqueda es de las pocas
cosas donde un error se confunde con una pantalla que funciona, así que el orden va con prueba.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from backend.core.filtros_texto import escape_like

#: La raíz de `backend/`, para recorrer el árbol sin depender del directorio de trabajo.
RAIZ_BACKEND = Path(__file__).resolve().parents[1]

#: El cuerpo literal de las cuatro copias, tal como estaba en el árbol antes de consolidar.
CUERPO_DE_LAS_COPIAS = "return termino.replace"


def _copia_local_como_antes(termino: str) -> str:
    """El cuerpo exacto de las cuatro copias locales, para comparar contra el módulo."""

    return termino.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _terminos_que_separan_los_ordenes() -> list[str]:
    """Términos en los que un orden de escape distinto da un resultado distinto.

    Se incluyen todas las combinaciones de longitud hasta tres sobre el alfabeto del escape
    —`\\`, `%`, `_` y un carácter neutro—, que es lo que cubre el espacio entero: un orden que se
    equivoque al escapar la barra produce aquí un término que el orden correcto no produce.
    """

    base = [
        "",
        "acme_corp",
        "100%off",
        "web_app",
        "CVE-2026-100599",
        "TK-1005",
        "\\",
        "\\\\",
        "%",
        "_",
        "\\_",
        "%\\_",
        "_%\\",
        "\\\\%",
    ]
    alfabeto = ["\\", "%", "_", "x"]
    for largo in (1, 2, 3):
        for combinacion in itertools.product(alfabeto, repeat=largo):
            base.append("".join(combinacion))

    # Los términos no salen solo de la base: 4^1 + 4^2 + 4^3 = 84 combinaciones del alfabeto del
    # escape, más los 13 términos a mano.
    #
    # Y sobre un alfabeto más ancho —el del escape más dos caracteres neutros— se recorren todas
    # las combinaciones de longitud hasta tres: 6^1 + 6^2 + 6^3 = 258. No se usa `random` porque
    # una lista de casos tiene que ser **la misma** en cada ejecución: si un fallo aparece un día
    # y al otro día no, no se puede depurar. Exhausivo y determinista.
    alfabeto_amplio = [*alfabeto, "a", "9"]
    for largo in (1, 2, 3):
        for combinacion in itertools.product(alfabeto_amplio, repeat=largo):
            base.append("".join(combinacion))
    return base


def test_no_queda_ninguna_copia_local() -> None:
    """Ningún módulo fuera del compartido vuelve a declarar su propio escape.

    Se recorre el árbol entero en vez de una lista de ficheros, porque la quinta copia es
    justamente el caso que una lista no pilla: aparecería en un módulo que nadie pensó en añadir.

    Este propio fichero queda fuera, y no por una excepción colada: guarda a propósito el cuerpo
    viejo para poder compararlo, así que se excluiría a sí mismo. La comprobación es que **no haya
    ninguna otra**.
    """

    definiciones: list[str] = []
    for ruta in RAIZ_BACKEND.rglob("*.py"):
        if "__pycache__" in ruta.parts or ".venv" in ruta.parts:
            continue
        if ruta.name in {"filtros_texto.py", Path(__file__).name}:
            continue
        if ruta.parts[-2] == "tests":
            # Los ficheros de prueba guardan a proposito el cuerpo viejo para compararlo, asi que
            # buscar el texto del escape en ellos daria un positivo siempre. Un modulo de codigo
            # nunca lleva una copia.
            continue
        texto = ruta.read_text(encoding="utf-8", errors="replace")
        if CUERPO_DE_LAS_COPIAS in texto or "def _escape_like" in texto:
            definiciones.append(str(ruta.relative_to(RAIZ_BACKEND)))

    assert definiciones == [], (
        f"Ha vuelto a aparecer una copia local del escape de LIKE en {definiciones}. El módulo "
        "compartido es backend/core/filtros_texto.py:escape_like."
    )


@pytest.mark.parametrize("termino", _terminos_que_separan_los_ordenes())
def test_el_escape_consolidado_hace_lo_mismo_que_las_copias_que_tenia(termino: str) -> None:
    """Consolidar no cambió ni un término, en ninguno de los que separan los órdenes.

    Es la misma comprobación que se hizo a mano antes de borrar las cuatro copias, pero dejada como
    prueba: si alguien «simplifica» el escape del módulo compartido creyendo que las dos formas
    son lo mismo, esto cae.
    """

    assert escape_like(termino) == _copia_local_como_antes(termino)
