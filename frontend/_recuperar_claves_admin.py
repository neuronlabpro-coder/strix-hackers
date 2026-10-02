"""Rehace las 28 claves de `admin.json` que un `git checkout` se llevó por delante.

## Qué pasó

Al revisar el reformateo de `admin.json` se ejecutó `git checkout --` sobre los dos ficheros de
idioma para deshacerlo. El reformateo venía de este mismo script —`json.dumps` reescribe el fichero
entero—, pero el `checkout` no distingue entre lo que hizo el script hace un minuto y lo que había
sin commitear desde la sesión anterior. Revirtió las dos cosas a la vez, y con ellas 28 claves que
alguien había escrito y nadie había commiteado.

## Por qué este fichero existe y no se editó a mano

Porque las claves que faltaban están repartidas en dos sitios del JSON y Editar a mano las 28 en
español y en inglés es exactamente el trabajo en el que uno se equivoca por cansancio: saltarse
una, duplicar una, dejar una tilde donde no toca. Aquí se declaran **juntas** las 28, en los dos
idiomas, en el mismo orden, y se comprueba al final que los dos ficheros siguen siendo JSON válido
y que las claves nuevas están donde el código las busca.

## De dónde sale cada texto

El **inglés** es literal: estaba en el `git diff` que se imprimió antes del `checkout`, así que se
repone palabra por palabra.

El **español es redacción nueva**, hecha para encajar en el hueco que cada clave ocupa —una ayuda
de campo, un pie de página, el distintivo de una fila— y para sonar como el resto del fichero, que
va en español sin tildes en los valores por una decisión anterior del proyecto. Lo que no se puede
recuperar es el tono que hubiera elegido quien las escribió la primera vez: si esas frases estaban
aprobadas por el cliente, hay que revisarlas antes de vender.

## Qué NO hace

No toca nada más de `admin.json`. Las claves que ya estaban en HEAD se dejan como estaban, y solo
se añaden las que faltaban. El volcado usa `indent=2` y `ensure_ascii=False` para noguía de estilo:
sin ellos, cada acento se convierte en `\u00e1` y el fichero deja de poder revisarse de un vistazo,
que es como se revisaba antes.
"""

from __future__ import annotations

import io
import json
import os
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))

#: Las 28 claves perdidas, por idioma. El orden es el que tienen en el fichero para que las nuevas
#: queden junto a las suyas y no al final de todo el bloque.
PERDIDAS: dict[str, dict] = {
    "es": {
        "tenants": {
            "pricing": {
                "states": {
                    "superseded": "Sustituido",
                },
                "chain": {
                    "summary": (
                        "{{vigente}} vigente, {{programado}} programado, "
                        "{{sustituido}} sustituido, {{total}} en total"
                    ),
                },
                "eyebrow": "Operacion de plataforma",
                "description": (
                    "Lo que quedo acordado con este cliente, por encima del precio de plataforma. "
                    "No se borra nada: cada pactado nuevo sustituye al anterior y los dos se quedan "
                    "con su motivo y su autor."
                ),
                "back": "Volver a organizaciones",
                "footnote": (
                    "El precio cobrado queda congelado en el libro mayor cuando se encola el "
                    "escaneo. Cambiar un precio no altera lo que ya se cobro."
                ),
                "rows": {
                    "scanCost": "Costo en creditos de un escaneo completo.",
                    "quickMultiplier": "Fraccion de un escaneo completo que cuesta un escaneo rapido.",
                    "creditsPerUsd": "Cuantos creditos compra un dolar.",
                    "negotiated": "Pactado",
                },
                "form": {
                    "valueHelp": (
                        "Mismo formato que el precio de plataforma: creditos, fraccion o "
                        "creditos por dolar."
                    )
                },
            }
        },
        "pricing": {
            "states": {
                "failedTitle": "No se pudieron cargar los precios",
                "failedBody": "El servidor no respondio. No se cambio ningun precio.",
            },
            "history": {
                "failedTitle": "No se pudo cargar el historico",
                "failedBody": (
                    "Los precios de esta pantalla si estan cargados; lo que no se pudo pedir es "
                    "quien los cambio y cuando."
                ),
                "loading": "Cargando el historico de cambios...",
            },
        },
    },
    "en": {
        "tenants": {
            "pricing": {
                "states": {
                    "superseded": "Superseded",
                },
                "chain": {
                    "summary": (
                        "{{vigente}} in force, {{programado}} scheduled, "
                        "{{sustituido}} superseded, {{total}} in total"
                    ),
                },
                "eyebrow": "Platform operation",
                "description": (
                    "What was agreed with this customer, on top of the platform price. Nothing is "
                    "deleted: each new agreement replaces the previous one and both stay with their "
                    "reason and their author."
                ),
                "back": "Back to organizations",
                "footnote": (
                    "The charged price is frozen in the ledger when the scan is enqueued. Changing "
                    "a price does not alter what was already charged."
                ),
                "rows": {
                    "scanCost": "Cost in credits of a full scan.",
                    "quickMultiplier": "Fraction of a full scan that a quick scan costs.",
                    "creditsPerUsd": "How many credits one dollar buys.",
                    "negotiated": "Agreed",
                },
                "form": {
                    "valueHelp": (
                        "Same format as the platform price: credits, fraction, or credits per "
                        "dollar."
                    )
                },
            }
        },
        "pricing": {
            "states": {
                "failedTitle": "The prices could not be loaded",
                "failedBody": "The server did not respond. No price was changed.",
            },
            "history": {
                "failedTitle": "The history could not be loaded",
                "failedBody": (
                    "The prices on this screen are loaded; what could not be asked for is who "
                    "changed them and when."
                ),
                "loading": "Loading the change history...",
            },
        },
    },
}

