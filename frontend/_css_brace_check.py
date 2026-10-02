"""Comprueba que las llaves de cada hoja de estilos cuadran.

## Por qu\u00e9 hace falta

Porque un CSS al que le falta una llave de cierre **no da error de sintaxis**: el parser se come la
regla siguiente como si fuera parte de la anterior, y el resultado es que una regla entera no se
aplica sin decir nada. Pas\u00f3 dos veces en una sesi\u00f3n con parches programados sobre
`index.css`, y las dos veces el sintoma fue \u00abun bot\u00f3n ha dejado de tener estilo\u00bb, que no
apunta al fichero del error.

Adem\u00e1s `_css_comment_check.py` no lo habr\u00eda visto: aquel busca un `/*` perdido, y esto es una
llave perdida.

## Qu\u00e9 hace

Recorre la hoja ignorando los comentarios y cuenta la profundidad de llaves. Si no vuelve a cero,
dice qu\u00e9 selectores han quedado sin cerrar y en qu\u00e9 l\u00ednea empez\u00f3 cada uno.
"""

from __future__ import annotations

import io
import os
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
DIR_CSS = os.path.join(RAIZ, "src", "styles")


def revisar(ruta: str) -> tuple[int, list[tuple[int, str]]]:
    texto = io.open(ruta, encoding="utf-8", errors="replace").read().replace("\\r\\n", "\\n")
    profundidad = 0
    pila: list[tuple[int, str]] = []
    for n, linea in enumerate(texto.split("\n"), 1):
        limpio = linea
        i = 0
        while i < len(limpio):
            if limpio[i : i + 2] == "/*":
                fin = limpio.find("*/", i + 2)
                if fin < 0:
                    limpio = limpio[:i]
                    break
                limpio = limpio[:i] + limpio[fin + 2 :]
                i += 2
                continue
            if limpio[i : i + 2] == "*/":
                limpio = limpio[:i] + limpio[i + 2 :]
                i += 2
                continue
            if limpio[i] == "{":
                profundidad += 1
                pila.append((n, limpio[: i + 1].strip()[-70:]))
            elif limpio[i] == "}":
                profundidad -= 1
                if pila:
                    pila.pop()
            i += 1
    return profundidad, pila


def main() -> int:
    problemas = 0
    for nombre in sorted(os.listdir(DIR_CSS)):
        if not nombre.endswith(".css"):
            continue
        profundidad, pila = revisar(os.path.join(DIR_CSS, nombre))
        if profundidad != 0:
            problemas += 1
            print("  %s: faltan %d llave(s) de cierre" % (nombre, profundidad))
            for n, sel in pila:
                print("    L%d sin cerrar  %s" % (n, sel))
    if problemas:
        print()
        print("  Una llave sin cerrar hace que la regla siguiente se trague como cuerpo, y el")
        print("  sintoma aparece en otro elemento del que se acaba de tocar.")
        return 1
    print("  todas las llaves cuadran en las hojas de estilos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
