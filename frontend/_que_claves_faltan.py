"""Comprueba una pantalla suelta, sin editarla, y dice qué claves de traducción no existen.

## Por qué existe esto, cuando ya hay `_i18n_check.py`

Porque `_i18n_check.py` recorre una lista fija de pantallas —las nueve de la consola— y todo lo
que quede fuera de esa lista no se comprueba. `KnowledgeCatalogView` no está en la lista, y por eso
sus claves `catalog.*` llevan tiempo sin existir sin que nada lo advierta: se comprueba contra
cualquier namespace, y como esas claves no están en ninguno, el resultado es «ninguna falta».

Es un script de **investigación**, no un gate: se le pasa la ruta de un fichero y responde. No se
añade a `ci_check.py` porque un gate que obliga a ampliar una lista a mano se queda corto justo
cuando hace falta, que es lo que pasó aquí.

## Cómo se lee el resultado

Cada clave sale con su namespace real, que es el que declara `useTranslation(...)` en ese
fichero. Sin eso no hay forma de saber *dónde* va la traducción que falta: `catalog.severities` en
un componente con namespace `knowledge` no es un typo de una letra, es un desajuste entero.

Y una clave que empieza por un prefijo con `$` —`catalog.severities.${severity}`— se comprueba
como bloque: lo que se verifica es que exista el diccionario `catalog.severities`, no que tenga un
valor para cada severidad posible, porque los valores posibles vienen del servidor.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
LOCALES = os.path.join(RAIZ, "src", "locales")
IDIOMAS = ("es", "en")

#: `t('clave')` con la clave literal, y `t(`prefijo.${x}`)` con la clave construida.
LITERAL = re.compile(r"""\bt[A-Za-z]*\(\s*['"]([a-zA-Z0-9_.]+)['"]""")
DINAMICA = re.compile(r"""\bt[A-Za-z]*\(\s*`([a-zA-Z0-9_.]+)\.\$\{""")

#: El namespace que declara el propio fichero.
NAMESPACE = re.compile(r"useTranslation\(\s*['\"]([a-zA-Z]+)['\"]")


def cargar(idioma: str, namespace: str | None) -> dict:
    """El JSON del namespace que declara el fichero, o todos juntos si no declara ninguno.

    ## Por qué no basta con juntar todos los ficheros

    Porque las claves de raíz se repiten entre namespaces: `detail` existe en `cve.json`, en
    `issues.json`, en `knowledge.json` y en `support.json`, y cada una con un contenido distinto.
    Si se juntan todos con `setdefault`, gana el primero en orden alfabético —`cve.json`— y una
    clave que sí existe en `knowledge.json` sale como ausente solo porque `cve.json` no la tiene.

    Esa es exactamente la clase de falso positivo que hace que alguien "arregle" una traducción que
    estaba bien. Aquí se lee **el namespace que declara el componente**, que es el único sitio donde
    i18next va a buscarla de verdad.
    """
    carpeta = os.path.join(LOCALES, idioma)
    if namespace is not None:
        ruta = os.path.join(carpeta, "%s.json" % namespace)
        if os.path.exists(ruta):
            return json.load(io.open(ruta, encoding="utf-8"))

    # Sin namespace declarado no hay forma de saber el fichero, así que se informa y se usan todos.
    datos: dict = {}
    for nombre in sorted(os.listdir(carpeta)):
        if nombre.endswith(".json"):
            for k, v in json.load(io.open(os.path.join(carpeta, nombre), encoding="utf-8")).items():
                datos.setdefault(k, v)
    return datos


#: Los sufijos con los que i18next pluraliza. Una clave que se llama con `{ count }` **no** existe
#: jamás en singular: lo que existen son `clave_one` y `clave_other`. Sin esta lista, las claves de
#: plural del panel saldrían como faltando cuando están perfectamente escritas, y "arreglarlas"
#: crearía claves nuevas que no usa nadie.
#:
#: Y el caso inverso también está: desde el 4 de octubre de 2026 las claves con plural de
#: `supplyChain.json` están **en plano** —`metrics.repositories_one` cuelga de la raíz del
#: namespace, no de `metrics`— porque con i18next 26.4.2 la forma anidada devuelve el aviso
#: «returned an object instead of string» en lugar de la frase. `resolver` acepta tanto la forma
#: anidada como la plana por eso: un comprobador que solo entendiera una de las dos daría un
#: falso positivo en el fichero que está bien. Ver `plurales.test.ts`, que es donde vive el
#: porqué.
SUFIJOS_PLURAL = ("_one", "_other", "_zero", "_two", "_few", "_many")


def resolver(datos: dict, clave: str) -> bool:
    nodo: object = datos
    for parte in clave.split("."):
        if not isinstance(nodo, dict) or parte not in nodo:
            return False
        nodo = nodo[parte]
    return True


def existe(datos: dict, clave: str) -> bool:
    """Si la clave existe, o si es un plural bien escrito de i18next.

    ## Por qué dos formas y no una

    Porque i18next acepta las dos y el proyecto usa las dos. La **anidada** —`metrics` con una
    clave `repositories` que es un objeto `_one`/`_other`— es la que documenta i18next; la
    **plana** —`metrics.repositories_one` colgando de la raíz del namespace— es la que hace que
    las frases plurales salgan con i18next 26.4.2, donde la anidada devuelve el aviso «returned
    an object instead of string». Ver la nota de `SUFIJOS_PLURAL`.

    Se prueban las dos porque este script responde «¿existe esta clave en el fichero?», y la
    respuesta honesta es que sí, en las dos formas. Un comprobador que solo entendiera una
    diría que falta una clave que existe, y eso enseña a ignorar su salida.
    """

    if resolver(datos, clave):
        return True
    # La forma plana se busca **antes** de partir por puntos: `metrics.repositories_one`
    # contiene puntos, y partirlo buscaría `repositories_one` dentro de `metrics`, que no
    # existe porque la hoja cuelga de la raíz del namespace.
    return any(clave + sufijo in datos for sufijo in SUFIJOS_PLURAL) or any(
        resolver(datos, clave + sufijo) for sufijo in SUFIJOS_PLURAL
    )


def principal() -> int:
    if len(sys.argv) < 2:
        print("uso: python _que_claves_faltan.py <ruta.tsx> [mas rutas...]")
        return 2

    for ruta_rel in sys.argv[1:]:
        ruta = os.path.join(RAIZ, ruta_rel)
        if not os.path.exists(ruta):
            print("  no existe: %s" % ruta_rel)
            return 2
        texto = io.open(ruta, encoding="utf-8").read()

        espacios = set(NAMESPACE.findall(texto))
        print("=== %s" % ruta_rel)
        print("    namespace(s): %s" % (", ".join(sorted(espacios)) or "ninguno declarado"))

        literales = sorted(set(LITERAL.findall(texto)))
        dinamicas = sorted(set(DINAMICA.findall(texto)))

        # Con varios namespaces declarados en el mismo fichero hay que dar la clave por buena si
        # aparece en **cualquiera** de ellos: i18next busca en el que se le pase a `t` en cada
        # línea, y este script no puede saber cuál es cuál. Marcarlo como faltando cuando está en
        # uno de los dos sería un falso positivo, que es el modo de fallo de este tipo de comprobación.
        for idioma in IDIOMAS:
            if espacios:
                conjuntos = [cargar(idioma, ns) for ns in sorted(espacios)]
                faltan = [
                    c for c in literales if not any(existe(d, c) for d in conjuntos)
                ]
                faltan_bloque = [
                    c for c in dinamicas if not any(resolver(d, c) for d in conjuntos)
                ]
                donde = ", ".join(sorted(espacios))
            else:
                print(
                    "    [%s] sin namespace declarado: se revisan todos los ficheros juntos" % idioma
                )
                datos = cargar(idioma, None)
                faltan = [c for c in literales if not existe(datos, c)]
                faltan_bloque = [c for c in dinamicas if not resolver(datos, c)]
                donde = "todos"

            print(
                "    [%s] namespace: %-12s %d literal(es), %d bloque(s) construido(s)"
                % (idioma, donde, len(literales), len(dinamicas))
            )
            for clave in faltan:
                print("      FALTA      %s" % clave)
            for clave in faltan_bloque:
                print("      FALTA BLOQ %s.${...}" % clave)

    return 0


if __name__ == "__main__":
    sys.exit(principal())
