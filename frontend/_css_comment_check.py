"""Comprueba que no haya comentarios CSS sin abrir.

## Por qué hace falta

Porque un `/*` que se pierde **no da error**. El parser toma la línea siguiente como preludio de una
regla, se come hasta el `*/` que ya no le corresponde y descarta la declaración. El resultado es una
regla muerta que en el código se lee como si estuviera puesta y en la pantalla no está.

Pasó de verdad: en `index.css` faltaba el `/*` de apertura de un bloque numerado, y se llevaban por
delante cinco reglas, entre ellas `.console-row-actions`. Esa clase es la que da caja a los botones
de fila de la consola, así que los cuatro botones de cada cliente salían como enlaces subrayados
en lugar de como botones. El código decía lo contrario.

## Qué detecta

Una línea que abre un elemento de lista numerada (`2. Título`) sin que antes haya un `/*` en el
mismo bloque de comentario. Es el patrón exacto que perdió el bloque, así que la búsqueda es
estrecha a propósito en lugar de un analizador de CSS.
"""

from __future__ import annotations

import io
import os
import re
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
DIR_CSS = os.path.join(DIR, "src", "styles")

NUMERADA = re.compile(r"^\s+\d+\.\s+\S")


def revisar(ruta: str) -> list[tuple[int, str]]:
    """Las líneas numeradas que no tienen un `/*` que las sostenga."""
    texto = io.open(ruta, encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    lineas = texto.split("\n")
    en_comentario = False
    sospechosos: list[tuple[int, str]] = []
    for n, linea in enumerate(lineas, 1):
        if not en_comentario and NUMERADA.match(linea):
            sospechosos.append((n, linea.strip()[:70]))
        abre = linea.count("/*")
        cierra = linea.count("*/")
        if abre > cierra:
            en_comentario = True
        elif cierra > abre:
            en_comentario = False
    return sospechosos


def main() -> int:
    problemas = 0
    for nombre in sorted(os.listdir(DIR_CSS)):
        if not nombre.endswith(".css"):
            continue
        sospechosos = revisar(os.path.join(DIR_CSS, nombre))
        if sospechosos:
            problemas += len(sospechosos)
            print("  %s: %d linea(s) sin `/*` que las sostenga:" % (nombre, len(sospechosos)))
            for n, linea in sospechosos:
                print("    %5d  %s" % (n, linea))
    if problemas:
        print()
        print("  Un `/*` perdido deja reglas muertas sin decir nada.")
        return 1
    print("  ningun comentario CSS sin abrir")
    return 0


if __name__ == "__main__":
    sys.exit(main())
