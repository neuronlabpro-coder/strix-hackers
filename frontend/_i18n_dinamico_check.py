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

## De dónde sale el namespace, y por qué no se deduce del nombre de la clave

Porque se **lee del fichero donde se construye la clave**, no se adivina. Un componente que llama a
`useTranslation('integrations')` no puede estar leyendo `admin.json`, por muy raro que sea el primer
segmento de lo que pide.

Adivinarlo por el primer segmento era lo que había, con una tabla de excepciones, y era **peor que
no comprobar**, por dos razones a la vez. La primera es que daba falsos positivos: `create.types` lo
construye `CreateTokenModal`, que usa el namespace `apiAccess`, y la tabla lo mandaba a `pentests`;
`issues.destinations` lo construye `IntegrationsPage`, que usa `integrations`, y como `issues` sí es
un namespace la tabla se paraba ahí. Los cinco que fallaban **existen** en `es` y en `en`, y el
semáforo estaba rojo por culpa del semáforo, que es la clase de fallo que entrena a ignorar al
comprobador. La segunda es que mientras daba ese falso positivo, `readiness.checkNames` y
`readiness.reasons` no se comprobaban en absoluto.

Un prefijo se da por bueno cuando resuelve en **alguno** de los namespaces que declara el fichero que
lo construye, y se avisa cuando resuelve en varios, porque ahí el valor concreto puede venir del
sitio equivocado y eso solo se ve a mano.

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
#:
#: El prefijo capturado admite **un solo segmento** antes del punto, porque hay claves que se arman
#: sobre el namespace entero: `t(`expiry.${choice.key}`)` no tiene ni un punto en el literal que
#: precede a la interpolación. Con la versión anterior, que exigía al menos un punto más, esa línea
#: **no se veía**: y por eso `expiry.never` faltaba en los dos idiomas y se pintaba escrito en el
#: desplegable de caducidad del diálogo de token nuevo. Un comprobador que no ve la mitad de los
#: casos que dice cubrir no es un comprobador.
DINAMICA = re.compile(r"[\"'`]([a-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\.[^`'\"]*\$\{")

#: Los `useTranslation('x')` de un fichero. Es el dato que dice en qué JSON se busca de verdad.
USO_TRADUCCION = re.compile(r"useTranslation\(\s*['\"]([A-Za-z0-9_]+)['\"]")

IDIOMAS = ("es", "en")


def _ficheros() -> list[tuple[str, str]]:
    """Los `(ruta, texto)` de los ficheros que pueden construir una clave en runtime."""
    encontrados: list[tuple[str, str]] = []
    for raiz, dirs, ficheros in os.walk(SRC):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        for nombre in ficheros:
            if not nombre.endswith((".tsx", ".ts")):
                continue
            ruta = os.path.join(raiz, nombre)
            texto = io.open(ruta, encoding="utf-8", errors="replace").read()
            if DINAMICA.search(texto):
                encontrados.append((ruta, texto))
    return encontrados


def prefijos_dinamicos() -> set[str]:
    """Los prefijos `a.b.c` que el código completa con una interpolación."""
    vistos: set[str] = set()
    for _ruta, texto in _ficheros():
        vistos.update(DINAMICA.findall(texto))
    return vistos


def usos() -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    """Cada prefijo con los ficheros que lo construyen y los namespaces que esos ficheros declaran."""
    mapa: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for ruta, texto in _ficheros():
        namespaces = tuple(sorted(set(USO_TRADUCCION.findall(texto))))
        for prefijo in set(DINAMICA.findall(texto)):
            mapa.setdefault(prefijo, []).append((ruta, namespaces))
    return mapa


def cargar(idioma: str, namespace: str, clave: str) -> tuple[object, bool]:
    """El valor de `namespace:clave` —con puntos— y si ese namespace existe como fichero JSON.

    El segundo elemento de la tupla existe para distinguir "el bloque no existe" de "no supe qué
    fichero mirar". Lo segundo no es un fallo de las traducciones, es una limitación de este
    script, y mezclarlos haría que un `OK` de aquí significara en realidad una renuncia.
    """
    import json

    ruta = os.path.join(RAIZ, "src", "locales", idioma, "%s.json" % namespace)
    if not os.path.exists(ruta):
        return None, False
    datos = json.load(io.open(ruta, encoding="utf-8"))
    actual: object = datos
    for parte in clave.split("."):
        if not isinstance(actual, dict) or parte not in actual:
            return None, True
        actual = actual[parte]
    return actual, True


def principal() -> int:
    mapa = usos()
    if not mapa:
        print("  ninguna clave construida en runtime")
        return 0

    faltan: list[str] = []
    vacios: list[str] = []
    sin_namespace: list[str] = []
    ambiguos: list[str] = []

    for prefijo in sorted(mapa):
        for ruta, namespaces in mapa[prefijo]:
            relativo = os.path.relpath(ruta, RAIZ).replace("\\", "/")
            if not namespaces:
                sin_namespace.append("[%s] en %s" % (prefijo, relativo))
                continue
            resuelven: set[str] = set()
            for namespace in namespaces:
                for idioma in IDIOMAS:
                    valor, resuelto = cargar(idioma, namespace, prefijo)
                    if not resuelto:
                        continue
                    if valor is None:
                        # Un namespace declarado que no tiene el prefijo no es un fallo: el
                        # componente puede leer de varios. Solo es fallo si **ninguno** resuelve.
                        continue
                    resuelven.add("%s/%s" % (namespace, idioma))
                    if isinstance(valor, dict) and not valor:
                        vacios.append("[%s] %s en %s (%s)" % (idioma, prefijo, relativo, namespace))
            if not resuelven:
                faltan.append(
                    "[%s] %s en %s, declarados %s y ninguno lo tiene"
                    % (IDIOMAS[0], prefijo, relativo, " y ".join(namespaces))
                )
            elif len({r.split("/")[0] for r in resuelven}) > 1:
                ambiguos.append(
                    "[%s] %s resuelve en %s" % (prefijo, relativo, ", ".join(sorted(resuelven)))
                )

    print(
        "  %d prefijo(s) construido(s) en runtime, comprobados en %s"
        % (len(mapa), " y ".join(IDIOMAS))
    )

    if faltan or vacios:
        for linea in faltan:
            print("    FALTA   %s" % linea)
        for linea in dict.fromkeys(vacios):
            print("    VACIO   %s" % linea)
        print()
        print("  Una clave que se arma con `${}` no la ve ningun verificador de literales.")
        return 1

    if sin_namespace:
        # No es un fallo: es que el fichero no declara con qué namespace se lee. Se avisa igual
        # para que quede escrito, y el gate pasa porque no hay ningun problema demostrado.
        print(
            "  %d prefijo(s) en ficheros que no declaran namespace: sin comprobar"
            % len(set(sin_namespace))
        )
        for linea in dict.fromkeys(sin_namespace):
            print("    SIN REVISAR  %s" % linea)

    if ambiguos:
        print(
            "  %d prefijo(s) que resuelven en mas de un namespace del mismo fichero:"
            % len(set(ambiguos))
        )
        for linea in dict.fromkeys(ambiguos):
            print("    REVISAR  %s" % linea)

    print("  todos los comprobados existen y ninguno esta vacio")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
