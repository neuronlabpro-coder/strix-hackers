#!/usr/bin/env python3
"""Comprueba que `locales/es/` y `locales/en/` están en paridad.

## Por qué compara **hojas** y no claves de primer nivel

Porque las traducciones están anidadas. Comparar el primer nivel daría por bueno un
namespace donde `es` tiene `detalle.parche.titulo` y `en` no tiene ese nivel: el recuento
cuadraría y el panel mostraría la clave en crudo.

## Por qué compara los marcadores de interpolación

Porque es el fallo que no se ve en una revisión. Si la versión española de una cadena usa
`{{count}}` y la inglesa usa `{{total}}`, ambas están "traducidas" y el panel funciona en un
idioma y enseña la llave literal en el otro. `i18next` no avisa: sustituye lo que encuentra y
deja el resto.

## Por qué no importa nada del proyecto

Porque se ejecuta **por separado** del backend y antes de nada, para que un fallo de
dependencia no impida ver un problema de traducciones. Solo usa la biblioteca estándar.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent
LOCALES = RAIZ / "frontend" / "src" / "locales"

#: Marcadores de interpolación de i18next. Se buscan con `{{nombre}}` y no con `{nombre}`
#: porque las llaves simples son sintaxis de JavaScript y aparecen en el contenido de otras
#: formas; las dobles son exclusivamente de i18next.
MARCADOR = re.compile(r"\{\{(\w+)")

#: Cuántas claves se imprimen de cada lista antes de resumir el resto. Imprimir 400 líneas de
#: diferencias no ayuda a nadie: se imprime lo que hay y un "(y N más)" con el total, que es
#: la información que hace falta para decidir si es una entrada mal puesta o un namespace
#: entero sin traducir.
MAXIMO_DETALLADO = 25


def hojas(nodo: dict[str, Any], prefijo: str = "") -> dict[str, str]:
    """Aplana el diccionario hasta sus valores escalares.

    La clave resultante es la ruta con puntos, que es exactamente la que usa `t()`. Comparar
    estas rutas es lo que hace que el resultado de la comprobación sea accionable: quien
    lee el fallo sabe qué clave falta y en qué fichero.
    """

    salida: dict[str, str] = {}
    for clave, valor in nodo.items():
        ruta = f"{prefijo}{clave}"
        if isinstance(valor, dict):
            salida.update(hojas(valor, f"{ruta}."))
        else:
            salida[ruta] = valor if isinstance(valor, str) else json.dumps(valor)
    return salida


def cargar(idioma: str) -> dict[str, dict[str, str]]:
    carpeta = LOCALES / idioma
    if not carpeta.is_dir():
        print(f"No existe el directorio de traducciones: {carpeta}")
        raise SystemExit(2)
    resultado: dict[str, dict[str, str]] = {}
    for ruta in sorted(carpeta.glob("*.json")):
        try:
            with ruta.open(encoding="utf-8") as fichero:
                resultado[ruta.name] = hojas(json.load(fichero))
        except json.JSONDecodeError as error:
            print(f"{ruta.name} no es JSON valido: {error}")
            raise SystemExit(2) from error
    return resultado


def comprobar() -> int:
    es = cargar("es")
    en = cargar("en")

    problemas = 0

    solo_es = set(es) - set(en)
    solo_en = set(en) - set(es)
    if solo_es or solo_en:
        problemas += 1
        if solo_es:
            print(f"Namespaces presentes solo en es: {', '.join(sorted(solo_es))}")
        if solo_en:
            print(f"Namespaces presentes solo en en: {', '.join(sorted(solo_en))}")

    for nombre in sorted(set(es) & set(en)):
        claves_es = es[nombre]
        claves_en = en[nombre]

        faltan_en = sorted(set(claves_es) - set(claves_en))
        faltan_es = sorted(set(claves_en) - set(claves_es))
        if faltan_en:
            problemas += 1
            print(f"\n{nombre}: {len(faltan_en)} clave(s) solo en es")
            for clave in faltan_en[:MAXIMO_DETALLADO]:
                print(f"  - {clave}")
            if len(faltan_en) > MAXIMO_DETALLADO:
                print(f"  ... y {len(faltan_en) - MAXIMO_DETALLADO} mas")
        if faltan_es:
            problemas += 1
            print(f"\n{nombre}: {len(faltan_es)} clave(s) solo en en")
            for clave in faltan_es[:MAXIMO_DETALLADO]:
                print(f"  - {clave}")
            if len(faltan_es) > MAXIMO_DETALLADO:
                print(f"  ... y {len(faltan_es) - MAXIMO_DETALLADO} mas")

        for clave in sorted(set(claves_es) & set(claves_en)):
            marcadores_es = set(MARCADOR.findall(claves_es[clave]))
            marcadores_en = set(MARCADOR.findall(claves_en[clave]))
            if marcadores_es != marcadores_en:
                problemas += 1
                print(
                    f"\n{nombre}:{clave} marcadores distintos "
                    f"es={sorted(marcadores_es)} en={sorted(marcadores_en)}"
                )

    total_es = sum(len(v) for v in es.values())
    total_en = sum(len(v) for v in en.values())
    if total_es != total_en:
        print(f"\nRecuento distinto: es={total_es} en={total_en}")
        problemas += 1

    print(f"\n{len(es)} namespaces, {total_es} hojas en es y {total_en} en en.")
    if problemas:
        print(f"{problemas} problema(s) de paridad.")
        return 1
    print("Paridad completa.")
    return 0


if __name__ == "__main__":
    sys.exit(comprobar())