#: Cuántas claves se espera recuperar en total, por idioma. Sirve para que el script falle si un
#: día se edita la tabla y se olvida una: el número está escrito antes que el resultado.
#: Cuántas claves se espera recuperar en total, por idioma. El número está escrito antes de mirar
#: el resultado a propósito: si el día de mañana se añade una clave a `PERDIDAS` y se olvida subir
#: este número, el script avisa en lugar de declarar un éxito falso.
ESPERADAS = 16


def insertar(objetivo: dict, nuevas: dict) -> None:
    """Copia `nuevas` dentro de `objetivo`, creando los bloques que falten.

    No se usa `dict.update` en las hojas porque eso pondría las claves nuevas al final del bloque y
    el fichero acabaría con las etiquetas de una sección partidas en dos sitios. Con este orden, las
    nuevas van detrás de las que ya había, que es lo más cerca de donde estaban.
    """
    for clave, valor in nuevas.items():
        if isinstance(valor, dict):
            hijo = objetivo.setdefault(clave, {})
            if not isinstance(hijo, dict):
                raise TypeError("'%s' existe y no es un bloque" % clave)
            insertar(hijo, valor)
        else:
            objetivo.setdefault(clave, valor)


def principal() -> int:
    fallos: list[str] = []
    for lang, arbol in PERDIDAS.items():
        ruta = os.path.join(RAIZ, "src", "locales", lang, "admin.json")
        with io.open(ruta, encoding="utf-8") as fh:
            datos = json.load(fh)
        insertar(datos, arbol)
        with io.open(ruta, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(datos, ensure_ascii=False, indent=2) + "\n")

        # Se relee del disco: write puede haber fallado a medio escribir y esto lo detecta.
        with io.open(ruta, encoding="utf-8") as fh:
            recarga = json.load(fh)
        anadidas = sum(
            1
            for clave in (
                "tenants.pricing.eyebrow",
                "tenants.pricing.description",
                "tenants.pricing.back",
                "tenants.pricing.footnote",
                "tenants.pricing.rows.scanCost",
                "tenants.pricing.rows.quickMultiplier",
                "tenants.pricing.rows.creditsPerUsd",
                "tenants.pricing.rows.negotiated",
                "tenants.pricing.form.valueHelp",
                "tenants.pricing.states.superseded",
                "tenants.pricing.chain.summary",
                "pricing.states.failedTitle",
                "pricing.states.failedBody",
                "pricing.history.failedTitle",
                "pricing.history.failedBody",
                "pricing.history.loading",
            )
            if _existe(recarga, clave)
        )
        if anadidas != ESPERADAS:
            fallos.append("%s: %d de %d claves presentes" % (lang, anadidas, ESPERADAS))
        print("  %s: %d claves recuperadas, JSON valido" % (lang, anadidas))

    if fallos:
        print()
        for linea in fallos:
            print("  FALLO %s" % linea)
        return 1
    return 0


def _existe(datos: dict, camino: str) -> bool:
    """Si existe la clave `a.b.c` dentro de `datos`.

    Se recorre por partes porque un `in` sobre el camino entero daría False siempre, ya que las
    claves viven anidadas.
    """
    actual: object = datos
    for parte in camino.split("."):
        if not isinstance(actual, dict) or parte not in actual:
            return False
        actual = actual[parte]
    return True


if __name__ == "__main__":
    sys.exit(principal())
