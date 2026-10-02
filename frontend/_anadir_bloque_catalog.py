"""Añade a `knowledge.json` el bloque `catalog`, que el catálogo técnico usa y que no existía.

## Qué estaba roto

`KnowledgeCatalogView` y `KnowledgePage` piden sus textos con prefijo `catalog.` —`catalog.loading`,
`catalog.searchPlaceholder`, `catalog.severities.CRITICAL`— dentro del namespace `knowledge`. Pero
`knowledge.json` no tenía ninguna clave `catalog`: sus bloques de raíz son `category`, `detail`,
`documents`, `filters`, `okf`, `states`, `views`. Resultado: **14 textos pintados literalmente** en
la pestaña «Catálogo técnico» —`catalog.searchPlaceholder` en el buscador, `catalog.severities.HIGH`
en los botones de filtro— en los dos idiomas.

Por qué nadie lo vio: el gate `i18n` del proyecto compara `es` contra `en`, y como la clave faltaba
en los dos, los dos estaban de acuerdo. `_i18n_check.py` tampoco, porque recorre una lista fija de
nueve pantallas de la consola y esta no está en ella. Dos capas de comprobación, dos capas ciegas a
lo mismo.

## Por qué se añaden las claves y no se cambia el prefijo del componente

Porque `documents.*` y `catalog.*` son las dos vistas de la misma página y conviene que se lean
así. `/knowledge` tiene documentos del workspace y catálogo técnico: son datos distintos, con
filtros distintos, y el código ya los separa en dos componentes. Renombrar 14 llamadas a `t()` para
quitarles el prefijo deja las dos vistas con sintaxis distinta —una con prefijo y otra sin él— y no
arregla nada que estuviera roto. Añadir el bloque es lo que hace el componente legible: el prefijo
`catalog.` documenta en la propia línea de qué vista es el texto.

## De dónde sale cada traducción

Las severidades y categorías ya existen traducidas en la raíz, en `category.*` y en el bloque que
usa la tabla. Se **copian de ahí** en lugar de redactarlas: son las mismas palabras, y copiar evita
que el mismo concepto tenga dos nombres distintos en dos sitios de la misma pantalla.

El resto —botones de filtro, estados de carga, cuenta de resultados— es redacción nueva siguiendo el
tono del resto del fichero, sin tildes en los valores como el resto del proyecto.
"""

from __future__ import annotations

import io
import json
import os
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))

#: Textos propios de la vista, en ambos idiomas. Los que dependen de un valor del servidor —
#: severidades y categorías— no se declaran aquí: se copian de los bloques que ya los tenían, en
#: `principal()`, para que no puedan divergir.
PROPIOS: dict[str, dict[str, str]] = {
    "es": {
        "searchPlaceholder": "Buscar en el catalogo",
        "severityLabel": "Severidad",
        "categoryLabel": "Familia de fallo",
        "allCategories": "Todas",
        "clearFilters": "Quitar filtros",
        "refresh": "Actualizar",
        "loading": "Cargando el catalogo...",
        "loadFailed": "No se pudo cargar el catalogo.",
        "retry": "Reintentar",
        "empty": "El catalogo esta vacio.",
        "emptyFiltered": "Ningun apunte coincide con los filtros.",
        "count": "{{shown}} de {{total}} apuntes",
    },
    "en": {
        "searchPlaceholder": "Search the catalog",
        "severityLabel": "Severity",
        "categoryLabel": "Vulnerability family",
        "allCategories": "All",
        "clearFilters": "Clear filters",
        "refresh": "Refresh",
        "loading": "Loading the catalog...",
        "loadFailed": "The catalog could not be loaded.",
        "retry": "Try again",
        "empty": "The catalog is empty.",
        "emptyFiltered": "No note matches the filters.",
        "count": "{{shown}} of {{total}} notes",
    },
}

#: Cuántas claves deben quedar en el bloque `catalog` de cada idioma.
ESPERADAS = 14


def principal() -> int:
    fallos: list[str] = []

    for lang, propios in PROPIOS.items():
        ruta = os.path.join(RAIZ, "src", "locales", lang, "knowledge.json")
        with io.open(ruta, encoding="utf-8") as fh:
            datos = json.load(fh)

        severidades = _severidades(RAIZ, lang)
        categorias = dict(datos.get("category", {}))

        if not severidades:
            fallos.append("%s: no se encontro donde estan traducidas las severidades" % lang)
            continue
        if not categorias:
            fallos.append("%s: no se encontro el bloque de categorias" % lang)
            continue

        catalogo: dict[str, object] = {}
        catalogo.update(propios)
        catalogo["severities"] = severidades
        catalogo["categories"] = categorias
        datos["catalog"] = catalogo

        _copiar_etiquetas_llm(RAIZ, lang)

        with io.open(ruta, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(datos, ensure_ascii=False, indent=2) + "\n")

        with io.open(ruta, encoding="utf-8") as fh:
            recarga = json.load(fh)
        total = len(recarga.get("catalog", {}))
        if total != ESPERADAS:
            fallos.append("%s: %d claves en catalog, se esperaban %d" % (lang, total, ESPERADAS))
        print(
            "  %s: bloque catalog con %d claves (%d severidades, %d categorias)"
            % (lang, total, len(severidades), len(categorias))
        )

    if fallos:
        print()
        for linea in fallos:
            print("  FALLO %s" % linea)
        return 1
    return 0


CLAVES_SEVERIDAD = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def _severidades(raiz: str, lang: str) -> dict[str, object]:
    """Las severidades ya traducidas, de donde ya estén.

    ## Por qué en otro fichero y no aquí

    Porque en `knowledge.json` **no** están: los bloques de raíz son `category`, `detail`,
    `documents`, `filters`, `okf`, `states`, `views`, y las severidades viven en `cve.json`
    (`severity`) y en `issues.json` (`severityCounts`). Copy-paste de la traducción de otro sitio es
    preferible a redactar cuatro palabras nuevas: son el mismo concepto, y dos redacciones
    distintas para la misma severidad en la misma pantalla es un defecto que se ve.

    Se recorren **todos** los ficheros del idioma en orden alfabético y se coge el primer bloque
    con las cuatro claves. Es determinista, y si mañana se añade otro sitio con las mismas claves
    pero mejor traducidas, el cambio sale en el informe y se revisa a mano en lugar de cambiar de
    golpe sin que nadie lo note.
    """
    carpeta = os.path.join(raiz, "src", "locales", lang)
    for nombre in sorted(os.listdir(carpeta)):
        if not nombre.endswith(".json"):
            continue
        datos = json.load(io.open(os.path.join(carpeta, nombre), encoding="utf-8"))
        for bloque in _recorrer(datos):
            if all(clave in bloque for clave in CLAVES_SEVERIDAD):
                return {clave: bloque[clave] for clave in CLAVES_SEVERIDAD}
    return {}


def _recorrer(nodo: object) -> list[dict]:
    """Todos los diccionarios del árbol, del más exterior al más interior."""
    salida: list[dict] = []
    if isinstance(nodo, dict):
        salida.append(nodo)
        for valor in nodo.values():
            salida.extend(_recorrer(valor))
    return salida


if __name__ == "__main__":
    sys.exit(principal())
