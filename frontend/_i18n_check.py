"""Comprueba que cada clave de traducción usada en el código exista en los dos idiomas.

## Qué resuelve y por qué no basta con comparar es contra en

Porque el gate de paridad del proyecto (`scripts/ci_i18n_check.py`) compara `es` **contra** `en`:
comprueba que las dos hojas tengan las mismas claves. Eso detecta una clave traducida en un idioma
y no en el otro, y no detecta el fallo más común: **una clave que no existe en ninguno de los dos**.

Se comprobó: borrando `tenants.pricing.title` de `es` y de `en` a la vez, el gate del proyecto
responde «Paridad completa» y sale con código 0, mientras la pantalla enseña literalmente
`tenants.pricing.title` en lugar del título. Nada en la cadena de herramientas lo detecta.

Este script compara **el código contra las hojas**, que es el otro lado de la comparación.

## Por qué prueba en todos los namespaces y no en uno

Porque cada pantalla usa el suyo: las de la consola usan `admin`, la de tickets usa `support`, la
de modelos usa `llm`. Buscar solo en `admin` daba 54 falsos positivos en la página de tickets.
Resolver en todos no dice *en cuál* habría que añadir la clave cuando falta, pero no mintió nunca, y
para lo que importa — ¿existe esta clave? — es la respuesta exacta.

## Qué no cubre

Solo mira el `t('clave')` estático. Las claves que se componen con plantillas
(`t(\`pricing.scalars.fields.${x}\`)`) no se pueden comprobar sin montar React, y quedan fuera.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(RAIZ, "src")
LOCALES = os.path.join(SRC, "locales")
IDIOMAS = ("es", "en")

#: Pantallas de la consola, que es lo que esta ronda de rediseño toca.
#:
#: ## Por qué esta lista está escrita a mano y por qué es un problema
#:
#: Porque una lista de ficheros es una lista que hay que recordar ampliar, y se queda corta
#: justo cuando hace falta. `KnowledgeCatalogView` no estaba aquí y por eso sus catorce claves
#: `catalog.*` —que el componente pide y el JSON no tenía— llevaba tiempo sin existir sin que
#: ninguna comprobación lo dijera: el gate `i18n` compara es contra en y los dos estaban de
#: acuerdo, y este script no miraba el fichero.
#:
#: Por eso ahora la lista **no se amplía a mano**: `main()` recorre además todo `src/features` y
#: avisa de los ficheros que no están en la lista, para que la Ampliación sea una decisión y no un
#: olvido. Lo que hay aquí es lo que se comprueba con mensaje detallado por pantalla; lo demás
#: entra por el barrido.
PANTALLAS = [
    "src/features/admin/AdminTenantPricingPage.tsx",
    "src/features/admin/AdminTenantsPage.tsx",
    "src/features/admin/AdminPricingPage.tsx",
    "src/features/admin/AdminOperationsPage.tsx",
    "src/features/admin/AdminTicketsPage.tsx",
    "src/features/admin/AdminUsersPage.tsx",
    "src/features/admin/AdminSalesPage.tsx",
    "src/features/admin/AdminAuditPage.tsx",
    "src/features/admin/AdminAgentsPage.tsx",
]

#: La llamada es `t(...)` y cualquier alias suyo (`tAdmin(...)`, `tCommon(...)`). El alias se
#: acepta porque una hoja puede tener la misma clave en dos namespaces y el nombre de la función no
#: dice cuál se está usando; para la pregunta "¿existe?", probarlos todos es la respuesta.
LLAMADA = re.compile(r"\bt[A-Za-z]*\(\s*['\"]([a-zA-Z0-9_.]+)['\"]")

SUFIJOS_PLURAL = ("_one", "_other", "_zero", "_two", "_few", "_many")


def resolver(datos: dict, clave: str) -> bool:
    nodo: object = datos
    for parte in clave.split("."):
        if not isinstance(nodo, dict) or parte not in nodo:
            return False
        nodo = nodo[parte]
    return True


def existe(alguna: list[dict], clave: str) -> bool:
    """Si la clave existe en algún namespace, o si es un plural de i18next.

    ## Por qué el plural va aparte

    Porque `t('tenants.members', { count })` no busca `tenants.members`: i18next busca
    `tenants.members_one` y `tenants.members_other` según el número. Una clave de plural bien
    escrita no existe jamás en singular, así que un verificador que mirara solo el nombre exacto
    marcaría como faltando precisamente las claves que están bien.
    """
    for datos in alguna:
        if resolver(datos, clave):
            return True
    return any(resolver(datos, clave + s) for datos in alguna for s in SUFIJOS_PLURAL)


def main() -> int:
    faltan: list[tuple[str, str]] = []
    total = 0

    for idioma in IDIOMAS:
        hojas: list[dict] = []
        carpeta = os.path.join(LOCALES, idioma)
        for nombre in sorted(os.listdir(carpeta)):
            if nombre.endswith(".json"):
                with io.open(os.path.join(carpeta, nombre), encoding="utf-8") as f:
                    hojas.append(json.load(f))

        for pantalla in PANTALLAS:
            ruta = os.path.join(RAIZ, pantalla)
            with io.open(ruta, encoding="utf-8", errors="replace") as f:
                texto = f.read()
            for clave in sorted(set(LLAMADA.findall(texto))):
                total += 1
                if not existe(hojas, clave):
                    faltan.append((idioma, f"{pantalla} -> {clave}"))

    print("  %d comprobaciones de clave literal en %d pantallas" % (total, len(PANTALLAS)))
    if faltan:
        print("  %d FALTAN:" % len(faltan))
        for idioma, clave in faltan:
            print("    [%s] %s" % (idioma, clave))
        print()
        print("  Una clave que falta en los dos idiomas no la ve el gate de paridad del proyecto,")
        print("  porque ese compara es contra en y los dos siguen estando de acuerdo.")
        return 1
    print("  ninguna falta: las claves existen en es y en")

    sin_revisar = _barrido_completo()
    if sin_revisar:
        print()
        print(
            "  %d fichero(s) de src/features NO estan en PANTALLAS, y por eso sus claves no se"
            % len(sin_revisar)
        )
        print("  han comprobado de forma detallada:")
        for ruta in sin_revisar:
            print("    %s" % ruta)
        print()
        print(
            "  Esto no es un fallo: es un aviso. La lista se ha escrito a mano y se queda corta."
        )
        print(
            "  Para no depender de que alguien la amplíe, `_que_claves_faltan.py` acepta cualquier"
        )
        print("  ruta y responde en un segundo. No es un gate porque depende de que lo ejecuten.")
    return 0


def _barrido_completo() -> list[str]:
    """Los `.tsx` de `src/features` que no están en la lista de pantallas.

    Solo mira si el fichero usa `t(`, para no ensuciar la salida con fifty componentes de presentación
    que no traducen nada.
    """
    raiz = os.path.join(SRC, "features")
    conocidas = {os.path.normpath(p) for p in PANTALLAS}
    fuera: list[str] = []
    for base, _dirs, ficheros in os.walk(raiz):
        if "node_modules" in base:
            continue
        for nombre in sorted(ficheros):
            if not nombre.endswith(".tsx"):
                continue
            completa = os.path.join(base, nombre)
            relativa = os.path.normpath(os.path.join("src/features", os.path.relpath(completa, raiz)))
            if relativa in conocidas:
                continue
            texto = io.open(completa, encoding="utf-8", errors="replace").read()
            if LLAMADA.search(texto):
                fuera.append(relativa.replace("\\", "/"))
    return fuera


if __name__ == "__main__":
    sys.exit(main())
