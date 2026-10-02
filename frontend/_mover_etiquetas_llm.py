"""Mueve a `llm.json` las dos etiquetas `aria` de coste, que solo existían en `admin.json`.

## Qué estaba roto

`LlmModelsPage` declara `useTranslation('llm')` y llama a `t('actions.costInputAria')` y
`t('actions.costOutputAria')`. Las dos claves existen —en español y en inglés— pero en
`admin.json`, cuyo `actions` tiene solo cuatro entradas y no las incluye. En `llm.json` el bloque
`actions` tiene tres: `activate`, `deactivate`, `save`.

Resultado: los dos campos de coste del modelo tenían un `aria-label` con el texto literal
`actions.costInputAria` dentro, en los dos idiomas. No es visible en la captura, pero un lector de
pantalla lee esa cadena en crudo, y ese es justo el público al que un atributo `aria` le sirve de
algo.

## Por qué no se quitan de `admin.json`

Porque no se borra nada sin motivo. Puede que otro componente las use desde el namespace `admin`, y
comprobar eso es una búsqueda. Este script **copia** y luego avisa de cuántas quedan en `admin`, para
que la limpieza —si algún día se hace— sea una decisión documentada y no un olvido.

## Por qué copiar y no volver a escribir

Porque el texto ya estaba redactado y traducidos los dos idiomas. Reescribirlo produciría dos
versiones del mismo `aria-label` que divergen en la primera corrección de estilo, y el síntoma —una
pantalla de lectura que anuncia algo distinto en español y en inglés— no lo caza ningún gate.
"""

from __future__ import annotations

import io
import json
import os
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
CLAVES = ("costInputAria", "costOutputAria")


def principal() -> int:
    fallos: list[str] = []

    for lang in ("es", "en"):
        admin_ruta = os.path.join(RAIZ, "src", "locales", lang, "admin.json")
        llm_ruta = os.path.join(RAIZ, "src", "locales", lang, "llm.json")

        admin = json.load(io.open(admin_ruta, encoding="utf-8"))
        llm = json.load(io.open(llm_ruta, encoding="utf-8"))

        origen = admin.get("actions", {})
        destino = llm.setdefault("actions", {})

        copiadas: list[str] = []
        for clave in CLAVES:
            if clave in destino:
                # Ya está: no se pisa. Un texto existente manda sobre una copia, porque lo que hay
                # es lo que alguien ha estado viendo.
                continue
            if clave not in origen:
                fallos.append("[%s] %s no está ni en llm ni en admin" % (lang, clave))
                continue
            destino[clave] = origen[clave]
            copiadas.append(clave)

        with io.open(llm_ruta, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(llm, ensure_ascii=False, indent=2) + "\n")

        # Se relee del disco: si el volcaje falló a medias, aquí se ve.
        recarga = json.load(io.open(llm_ruta, encoding="utf-8"))
        ausentes = [c for c in CLAVES if c not in recarga.get("actions", {})]
        if ausentes:
            fallos.append("[%s] no llegaron a llm.json: %s" % (lang, ", ".join(ausentes)))

        print(
            "  %s: copiadas %d a llm.json; quedan %d en admin.json"
            % (lang, len(copiadas), sum(1 for c in CLAVES if c in origen))
        )

    if fallos:
        print()
        for linea in fallos:
            print("  FALLO %s" % linea)
        return 1

    print()
    print("  Las dos siguen en admin.json a proposito: no se borra nada sin comprobar quien las usa.")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
