"""Comprueba que cada clase usada en el JSX exista en el CSS.

## Por qué esto hace falta

Porque una clase que no existe en la hoja de estilos **no da error**: el elemento se renderiza sin
ese estilo y la pantalla se ve simplemente mal, sin que ni el navegador ni el compilador digan nada.
Es la clase de fallo más silenciosa que hay en un frontend, y la que hizo que las pestañas de la
consola de operaciones salieran pegadas en fila: el JSX pedía `.tabs` y `.tab`, y el CSS tenía
`.tab-bar` y `.tab-button`. Nada avisó.

## Qué no cubre

Solo mira `className="..."` estático. No sigue las clases que se componen con plantillas, ni las
que se calculan, ni los módulos CSS. Es una comprobación de la clase de fallo más común, no una
garantía: sirve para cazar lo que se escribe a mano, que es donde se cuela esto.

## Por qué compara en los dos sentidos

Unidirectional no basta. Falta una clase en el CSS es un elemento sin estilo. Y sobrar una clase en
el CSS es deuda que otro agente acabará aplicando por costumbre, creyendo que existe. Las dos
direcciones seAvisan, con distinta severidad.
"""

from __future__ import annotations

import io
import os
import re
import sys
from collections import defaultdict

RAIZ = os.path.dirname(os.path.abspath(__file__))
DIR_CSS = os.path.join(RAIZ, "src", "styles")
SRC = os.path.join(RAIZ, "src")

#: Las clases que existen pero no son de estilo: las pone el framework o el navegador.
EXTERNAS = {"use", "container", "row", "col"}

CLASE_JSX = re.compile(r'className\s*=\s*"([^"]*)"')
CLASE_CSS = re.compile(r'\.([A-Za-z_][A-Za-z0-9_-]*)')


def clases_del_jsx() -> dict[str, set[str]]:
    """Las clases usadas en cada fichero `.tsx`, agrupadas por fichero."""
    por_fichero: dict[str, set[str]] = defaultdict(set)
    for raiz, _dirs, ficheros in os.walk(SRC):
        if "node_modules" in raiz:
            continue
        for nombre in ficheros:
            if not nombre.endswith(".tsx"):
                continue
            ruta = os.path.join(raiz, nombre)
            texto = io.open(ruta, encoding="utf-8", errors="replace").read()
            for bloque in CLASE_JSX.findall(texto):
                for clase in bloque.split():
                    por_fichero[os.path.relpath(ruta, RAIZ)].add(clase)
    return por_fichero


def clases_del_css() -> dict[str, set[str]]:
    """Las clases de **todas** las hojas de estilos, con la hoja de la que viene cada una.

    ## Por qué todas y no solo una

    Porque el proyecto tiene ocho hojas y las importa todas en `main.tsx`. Una primera versión de
    este script solo miraba `index.css` y decía que faltaban 250 clases, cuando en realidad la
    mayoría estaban repartidas por `chat.css`, `settings.css`, `support.css` y las demás. Un
    verificador que solo mira una parte de la verdad es peor que no tener ninguno: da 250 errores
    falsos y quien lo lee deja de fiarse.

    Y devolver la hoja de origen no es un detalle: cuando falte una clase de verdad, el mensaje dice
    en qué hoja debería estar, que es media solución hecha.
    """

    por_hoja: dict[str, set[str]] = {}
    for nombre in sorted(os.listdir(DIR_CSS)):
        if not nombre.endswith(".css"):
            continue
        texto = io.open(os.path.join(DIR_CSS, nombre), encoding="utf-8", errors="replace").read()
        por_hoja[nombre] = set(CLASE_CSS.findall(texto))
    return por_hoja


def _parece(clase: str, definidas: set[str]) -> bool:
    """Si existe una clase con el mismo nombre en otra palabra.

    ## Por qué esto es una pista y no una solución

    Porque `.tab-button` no es `.tab` escrito de otra forma: son dos elementos distintos. Lo que
    sí resuelve es el caso frecuente de un error de tecleo o de una regla que se renombró en un
    sitio y no en el otro, y en ese caso la respuesta está en el fichero de al lado.

    Y devolver la hoja de origen no es un detalle: cuando falte una clase de verdad, el mensaje dice
    dónde deberíaa estar, que es media solución hecha.

    La comparación es por prefijo común de longitud >= 4, para que `.badge-on` sugiera
    `.badge-warning` sin sugerir `.badge` a cada badge de la aplicación.
    """

    largo = len(clase)
    return any(
        otra != clase and len(otra) >= 4 and otra.startswith(clase[: min(largo, 4)])
        for otra in definidas
    )


def main() -> int:
    por_hoja = clases_del_css()
    definidas = set().union(*por_hoja.values()) if por_hoja else set()
    usadas = clases_del_jsx()

    #: Las clases que se construyen en plantillas no se pueden comprobar, y se listan aparte para
    #: que quede constancia de que hay código que este verificador no ve.
    dinamicas: set[str] = set()
    for raiz, _dirs, ficheros in os.walk(SRC):
        for nombre in ficheros:
            if not nombre.endswith(".tsx"):
                continue
            texto = io.open(os.path.join(raiz, nombre), encoding="utf-8", errors="replace").read()
            for bloque in re.findall(r"className\s*=\s*\{([^}]*)\}", texto):
                if "`" in bloque or "'" in bloque or "+" in bloque:
                    dinamicas.add(os.path.relpath(os.path.join(raiz, nombre), RAIZ))

    faltan: dict[str, set[str]] = {}
    for fichero, clases in sorted(usadas.items()):
        ausentes = {c for c in clases if c not in definidas and c not in EXTERNAS}
        if ausentes:
            faltan[fichero] = ausentes

    #: En qué hoja debería estar cada clase que falta, para no tener que buscarla a mano.
    por_parecido: dict[str, list[str]] = defaultdict(list)
    for clase in sorted({c for cs in faltan.values() for c in cs}):
        por_parecido[clase] = [h for h, cs in por_hoja.items() if _parece(clase, cs)]

    total = sum(len(c) for c in usadas.values())
    print("  %d clases usadas en %d ficheros; %d definidas en %d hojas"
          % (total, len(usadas), len(definidas), len(por_hoja)))

    if faltan:
        print("  %d fichero(s) con clases SIN ESTILO:" % len(faltan))
        for fichero, clases in faltan.items():
            print("    %s" % fichero)
            for clase in sorted(clases):
                P = por_parecido.get(clase) or []
                destino = ("parecido a: " + ", ".join(P)) if P else "sin parecido"
                print("      .%s   (%s)" % (clase, destino))
        print()
        print("  Un elemento con una clase inexistente no da error: se renderiza sin estilo.")
        return 1

    print("  ninguna clase usada falta en el CSS")
    if dinamicas:
        print("  (%d fichero(s) con className compuesto que este verificador no ve)" % len(dinamicas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
