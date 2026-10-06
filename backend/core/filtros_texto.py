r"""Ayudas de filtro por texto y por rango de fechas, compartidas por los listados paginados.

## Qué hay aquí y por qué

Porque cuatro listados del proyecto necesitan exactamente las mismas dos cosas —escapar los
comodines de `LIKE` y cortar un rango de fechas naturales en UTC— y cada copia tiene un detalle
que se puede equivocar de forma distinta. Un filtro de búsqueda es de las pocas cosas del
proyecto donde un error **no se ve**: la consulta sigue funcionando, y lo que falla es que
devuelva filas que el usuario no pidió.

## Por qué escapar `LIKE` no es opcional

Porque `%` y `_` son comodines, y sin escapar `?search=%` devuelve **la tabla entera**: el
patrón pasa a ser `%\%%`, que casa con cualquier valor. Es un fallo silencioso y además
engañosamente útil —la pantalla "funciona"—, así que nadie lo reporta. Con el escape,
`?search=%` busca literalmente un símbolo de porcentaje, que es lo que el usuario escribió.

El `_` aparece en el mundo real: `api_admin`, `app_web`, `acme_web`. Sin escapar, buscar
`web_app` también devuelve `webXapp` y `web1app`.

## Por qué el rango se corta en UTC

Porque las columnas de fecha son `timestamptz` y PostgreSQL compara instantes. Si el corte se
calculara en hora local, el mismo rango daría resultados distintos según desde qué zona se
consulte, y «del lunes al martes» dejaría de ser una frase que significa lo mismo para todo el
mundo. El backend no tiene forma de saber la zona del navegador que pregunta, así que la única
decisión que no depende de quién pregunta es UTC.

## Por qué el límite superior es **inclusivo**

Porque «del 1 al 5» son cinco días, no cinco días menos el último. El corte superior es la
medianoche **del día siguiente**, de modo que el último día entra entero. Con un corte en la
medianoche del propio día final, ese día solo aportaría las filas de exactamente las 00:00: un
resultado que nadie quiere y que además depende de la zona horaria de quien pregunta.

Y un rango invertido —`desde` posterior a `hasta`— **no** es un error de validación: las dos
condiciones son incompatibles por construcción, así que la respuesta ya es la que corresponde.
Devolver `422` obligaría a cada pantalla a manejar un estado que nunca se da.

## Por qué estas ayudas no construyen la consulta

Porque el filtro por `organization_id` —R3— va siempre y lo compone quien llama. Un ayudante
que construyera la consulta entera escondería justo el filtro del que no hay que prescindir
nunca, y además no serviría para un listado donde el aislamiento lo aporta un `JOIN` en vez de
una columna propia.

## Por qué aquí ya no hay copias del escape

Porque hubo **cuatro**, una por módulo, cada una con su docstring defendiendo por qué se quedaba:
`assets/service.py`, `support/service.py`, `repositories/router.py` y `cve_database/service.py`. Las
cuatro eran correctas —se comprobó que hacen lo mismo en los mismos términos, 5.104 por copia— y
eso es lo que lo hacía peor: un filtro de búsqueda es de las pocas cosas donde el fallo no se ve,
así que cuatro copias correctas son cuatro sitios donde el próximo que las toque se equivoca en
uno solo y no se entera.

Los cuatro módulos usan ya `escape_like` de aquí. Y `test_escape_like_consolidado.py` es lo que lo
mantiene así: recorre el árbol y falla si reaparece un `def _escape_like` fuera de este fichero, y
compara esta función contra el cuerpo exacto de las copias antigas sobre los términos que
separan un orden de escape correcto de uno equivocado —la barra invertida primero, que es el
detalle que no se ve en el resultado de la pantalla.

El comentario de aquí y el de los cuatro módulos están escritos con el motivo, no con la excusa:
la consolidación se hizo **después**, con sus pruebas, y no colgada de un cambio de filtros.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import cast

from sqlalchemy import ColumnElement, func, or_
from sqlalchemy.sql.operators import ColumnOperators


def escape_like(termino: str) -> str:
    """Escapa los comodines de `LIKE` para que el término se busque literal.

    ## Por qué la barra invertida se escapa primero

    Porque el orden de los tres `replace` no es arbitrario. Si la barra se escapara al final,
    la propia barra que ponen los otros dos reemplazos se convertiría a su vez en `\\\\` y el
    resto del escape quedaría mal. De aquí el `replace` de la barra en la **primera** posición.
    """

    return termino.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def patron_contains(termino: str) -> str:
    """El patrón `%término%`, en minúsculas y ya escapado, para `like(..., escape="\\\\")`."""

    return f"%{escape_like(termino.lower())}%"


def coincide(columnas: Sequence[ColumnOperators], termino: str) -> ColumnElement[bool]:
    """Condición «alguna de estas columnas contiene el término», sin distinguir mayúsculas.

    ## Por qué varias columnas y no una

    Porque «¿qué estoy buscando?» no tiene un único sitio donde mirar. En el listado de
    usuarios son el correo y el nombre; en el de ventas, el evento y la organización. Con una
    sola columna, buscar `acme` devolvería página vacía la mitad de las veces y el buscador
    parecería roto.

    ## Por qué `lower()` y no `ilike`

    Por simetría con `repositories/router.py`, que es donde se escribió primero. Las dos
    formas dan el mismo resultado en PostgreSQL; lo que importa es que todas las pantallas
    comparen **igual**, porque un buscador que distingue mayúsculas en una columna y no en otra
    es el peor de los dos mundos.

    Con una lista vacía, `or_()` devuelve una condición que nunca es cierta. Es el resultado
    correcto para «no hay ninguna columna donde buscar»: el listado sale vacío en vez de
    devolverlo entero, que es lo que daría un `or_()` sobre una condición vacía.
    """

    patron = patron_contains(termino)
    alternativas: list[ColumnElement[bool]] = [
        func.lower(columna).like(patron, escape="\\") for columna in columnas
    ]
    return or_(*alternativas)


def medianoche_utc(dia: date) -> datetime:
    """Medianoche UTC del día pedido.

    ## Por qué no `datetime.combine(dia, time.min)`

    Porque `time.min` sin `tzinfo` es naive, y una comparación entre un `timestamptz` y un
    naive en la zona del servidor deja el corte en hora local sin que se note. La zona se
    escribe aquí, a la vista.
    """

    return datetime(dia.year, dia.month, dia.day, tzinfo=UTC)


def rango_creado(
    columna: ColumnOperators,
    desde: date | None,
    hasta: date | None,
) -> list[ColumnElement[bool]]:
    """Las condiciones de un rango sobre una columna de **creación**.

    ## Por qué el rango va sobre la creación y no sobre una columna que pueda ser `NULL`

    Porque un filtro sobre una columna anulable no recorta filas: las **borra**. Si el rango
    fuera por una fecha de finalización, `NULL` —que es lo que hay mientras el trabajo no ha
    terminado— no cumple ni `>=` ni `<`, así que todos los trabajos en curso desaparecerían de
    la tabla en cuanto se tocara cualquiera de las dos fechas. El usuario vería desaparecer
    justo lo que está mirando, y lo leería como que el sistema lo ha perdido.

    Y porque la creación es la columna por la que se ordena el listado: el rango recorta filas y
    la paginación las cuenta, así que ambos van sobre la misma fecha y los bordes de página caen
    donde el usuario espera.

    Se devuelve una **lista** y no una condición única porque los dos extremos son
    independientes: solo se ha dado «desde», solo «hasta», o los dos. Quien llama la extiende
    con lo suyo —el filtro de organización, el de tipo— y así la lista entera de condiciones se
    lee en un solo sitio.

    ## Por qué hay dos `cast` y no una firma más directa

    Porque SQLAlchemy tipa una comparación como `ColumnOperators[bool]`, y `select().where()`
    no acepta eso: pide un `ColumnElement`. El `cast` está aquí y no en los cuatro listados
    porque el motivo es de las anotaciones de la librería, no de cada llamada, y porque en
    tiempo de ejecución la comparación es un `BinaryExpression`, que **sí** es un
    `ColumnElement`. No hay ninguna puerta trasera: lo que se entrega al `where` es la
    expresión que devuelve la comparación, sin reescribirla.
    """

    condiciones: list[ColumnElement[bool]] = []
    if desde is not None:
        condiciones.append(cast(ColumnElement[bool], columna >= medianoche_utc(desde)))
    if hasta is not None:
        # Inclusivo: la medianoche del día **siguiente**, no la del propio `hasta`.
        condiciones.append(
            cast(ColumnElement[bool], columna < medianoche_utc(hasta) + timedelta(days=1))
        )
    return condiciones
