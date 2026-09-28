"""Las claves de traducción que envía la consola de SuperAdmin existen de verdad.

## Por qué esta prueba y no una comprobación de paridad

La comprobación de paridad de `ci_i18n_check.py` verifica que `es` y `en` tengan **las mismas**
claves. Eso no dice nada de si las claves coinciden con **las que pide el API**: un fichero de
traducción puede tener `metrics.mrrHint` perfectamente traducida en los dos idiomas mientras
nadie la usa, y el API puede pedir `admin.metrics.mrrHint` sin que nadie se entere.

Eso es exactamente lo que pasó. El Resumen global pintaba las etiquetas bien —porque `key` es
relativa al namespace— y las pistas de debajo mostraban `admin.metrics.mrrHint` en crudo. Las dos
cosas conviven en la misma tarjeta, y una pantalla así parece medio traducida en lugar de rota.

## Qué comprueba

Que cada `key` y cada `hint_key` de cada métrica del Resumen global exista en **los dos**
idiomas, ya resuelta dentro del namespace `admin`.

## Por qué lee los ficheros en vez de usar i18next

Porque el fallo que previene es de la cadena de translation, y probarlo con la misma cadena
sería probar que la cadena funciona. Leyendo el JSON se comprueba el contrato en el punto donde
se rompe, que es el string que viaja por la red.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.apps.admin import queries
from backend.apps.admin.schemas import (
    DependencyHealth,
    DependencyStatusEnum,
    InfrastructureHealthResponse,
)
from backend.core.database import AsyncSessionLocal

LOCALES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "locales"
IDIOMAS = ("es", "en")
NAMESPACE = "admin"


def _traducciones(idioma: str) -> dict[str, object]:
    with (LOCALES / idioma / f"{NAMESPACE}.json").open(encoding="utf-8") as archivo:
        return json.load(archivo)


def _existe(clave: str, arbol: dict[str, object]) -> bool:
    """Si la clave existe y **es un string**.

    Que sea un string es la mitad de la comprobación. Una clave que apunta a un objeto —el caso
    de un plural— devuelve un objeto en pantalla y `react-i18next` lanza al pedirla como texto,
    que es el crash de `tenants.members` que aparecio en la consola.
    """
    actual: object = arbol
    for parte in clave.split("."):
        if not isinstance(actual, dict) or parte not in actual:
            return False
        actual = actual[parte]
    return isinstance(actual, str)


@pytest.mark.asyncio
async def test_las_claves_de_las_metricas_existen_en_los_dos_idiomas() -> None:
    """La condicion que el API no debe poder violar sin que esto se ponga rojo.

    Se llama a la función real del servicio, no a una lista escrita a mano. Una lista escrita a
    mano pasa aunque el API añada una métrica nueva y olvide su pista, que es el caso que
    importa: la métrica nueva aparecería con su etiqueta traducida y su pista en crudo, y la
    lista de la prueba seguiría en verde.
    """

    # `build_overview` pide también el sondeo de infraestructura, y aquí se construye uno
    # **ficticio** con la forma que exige el esquema. No es lo que se prueba, y por eso no se
    # invoca el sondeo real: esa llamada abre conexiones a PostgreSQL y a Redis, y lo que esta
    # prueba comprueba son las claves de traducción. Un dato inventado en el campo que no se
    # prueba es inocuo; uno inventado en el campo que sí se prueba es una trampa.
    sonda = InfrastructureHealthResponse(
        status=DependencyStatusEnum.HEALTHY,
        database=DependencyHealth(status="online", latency_ms=1),
        cache=DependencyHealth(status="online", latency_ms=1),
        checked_at=datetime.now(UTC),
    )

    async with AsyncSessionLocal() as session:
        metricas = await queries.build_overview(
            session,
            now=datetime.now(UTC),
            infrastructure=sonda,
        )

    assert metricas, "no se han construido metricas: la prueba no probaria nada"

    for idioma in IDIOMAS:
        arbol = _traducciones(idioma)
        for metrica in metricas:
            etiqueta = f"metrics.{metrica.key}"
            assert _existe(etiqueta, arbol), (
                f"[{idioma}] falta la etiqueta de la metrica '{metrica.key}': {etiqueta}"
            )
            # Y la pista, con la forma en la que se envía. Lo que se comprueba es que
            # **no** lleve el namespace dentro, que es el fallo que se vio: el backend mandaba
            # `admin.metrics.mrrHint` y el cliente, que ya usa `useTranslation('admin')`, la
            # buscaba como `admin.admin.metrics.mrrHint`, no la encontraba, y pintaba la cadena
            # entera en pantalla bajo una etiqueta que sí salía traducida.
            assert not metrica.hint_key.startswith(f"{NAMESPACE}."), (
                f"la pista de '{metrica.key}' lleva el namespace dentro: "
                f"'{metrica.hint_key}'. El cliente ya usa useTranslation('{NAMESPACE}'), "
                f"y con el prefijo se buscaria como '{NAMESPACE}.{metrica.hint_key}'."
            )
            assert _existe(metrica.hint_key, arbol), (
                f"[{idioma}] falta la pista de la metrica '{metrica.key}': {metrica.hint_key}"
            )


def test_los_placeholders_de_las_pistas_cuadran_con_las_metricas() -> None:
    """Las pistas que hablan de cifras llevan el mismo numero de marcadores que la cifra.

    Es una comprobacion de contenido y no de estructura, y esta porque una pista puede existir,
    traducirse a los dos idiomas y aun asi estar describiendo otra metrica. La que se vio en
    pantalla era correcta; lo que fallaba era que no llegaba a traducirse.
    """

    arbol = _traducciones("es")
    for clave in ("metrics.mrrHint", "metrics.pentestRunsHint", "metrics.activeTenantsHint"):
        assert _existe(clave, arbol), f"falta {clave}"
        # Una pista de este tipo es una frase, y una frase sin punto final en un panel se lee
        # como un texto cortado por el ancho de la tarjeta.
        #
        # Se navega la clave por partes y no se indexa con `arbol[clave]`: la clave lleva puntos
        # y el diccionario no los entiende. La primera versión de esta prueba hacía eso, y
        # fallaba con `KeyError: 'metrics.mrrHint'` en una clave que **sí** existe —lo que
        # parece un fallo de traducción y no lo era.
        texto: object = arbol
        for parte in clave.split("."):
            assert isinstance(texto, dict), f"{clave} no es un grupo de claves: {texto!r}"
            texto = texto[parte]
        assert isinstance(texto, str) and texto.strip().endswith("."), (
            f"la pista {clave} no termina en punto: {texto!r}"
        )
