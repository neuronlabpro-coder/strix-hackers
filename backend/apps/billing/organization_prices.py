"""Resolver qué precio rige a cada organización: plataforma pactada encima de la plataforma.

## Por qué este módulo existe

Porque hay dos precios y no siempre gana el mismo: el de plataforma, que es el mismo para todos, y
el que se ha **negociado con un cliente concreto**. Un plan no describe el consumo —dos clientes
con el mismo plan tienen mil y diez máquinas respectivamente—, así que la unidad que tiene sentido
es la organización, no el plan.

## Por qué el resolution es «override encima de plataforma» y no una tabla de precios por cliente

Porque son la misma regla con dos ceros distintos: uno es el precio published por la
plataforma y el otro el pactado encima. Un precio es el de plataforma **más** lo que se
haya pactado por encima, y si no se ha pactado nada, es el de la plataforma y punto. Una tabla de
precios por cliente tendría que copiar los siete números para cada cliente, y en el primer
cliente al que no se le copiara uno —porque se añadió un campo— ese campo saldría «vacío», que no
es lo mismo
que «vale lo de la plataforma».

Con el override encima no hay copia: si no hay override, el precio es el de plataforma por
definición, y no puede quedarse viejo al añadir un campo nuevo.

## Por qué la carga es por **organizaciones con override**, no por todas

Porque hay casi cuatro mil organizaciones y una cuántas tienen precio pactado. Cargar todas sería
un diccionario con cuatro mil entradas para usar dos; cargar solo las que lo tienen hace que el
tamaño de la caché lo dicta el negocio y no el número de clientes.

## Por qué se cachea en el proceso, y por qué eso no es un problema aquí

Porque el precio se lee en sitios **sin sesión**: el worker de Strix cobra desde una tarea de
Celery y dos servicios cobran desde webhooks. Enhebrar una sesión por esos caminos obligaría a
cambiar la firma de cuatro funciones y a repartir la regla de precio por medio dozen de ficheros.

Y a diferencia del precio de plataforma —que cambia una vez cada meses— un precio pactado cambia
cuando el comercial cierra una renovación, **una vez al año**. La ventana de desfase es el tiempo
que tarda un proceso en reiniciarse, y el trabajo afectado por ella está acotado: el precio de un
escaneo queda congelado en el ledger cuando se encola, así que lo que se liquida al precio viejo
es un trabajo que ya se había aceptado al precio viejo.

## Por qué el fin de vigencia lo resuelve el reloj y no un temporizador

Porque un override caduca **solo**, en su `valido_hasta`. No hay tarea que lo retire ni campo que
limpiar: cuando la fecha pasa, deja de aplicar y el cliente vuelve al precio de plataforma. El
único efecto colateral de la caché es que un proceso muy longevo podría seguir viendo un override
caducado hasta que se refresque, y por eso el filtro compara con la hora actual **al leer**, no
solo al cargar.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import OrganizationPriceOverride, PriceOperationEnum

if TYPE_CHECKING:  # pragma: no cover - solo para el anotado del retorno
    from backend.apps.billing.pricing import PlatformPrices


@dataclass(frozen=True, slots=True)
class _PrecioPactado:
    """Un override vivo, con la fecha hasta la que vive.

    ## Por qué se guarda la fecha y no solo el valor

    Porque el filtro de vigencia se hace **al leer**, y no al cargar. Un override cargado que
   -caduca- tiene que dejar de aplicar sin que nadie lo retire de la base: si el filtro se hiciera
    solo al cargar, un proceso que viviera un año seguiría cobrando a un precio que hace un año no
    valía, y no habría ninguna acción que lo corrigiera.
    """

    valor: Decimal
    valido_desde: datetime
    valido_hasta: datetime | None


#: Los overrides **abiertos** de cada organización, por operación, **todos**, del más
#: reciente al más antiguo.
#:
#: ## Por qué una lista y no un único valor por operación
#:
#: Porque quién está vigente depende del **reloj**, y el reloj no es el mismo al cargar
#: que al cobrar. Guardar solo el más reciente al cargar fue el error que hizo que un
#: pactado con fecha de futuro **borrase de facto** el precio del día: el de futuro ganaba
#: la selección por ser el más nuevo, y al filtrarlo por fecha en la lectura no quedaba
#: ninguno, así que el cliente pasaba a pagar el precio de plataforma sin que nadie lo
#: hubiera pedido, y sin que ninguna fila se hubiera borrado.
#:
#: ## Por qué el orden no es solo estético
#:
#: La lista llega ordenada de `valido_desde` descendente, y `overrides_de` se queda con el
#: primer elemento que ya ha empezado. El coste de resolver es el de un bucle que se corta en
#: el primero, no el de un `min()` sobre toda la cadena.
_vigentes: dict[uuid.UUID, dict[str, tuple[_PrecioPactado, ...]]] = {}

#: Cuándo se cargó la última vez. Se expone para que el healthcheck pueda avisar si está viejo,
#: que es la diferencia entre «no hay overrides» y «no se han cargado todavía».
_cargado_en: datetime | None = None


def _a_clave(operacion: PriceOperationEnum, alcance: str | None) -> str:
    """La clave interna del override.

    ## Por qué se compone con `alcance` y no solo con la operación

    Porque `CREDIT_PACK_AMOUNT` es la única que necesita saber **de qué** pack habla: un precio
    negociado para el pack de 100 créditos no dice nada del de 250. Sin el alcance, los dos se
    pisarían en el mismo diccionario y el último en escribir ganaría.

    Y el separador es `:` porque no puede aparecer en un nombre de operación ni en la cantidad de
    créditos de un pack, que son enteros.
    """

    return operacion.value if alcance is None else f"{operacion.value}:{alcance}"


def un_pactado(
    valor: Decimal,
    valido_desde: datetime,
    valido_hasta: datetime | None = None,
) -> _PrecioPactado:
    """Un pactado vivo, como lo que se guarda en la caché.

    ## Por qué existe esta fábrica y no se construyen las filas a mano

    Porque hay dos sitios que necesitan un pactado: `cargar_overrides`, que lo lee de la base, y
    las pruebas, que lo inventan. Si cada uno construye el dataclass por su cuenta, el dácimo
    parámetro que se le añada a uno no lo tiene el otro, y el fallo sale en pruebas con el
    nombre de otra cosa.

    ## Por qué no se exporta el dataclass

    ## Por qué es pública y el dataclass no

    Porque la forma de la caché es un detalle de esta versión, y una prueba que dependiera
    de ella habría que reescribirse cada vez que se cambie cómo se representa un pactado en
    memoria. Lo que la prueba necesita es poder decir «este pactado vale 3 desde ayer», y eso se
    dice con números y fechas.
    """

    return _PrecioPactado(valor=valor, valido_desde=valido_desde, valido_hasta=valido_hasta)


async def cargar_overrides(session: AsyncSession) -> int:
    """Lee los overrides de todas las organizaciones y los deja fijados.

    ## Por qué solo se cargan los que **no** tienen `valido_hasta` puesto

    Porque un override cerrado ya no va a volver a aplicar. Recogerlos todos sería guardar en
    memoria un histórico que para cobrar no sirve: la fila está en la base, en la traza y en el
    panel, y aquí solo hace falta el presente.
    """

    global _vigentes, _cargado_en
    filas = (
        await session.execute(
            select(OrganizationPriceOverride).where(
                OrganizationPriceOverride.valido_hasta.is_(None)
            )
        )
    ).scalars().all()

    cargados: dict[uuid.UUID, dict[str, list[_PrecioPactado]]] = {}
    for fila in filas:
        cargados.setdefault(fila.organization_id, {}).setdefault(
            _a_clave(fila.operacion, fila.alcance), []
        ).append(
            un_pactado(
                valor=fila.valor,
                valido_desde=fila.valido_desde,
                valido_hasta=fila.valido_hasta,
            )
        )

    # Se ordena **una vez**, al cargar, y no en cada cobro.
    #
    # ## Por qué aquí y no al leer
    #
    # Porque al leer se recorre la cadena hasta el primero vigente, y eso ocurre en cada cobro
    # de cada escaneo de cada cliente. Ordenar en la carga es una vez por proceso; ordenar en el
    # cobro son millones, y la diferencia se nota en el worker.
    #
    # Y el desempate por `created_at` no hace falta: dos pactados con `valido_desde` idéntico
    # solo pueden venir de un `INSERT` multiple, y ahí da igual cuál gane porque valen
    # lo mismo para el cobro.
    _vigentes = {
        organizacion: {
            clave: tuple(sorted(cadena, key=lambda p: p.valido_desde, reverse=True))
            for clave, cadena in operaciones.items()
        }
        for organizacion, operaciones in cargados.items()
    }
    _cargado_en = datetime.now(UTC)
    return len(filas)


def fijar_overrides(
    valores: dict[uuid.UUID, dict[str, tuple[_PrecioPactado, ...]]] | None,
) -> None:
    """Fija —o con `None` descarta— los overrides. Existe para los tests.

    ## Por qué hace falta

    Porque la caché es estado global del proceso, y un test que pacte un precio y no lo devuelva
    deja el proceso entero cobrando a ese número durante el resto de la sesión. El fallo aparecería
    en otro test, con un nombre que no habla de precios.
    """

    global _vigentes, _cargado_en
    _vigentes = valores or {}
    _cargado_en = datetime.now(UTC) if valores is not None else None


def overrides_de(organization_id: uuid.UUID) -> dict[str, _PrecioPactado]:
    """Los overrides **vigentes** de una organización, ya filtrados por fecha.

    El filtro de fechas se hace aquí y no solo al cargar, por lo que explica la segunda razón
    del docstring del módulo: un override caduca solo.

    ## Por qué el filtro mira **las dos** fechas y no solo `valido_hasta`

    Porque con el vigente reinando el de `valido_desde` más reciente, pactar una subida con
    fechas de futuro es el caso normal de una renovación, y si el filtro solo mirase
    `valido_hasta` ese pactado —que no tiene fecha de fin— empezaría a aplicar el día que se
    escribió, que es justo cuando el cliente aún está en el precio viejo y el comercial
    creía que ya estaba avisado.

    Comprobando `valido_desde <= ahora` el precio viejo sigue mandando hasta la fecha pactada, y
    solo entonces entra el nuevo. Sin esto, un pactado con `valido_desde` en el futuro es un
    precio que se aplica antes de existir.
    """

    ahora = datetime.now(UTC)
    vigentes: dict[str, _PrecioPactado] = {}
    for clave, cadena in _vigentes.get(organization_id, {}).items():
        for pactado in cadena:
            if pactado.valido_desde > ahora:
                # Todavía no ha empezado. Como la cadena va de más reciente a más
                # antigua, el siguiente es el que empezó el día de hoy.
                continue
            if pactado.valido_hasta is not None and pactado.valido_hasta <= ahora:
                # Caducado. Se sigue bajando: puede haber uno anterior que siga vivo.
                continue
            vigentes[clave] = pactado
            break
    return vigentes


#: Qué campo de `PlatformPrices` modifica cada operación.
#:
#: ## Por qué solo tres de las siete
#:
#: ## Por qué solo tres, y las otras cuatro se resuelven en otro sitio
#:
#: Porque estas siete no viven todas en `PlatformPrices`: tres son escalares de cobro de escaneo y
#: están aquí, y las otras cuatro —revisión de PR, recargo de LLM, suscripción Pro y packs— son de
#: otros modelos y se resuelven en `catalogo` y en el router. Meter aquí un mapa que cubre medio
#: sistema daría la falsa impresión de que esta función lo resuelve todo.
#:
#: Y una operación sin campo equivalente devuelve `None` y **no se aplica**, en vez de falla: un
#: proceso con una versión antigua del enum debe seguir cobrando, no caerse.
_CAMPO_DE_OPERACION: dict[PriceOperationEnum, str] = {
    PriceOperationEnum.SCAN_CREDIT_COST: "scan_credit_cost",
    PriceOperationEnum.QUICK_SCAN_MULTIPLIER: "quick_scan_credit_multiplier",
    PriceOperationEnum.CREDITS_PER_USD: "credits_per_usd",
}


def precios_de(organization_id: uuid.UUID | None) -> PlatformPrices:
    """Los precios que rigen a esta organización, con lo pactado encima de lo de plataforma.

    ## Por qué `organization_id=None` devuelve los precios de plataforma

    Porque hay llamadores que **no** tienen organización —un cálculo de diagnóstico, un test— y
    obligarlos a inventar un `UUID` para volver a conseguir el precio de plataforma sería un rodeo.
    `None` significa «nadie ha pactado nada con nadie», que es exactamente lo que devuelve.

    ## Por qué se usa `replace` y no construir el dataclass a mano

    Porque `PlatformPrices` tiene siete campos y van a crecer. Reconstruirlo a mano en cada
    aplicación de override es la forma de que añadir un campo al modelo y no a esta función pase
    desapercibido: el precio nuevo saldría siempre el de plataforma, en silencio, y nadie lo vería
    hasta una queja. Con `replace` solo se nombra el campo que cambia.
    """

    # Import local y no de nivel de modulo: `pricing` llama a esta funcion a su vez, y
    # los dos importarse arriba serian un ciclo. Aqui no puede haberlo porque la
    # importacion ocurre cuando ya se esta usando la funcion, con ambos modulos ya
    # cargados.

    from backend.apps.billing.pricing import precios_vigentes

    base = precios_vigentes()
    if organization_id is None:
        return base

    pactados = overrides_de(organization_id)
    if not pactados:
        return base

    cambios: dict[str, Decimal] = {}
    for clave, pactado in pactados.items():
        operacion = clave.split(":", 1)[0]
        try:
            enum = PriceOperationEnum(operacion)
        except ValueError:
            # Una clave que no es del enum actual se ignora en vez de reventar: el catálogo de
            # operaciones puede crecer, y un proceso con una versión antigua no puede caerse
            # entero por un override que no entiende.
            continue
        campo = _CAMPO_DE_OPERACION.get(enum)
        if campo is not None:
            cambios[campo] = pactado.valor

    return replace(base, **cambios) if cambios else base
