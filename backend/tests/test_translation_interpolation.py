"""Las tres formas de una traduccion que se han roto, y como se vuelven a romper.

## Por que este fichero existe

Son tres fallos que ya han pasado en este repositorio, y los tres son **la misma clase**: un
contrato de forma que dejo de cumplirse y que nadie comprueba. En los tres casos el sintoma no
era un error de ejecucion sino **texto en pantalla**.

1. **Llaves simples en las traducciones.** I18next interpola con llaves dobles, `{{nombre}}`.
   Estas traducciones estaban escritas con llaves simples, `{nombre}`, que i18next no toca: las
   deja tal cual. En la consola se leia `{ACTIVE} ACTIVOS DE {TOTAL}` y en el selector de
   permisos del modal de token, `0 / 49 (chosen) de (total) seleccionados`. Eran 46 ocurrencias
   en 4 namespaces.

2. **Una base de proveedor con el endpoint dentro.** `LLM_API_BASE` es la **base**, y el cliente
   le anade `/chat/completions`. Con la base mal puesta, la ruta final lleva el endpoint dos
   veces y el proveedor responde `404`. El `404` salia **dentro de una peticion del chat**, no al
   arrancar: el operador veia "el chat no funciona" sin rastro de que la causa era una variable
   de entorno.

3. **Una traduccion documentando un formato literal.** El caso limite del primero, y el que
   hace que la prueba anterior no pueda ser un `sed`. En `webhooks.secret.verifyHint` las llaves
   describen el formato real de una cabecera de firma, `t={unix},v1={hmac}`. Convertirlas a
   dobles haria que i18next buscara `unix` y `hmac`, no los encontrara, y los **borrara**: la
   documentacion de como verificar una firma se quedaria diciendo `t=,v1=`.

## Que comprueba

- Que ninguna traduccion tenga llaves simples **salvo** una lista explicita de documentacion
  literal, y que esa lista siga siendo documentacion literal.
- Que todo `t('x', { valor })` pase en efecto algo que se pueda interpolar: no basta con que
  `{{x}}` este en la cadena, tiene que haber un `{{y}}` en la llamada.
- Que `LLM_API_BASE` no traiga el endpoint de chat, y que el validador lo diga con un mensaje
  que nombre la variable.

## Por que la lista de intocables es explicita y no se deduce

Porque no se puede deducir. Una llave simple es un placeholder mal escrito **o** es parte de un
protocolo documentado, y la diferencia no esta en la cadena: esta en si el punto de llamada pasa
esa variable. Por eso la lista esta escrita a mano y la prueba comprueba que sigue siendo corta
y esta justificada, que es lo que evita que crezca sin que nadie mire.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.core.config import Settings

LOCALES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "locales"
CONFIG = Path(__file__).resolve().parents[1] / "core" / "config.py"

IDIOMAS = ("es", "en")

# Una llave simple: la que i18next NO interpola.
LLAVE_SIMPLE = re.compile(r"(?<!\{)\{(\w+)\}(?!\})")
# La que si: la forma de i18next.
LLAVE_I18NEXT = re.compile(r"\{\{(\w+)\}\}")

# Las unicas traducciones donde una llave simple es intencionada, con el motivo.
#
# Se recorren **todas** las de los dos idiomas, asi que anadir una sin motivesarla aqui hace que
# la prueba falle. Anadirla aqui sin motivo no lo detecta nadie, que es el riesgo que se acepta:
# una lista de excepciones es un contrato, y un contrato se aplica a mano las primeras veces.
INTOCABLES = {
    ("apiAccess", "webhooks.secret.verifyHint"): (
        "documenta el formato literal de la cabecera de firma X-MGF-Signature, "
        "t={unix},v1={hmac}; el punto de llamada no pasa ninguna variable y sus llaves "
        "son el nombre de los campos del protocolo, no variables de la traduccion"
    ),
}


def cargar(idioma: str, namespace: str):
    with (LOCALES / idioma / f"{namespace}.json").open(encoding="utf-8") as f:
        return json.load(f)


def hojas(nodo, prefijo: str = ""):
    if not isinstance(nodo, dict):
        yield prefijo.rstrip("."), nodo
        return
    for clave, valor in nodo.items():
        yield from hojas(valor, f"{prefijo}{clave}.")


def valor_en(arbol, clave: str):
    nodo = arbol
    for parte in clave.split("."):
        if not isinstance(nodo, dict) or parte not in nodo:
            return None
        nodo = nodo[parte]
    return nodo


@pytest.mark.parametrize("idioma", IDIOMAS)
def test_ninguna_traduccion_usa_llaves_simples(idioma: str) -> None:
    """Las llaves simples no se interpolan; salen en pantalla tal cual."""

    offending: list[str] = []
    for fichero in sorted((LOCALES / idioma).glob("*.json")):
        namespace = fichero.stem
        with fichero.open(encoding="utf-8") as f:
            arbol = json.load(f)
        for ruta, valor in hojas(arbol):
            if not isinstance(valor, str) or not LLAVE_SIMPLE.search(valor):
                continue
            if (namespace, ruta) in INTOCABLES:
                continue
            offending.append(f"{namespace}:{ruta}")

    assert not offending, (
        "estas traducciones usan llaves simples, que i18next no interpola: en pantalla sale el "
        f"placeholder tal cual. El arreglo es pasarlas a dobles, {{nombre}}. offending: {offending}"
    )


@pytest.mark.parametrize("idioma", IDIOMAS)
def test_las_llaves_simples_de_documentacion_siguen_siendolo(idioma: str) -> None:
    """La excepcion de `verifyHint` no se rompe por accidente.

    Si alguien le pone llaves dobles, i18next pasa a buscar `unix` y `hmac`, no los encuentra y
    los borra: la documentacion de como verificar una firma se queda en `t=,v1=`. Este fallo es
    silencioso —la cadena sigue siendo valida y se sigue viendo— y por eso se comprueba.
    """

    for (namespace, clave), motivo in INTOCABLES.items():
        valor = valor_en(cargar(idioma, namespace), clave)
        assert isinstance(valor, str), f"{namespace}:{clave} no existe en {idioma}"
        assert LLAVE_SIMPLE.search(valor), (
            f"{namespace}:{clave} ha perdido las llaves simples en {idioma}. {motivo}. Si ya no "
            "hace falta ser excepcion, borrala de INTOCABLES: dejarla es permitir que alguien la "
            "arregle sin querer."
        )
        assert not LLAVE_I18NEXT.search(valor), (
            f"{namespace}:{clave} tiene llaves dobles en {idioma}: i18next las buscara, no "
            f"encontrara nada y las borrara. {motivo}."
        )


def test_las_excepciones_son_las_justificadas_y_no_mas() -> None:
    """La lista de intocables no crece sin que nadie mire el motivo."""

    for (namespace, clave), motivo in INTOCABLES.items():
        assert motivo.strip(), f"la excepcion {namespace}:{clave} no explica por que"
        assert len(motivo) > 40, (
            f"la excepcion {namespace}:{clave} explica el motivo en cinco palabras: "
            f"'{motivo}'. Ese es el margen con el que se olvida revisar una excepcion."
        )
        for idioma in IDIOMAS:
            assert (LOCALES / idioma / f"{namespace}.json").is_file(), (
                f"la excepcion {namespace}:{clave} apunta a un namespace que no existe en {idioma}"
            )


def test_el_validador_normaliza_el_endpoint_de_chat_pegado_dentro() -> None:
    """Un endpoint de chat pegado en `LLM_API_BASE` se recorta, no se rechaza.

    ## Por qué normalizar y no rechazar, que era lo que hacía antes

    Porque el síntoma de este error es un `404` **dentro de una petición del chat**, no un
    error de arranque, y la variable que hay que corregir no viene mencionada en ninguna parte.
    Negarse a arrancar por un `/chat/completions` de más convierte un error de tecleo en una
    caída del servicio entero, y el operador que pegó la URL de la documentación del proveedor
    —que es exactamente lo que devuelve el buscador— recibe un portazo en vez de un arreglo.

    Normalizar devuelve la base correcta y sigue funcionando, y el valorNormalized es además
    lo que se ve en `/admin`, así que una configuración silenciosamente reparada se puede
    comprobar en la pantalla en vez de tener que confiar en que pasó.

    La comparación es en minúsculas a propósito: `.../CHAT/COMPLETIONS` es el mismo error
    copiado de una página en mayúsculas, y un recorte sensible a mayúsculas dejaría pasar
    justo el caso que se quiere tapar.
    """

    recortados = {
        "https://openrouter.ai/api/v1/chat/completions": "https://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1/chat/completions/": "https://openrouter.ai/api/v1",
        "https://api.openai.com/v1/CHAT/COMPLETIONS": "https://api.openai.com/v1",
    }
    for valor, esperado in recortados.items():
        assert _validar_base(valor) == esperado, valor


def test_el_validador_rechaza_la_base_con_otra_ruta_de_completions() -> None:
    """`/v1/completions` no se puede recortar y sí se rechaza, con mensaje utilizable.

    Es el caso que la normalización no cubre, y la razón de que siga existiendo: el sufijo que
    se recorta es exactamente `/chat/completions`, no "lo que acaben en completions". Cualquier
    otra ruta de `completions` significa que la variable apunta a un sitio que el cliente no va
    a usar, y adivinar cuál quería convertiría un error en un escaneo que sale a otro sitio.

    Un `ValueError` y no un `ValidationError` porque la prueba llama al validador **por su
    función**: es pydantic quien lo convierte en `ValidationError` al construir el modelo, y al
    saltar ese paso sale el `ValueError` original. Lo que importa es que rechace y que el
    mensaje sea utilizable.
    """

    for valor in ("https://api.openai.com/v1/completions", "https://api.openai.com/v1/COMPLETIONS"):
        with pytest.raises(ValueError) as error:
            _validar_base(valor)
        # El mensaje tiene que **nombrar la variable** y decir cuál es la forma buena. Un
        # "valor invalido" sin más obliga a ir a buscar la variable a mano.
        texto = str(error.value)
        assert "LLM_API_BASE" in texto, f"el mensaje no nombra la variable: {texto[:200]}"
        assert "completions" in texto.lower(), (
            f"el mensaje no explica la forma buena: {texto[:200]}"
        )


@pytest.mark.parametrize(
    "valor",
    [
        "https://openrouter.ai/api/v1",
        "https://api.openai.com/v1",
        "https://mi-proxy.interno/openrouter/v1",
        "",
    ],
)
def test_el_validador_acepta_una_base_de_verdad(valor: str) -> None:
    """Las bases correctas pasan, incluidas las que tienen ruta.

    Este test va con el anterior porque el peligro del validador no es que rechace poco, es que
    **rechace de mas**: si un "no parece una base" demasiado aparecen, la gente acaba poniendo
    cualquier cosa para que la aplicacion arranque, y el fallo vuelve pero mas tarde.
    """

    assert _validar_base(valor) == valor.strip().rstrip("/")


def _validar_base(valor: str) -> str:
    """Ejecuta **solo** el validador de `llm_api_base`, sin construir el resto de `Settings`.

    Construir `Settings` entero exigiria cumplir docenas de variables obligatorias, con lo que la
    prueba dejaria de hablar de la base y passaria a hablar de cuan molesto es el resto de la
    configuracion.

    Se llega al validador por `__pydantic_decorators__.field_validators[...].func`, que es un
    metodo **ya ligado** a la clase: pydantic envuelve el decorador en un `Decorator` que no
    expone ni `.cls` ni `.func` como un metodo normal, y se llega al comportamiento por ahi. Y
    se comprueba con `getattr(..., None)` en vez de subscripting directo para que, si pydantic
    cambia la estructura interna, el fallo sea "ya no hay este validador" y no un
    `KeyError` en un sitio que no explica nada.
    """

    validador = Settings.__pydantic_decorators__.field_validators.get("validate_llm_api_base")
    if validador is None:
        pytest.fail(
            "Settings ya no declara el validador 'validate_llm_api_base'. Si el fallo de la base "
            "con el endpoint dentro ha vuelto a ser posible, esta prueba esta escribiendo sobre "
            "nada."
        )
    return str(validador.func(valor))  # type: ignore[no-any-return]
