"""Comprueba que las claves que el codigo construye en runtime existan en los dos idiomas.

## Por qué hace falta, y por qué `_i18n_check.py` no lo cubre

Porque `_i18n_check.py` lee las llamadas `t('clave')` con la clave **escrita a mano**. Y hay
llamadas donde la clave se arma con una interpolación:

```tsx
t(`tenants.pricing.operations.${pactado.operacion}`)
```

Ahí el verificador literal no ve nada, porque en el fuente no aparece una clave completa: aparece
un prefijo y una variable. Ese es justo el punto donde una clave se puede perder sin que nada se
entere, y fue lo que pasó con `tenants.pricing.states.superseded`: la cuarta insignia de la cadena
de pactados se pide por un mapa de `Record<EstadoPactado, string>`, así que ninguna comprobación de
literales la señala aunque falte.

## Qué hace

Recoge los prefijos dinámicos del código y comprueba que el bloque al que apuntan existe en `es` y
en `en`. Bloque, no clave: el valor concreto depende de lo que venga del servidor —el enum de
operaciones, el estado de cada pactado—, y eso no se puede saber leyendo el JSON. Lo que sí se puede
comprobar es que el sitio donde se busca el texto existe y no está vacío.

## Qué NO cubre

No comprueba que exista una clave para **cada** valor posible de la variable. Eso exigiría conocer
el enum del backend y cruzarlo con el JSON, que es un gate distinto. Aquí se cubre el fallo de que
el bloque entero falte, que es el que produce una pantalla con la clave pintada literalmente.
"""

from __future__ import annotations

import io
import os
import re
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(RAIZ, "src")

#: Las cadenas con `${` dentro, que son las que se arman en runtime.
DINAMICA = re.compile(r"[\"'`]([a-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)\.[^`'\"]*\$\{")

IDIOMAS = ("es", "en")


def prefijos_dinamicos() -> set[str]:
    """Los prefijos `a.b.c` que el código completa con una interpolación."""
    vistos: set[str] = set()
    for raiz, _dirs, ficheros in os.walk(SRC):
        if "node_modules" in raiz:
            continue
        for nombre in ficheros:
            if not nombre.endswith((".tsx", ".ts")):
                continue
            texto = io.open(os.path.join(raiz, nombre), encoding="utf-8", errors="replace").read()
            for encontrado in DINAMICA.findall(texto):
                vistos.add(encontrado)
    return vistos


#: Cada namespace es un fichero JSON: `admin.json` contiene `operations.*` y `tenants.pricing.*`,
#: pero ninguno de los dos vive en la raiz del fichero. Sin esta tabla, `cargar` buscaría
#: `operations.states` en `admin.json` por su primer segmento —`operations`—, lo encontraría en
#: `tenants.pricing` y daría un OK falso para casi todo.
#
#: Se deduce del nombre de la propia clave: si el primer segmento no es un fichero, se busca el
#: fichero que contenga ese segmento. Es una heurística, y por eso el script **solo avisa**: un
#: FALTA aquí puede ser una clave mal construida o una heurística que no aplica, y en los dos casos
#: la respuesta es revisarla a mano, no confiar en el semáforo.
NAMESPACE = {
    "operations": "admin",
    "tenants": "admin",
    "pricing": "admin",
    "catalog": "knowledge",
    "documents": "knowledge",
    "okfEditor": "knowledge",
    "issues": "issues",
    "agent": "agents",
    "audit": "admin",
    "health": "admin",
    "sales": "admin",
    "scopes": "admin",
    "organizations": "admin",
    "setup": "agents",
    "create": "pentests",
    "code": "integrations",
    "notifications": "settings",
    "infrastructure": "settings",
}


def _fichero(primer_segmento: str) -> str | None:
    """Qué fichero JSON contiene una clave que empieza por `primer_segmento`.

    Se comprueba en los dos sitios posibles: un namespace con su propio fichero
    —`knowledge.json` para `catalog.*`— o dentro de un fichero temático —`admin.json` para
    `operations.*`. Se devuelve el que exista, y `None` si no se encuentra ninguno, que es lo que
    ## convierte el fallo en "no se pudo resolver" en vez de "no existe".
    """
    candidatos = []
    directo = os.path.join(RAIZ, "src", "locales", "%s.json" % primer_segmento)
    if os.path.exists(directo):
        candidatos.append(primer_segmento)
    por_mapa = NAMESPACE.get(primer_segmento)
    if por_mapa and por_mapa not in candidatos:
        candidatos.append(por_mapa)
    return candidatos[0] if candidatos else None


def cargar(idioma: str, clave: str) -> tuple[object, bool]:
    """El valor de `clave` —con puntos— y si se pudo resolver el fichero que la contiene.

    El segundo elemento de la tupla existe para distinguir "el bloque no existe" de "no supe qué
    fichero mirar". Lo segundo no es un fallo de las traducciones, es una limitación de este
    script, y mezclarlos haria que un `OK` de aqui significara en realidad una renuncia.
    """
    import json

    partes = clave.split(".")
    primero = partes[0]
    archivo = _fichero(primero)
    if archivo is None:
        return None, False
    ruta = os.path.join(RAIZ, "src", "locales", idioma, "%s.json" % archivo)
    datos = json.load(io.open(ruta, encoding="utf-8"))
    actual: object = datos
    for parte in partes:
        if not isinstance(actual, dict) or parte not in actual:
            return None, True
        actual = actual[parte]
    return actual, True


def principal() -> int:
    prefijos = prefijos_dinamicos()
    if not prefijos:
        print("  ninguna clave construida en runtime")
        return 0

    faltan: list[str] = []
    vacios: list[str] = []
    sin_resolver: list[str] = []
    for prefijo in sorted(prefijos):
        for idioma in IDIOMAS:
            valor, resuelto = cargar(idioma, prefijo)
            if not resuelto:
                sin_resolver.append("[%s] %s" % (idioma, prefijo))
            elif valor is None:
                faltan.append("[%s] %s" % (idioma, prefijo))
            elif isinstance(valor, dict) and not valor:
                vacios.append("[%s] %s" % (idioma, prefijo))

    print(
        "  %d prefijo(s) construido(s) en runtime, comprobados en %s"
        % (len(prefijos), " y ".join(IDIOMAS))
    )

    if faltan or vacios:
        for linea in faltan:
            print("    FALTA   %s" % linea)
        for linea in vacios:
            print("    VACIO   %s" % linea)
        print()
        print("  Una clave que se arma con `${}` no la ve ningun verificador de literales.")
        return 1

    if sin_resolver:
        # No es un fallo: es que el script no sabe en que fichero vive ese prefijo. Se avisa igual
        # para que quede escrito, y el gate pasa porque no hay ningun problema demostrado.
        print("  %d prefijo(s) fuera del mapa de namespaces: este script no los ha comprobado" % len(set(sin_resolver)))
        for linea in dict.fromkeys(sin_resolver):
            print("    SIN REVISAR  %s" % linea)

    print("  todos los comprobados existen y ninguno esta vacio")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
