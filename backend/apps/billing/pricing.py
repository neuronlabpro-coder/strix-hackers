"""Precio en créditos de las operaciones facturables, y la **fuente única** de la paridad.

Vive aquí y no en el router de pentests porque el worker de Strix también lo
necesita para ajustar la reserva, y el router importa al worker: dejarlo en el
router cerraría un ciclo de importación.

## Por qué este módulo y no el modelo

Porque leer un precio es una operación de **cálculo** y no de persistencia, y porque
`llm_router.pricing` es deliberadamente una función pura sin base de datos: el
desglose de un cobro tiene que poder auditarse sin levantar el sistema. Este módulo
es la capa intermedia que traduce «lo que dice la base» en «el número que multiplica
importes», sin arrastrar una sesión a todas partes.

## Por qué hay una instantánea en memoria y no una consulta por cobro

Porque el precio se lee en sitios donde **no hay sesión**: el worker de Strix ajusta
la reserva desde una tarea de Celery, `agents/service.py` cobra desde un webhook y
`repositories/pipeline.py` cobra al terminar una revisión. Meter una sesión en las
cuatro obligaría a cambiar la firma de cuatro funciones y a enhebrar la sesión por
unos doce llamadores, y el resultado sería la misma lógica de precio repartida por
medio dozen de ficheros, que es exactamente el problema que se vino a arreglar.

## Por qué la instantánea se puede quedar obsoleta sin corromper nada

Porque **el precio no se relee durante la vida de un cobro**: la reserva se congela
cuando se encola el escaneo y el asiento del ledger guarda el delta ya cobrado. Una
instantánea obsoleta solo significa que un trabajo en vuelo se liquidó al precio del
momento en que se encoló, que es justo lo que quiere el cliente que lo pidió.

## Por qué la configuración sigue siendo el valor de arranque

Porque la fila de la base la crea la migración. Si esa fila **no existe** —una base
restaurada de antes de la migración, una fila borrada por error, un worker levantado
antes de que la corra el despliegue— la aplicación tiene que seguir cobrando. Con
`Settings` como respaldo, cobra; sin él, arrancaría sin precios y el primer escaneo
fallaría con un error que no menciona el precio. El respaldo no es un atajo: es lo
que permite desplegar la base y el código por separado.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from backend.apps.billing.models import PlatformPricing
from backend.apps.billing.organization_prices import precios_de
from backend.apps.pentests.models import ScanModeEnum
from backend.core.config import settings
from backend.core.database import AsyncSession

_CREDIT_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class PlatformPrices:
    """Los cuatro precios escalares de la plataforma, en un valor inmutable.

    ## Por qué un `dataclass` congelado y no la fila del modelo

    Porque este valor se pasa a funciones puras de cálculo y se compara en tests, y
    la fila del modelo lleva sesión, `_sa_instance_state` y columnas que cambian. Un
    valor plano que se puede pasar por valor hace imposible que alguien lo modifique
    «sin querer» mutando un objeto compartido entre dos peticiones.

    ## Por qué `Decimal` y nunca `float`

    Porque un `float` de ocho decimales ya ha perdido precisión al construirse, y el
    precio de un crédito con ocho decimales es un cociente que multiplica importes
    reales. Un céntimo de deriva por redondeo binario en la paridad se convierte en
    una diferencia de miles de euros al cerrar el mes.
    """

    credits_per_usd: Decimal
    scan_credit_cost: Decimal
    quick_scan_credit_multiplier: Decimal
    low_credit_balance_threshold: Decimal

    @classmethod
    def desde_configuracion(cls) -> PlatformPrices:
        """Los precios que declara la configuración, sin tocar la base.

        Es el respaldo y también el estado inicial: hasta que alguien llame a
        `fijar_precios`, este es el valor que ve todo el sistema, y por eso la suite
        de tests sigue siendo válida sin sembrar nada.
        """

        return cls(
            credits_per_usd=Decimal(settings.credits_per_usd),
            scan_credit_cost=Decimal(settings.scan_credit_cost),
            quick_scan_credit_multiplier=Decimal(settings.quick_scan_credit_multiplier),
            low_credit_balance_threshold=Decimal(settings.low_credit_balance_threshold),
        )

    @classmethod
    def desde_fila(cls, fila: PlatformPricing) -> PlatformPrices:
        """Los precios que dice la base, con los valores ya cuantizados.

        El `Decimal` que devuelve el driver viene en `numeric(18, 8)`, pero un
        cliente puede enviar `0.30000000000000004`. Cuantizar aquí, en el punto de
        entrada, deja un único sitio donde la normalización ocurre: si se hiciera en
        cada cálculo, dos precios visualmente iguales se compararían como distintos.
        """

        return cls(
            credits_per_usd=fila.credits_per_usd.quantize(_PRECISION_PRECIOS),
            scan_credit_cost=fila.scan_credit_cost.quantize(_PRECISION_PRECIOS),
            quick_scan_credit_multiplier=fila.quick_scan_credit_multiplier.quantize(
                _PRECISION_PRECIOS
            ),
            low_credit_balance_threshold=fila.low_credit_balance_threshold.quantize(
                _PRECISION_PRECIOS
            ),
        )


#: La escala con la que se guardan y se comparan los precios. Coincide con el
#: `Numeric(18, 8)` de la columna, para que cuantizar no cambie el número guardado.
_PRECISION_PRECIOS = Decimal("0.00000001")

#: La instantánea viva. `None` significa «nadie ha cargado nada todavía».
#:
#: ## Por qué `None` y no un objeto con los valores de la configuración
#:
#: ## Por qué empieza en `None` y no en los valores de la configuración
#:
#: Porque son dos estados distintos y significan cosas distintas. `None` quiere decir
#: «el arranque todavía no ha hablado con la base», y eso es una condición que un
#: operador necesita poder ver en el healthcheck. Si arrancara ya con los valores de
#: la configuración, un despliegue en el que la carga fallara sería **indistinguible**
#: de uno correcto: la aplicación cobraría y nadie se enteraría de que está cobrando
#: con precios de arranque mientras el panel muestra otros.
_vigentes: PlatformPrices | None = None


def precios_vigentes() -> PlatformPrices:
    """Los precios que rigen ahora mismo, con respaldo en la configuración.

    Nunca lanza. Si la base no se pudo leer, devuelve la configuración, porque un
    fallo al cargar un precio no puede dejar la plataforma sin poder cobrar: es peor
    cobrar al precio de arranque que no cobrar.
    """

    if _vigentes is None:
        return PlatformPrices.desde_configuracion()
    return _vigentes


def fijar_precios(precios: PlatformPrices | None) -> None:
    """Fija —o con `None` descarta— la instantánea de precios.

    ## Por qué existe un `fijar_precios(None)`

    Para que los tests puedan volver al estado de arranque sin tener que saber qué
    dejó puesto el test anterior. Un test que cambia los precios y no los devuelve
    deja el proceso cobrando a un precio que nadie eligió, y el fallo aparece en otro
    test, a otro fichero, con un nombre que no habla de precios.
    """

    global _vigentes
    _vigentes = precios


async def cargar_precios(session: AsyncSession) -> PlatformPrices | None:
    """Lee la fila de precios y la fija. Devuelve `None` si **no hay fila**.

    ## Por qué devuelve `None` en vez de lanzar

    Porque «la tabla existe y está vacía» y «la tabla no existe» son el mismo
    fallo desde el punto de vista de quien la lee, y los dos tienen la misma
    respuesta correcta: cobrar con la configuración y avisar. Si esto lanzara, el
    arranque de la aplicación dependería de que una migración concreta se hubiera
    aplicado, que es un acoplamiento que ningún otro módulo del proyecto tiene.

    ## Por qué una fila y no un `SELECT` con orden y límite

    Porque la tabla tiene `CHECK (id = 1)`. No hay dos filas que ordenar ni ninguna
    que pueda aparecer después: hay exactamente una, o no hay ninguna.
    """

    global _vigentes
    fila = (
        await session.execute(select(PlatformPricing).where(PlatformPricing.id == 1))
    ).scalar_one_or_none()
    if fila is None:
        _vigentes = None
        return None
    _vigentes = PlatformPrices.desde_fila(fila)
    return _vigentes


def scan_credit_cost(
    scan_mode: ScanModeEnum,
    organization_id: uuid.UUID | None = None,
    *,
    effective_prices: PlatformPrices | None = None,
) -> Decimal:
    """Coste en créditos de un escaneo según su modo.

    Un escaneo `QUICK` se cobra proporcionalmente porque consume una fracción de los
    tokens de uno `DEEP`, y la proporción también es un precio editable.

    ## Por qué `organization_id` es opcional y no obligatorio

    Porque hay llamadores que **no** tienen organización: un cálculo de diagnóstico, un
    Porque hay llamadores que **no** tienen organización: un cálculo, un informe, una prueba.
    un rodeo, y `None` significa exactamente lo que dice: «nadie ha pactado nada con nadie».

    ## Por qué el valor por defecto no rompe a nadie

    Porque es el precio de plataforma, que es lo que se cobraba antes de que existiera la
    negociación: un llamador que no se actualice sigue cobrando lo correcto.

    ## Por qué el nombre no cambia al mover el precio a la base

    Porque hay cuatro llamadores —el servicio de pentests, el worker de Strix, el
    servicio de agentes y el de repositorios— y renombrar la función para que suene
    más «nueva» cambiaría cuatro ficheros y un test para no ganar nada. Lo que cambia
    es de dónde sale el número, que es lo único que este módulo promised.

    Porque esta función se llama **una vez**, al encolar, y lo que congela el precio
    es el asiento del ledger que esa llamada produce. Releer el precio después sería
    incorrecto: si el precio sube entre el encolado y el ajuste, el ajuste pagaría la
    subida de trabajos que el cliente ya aceptó al precio viejo. La función se
    mantiene sin estado justamente para que ese error no se pueda escribir.
    """

    precios = effective_prices if effective_prices is not None else precios_de(organization_id)
    base = precios.scan_credit_cost
    if scan_mode == ScanModeEnum.QUICK:
        return (base * precios.quick_scan_credit_multiplier).quantize(_CREDIT_QUANTUM)
    return base.quantize(_CREDIT_QUANTUM)


def credits_per_usd(organization_id: uuid.UUID | None = None) -> Decimal:
    """La paridad vigente: cuántos créditos vale un dólar.

    ## Por qué esta función existe y no un atributo de `settings`

    Porque `settings.credits_per_usd` es el valor de arranque y no el vigente, y la
    diferencia entre los dos es precisamente el bug que se vino a arreglar: el chat
    leía su propia constante y cobraba a 1:1 mientras todo lo demás cobraba a lo que
    dijera la variable. Una función con un solo cuerpo no puede tener dos fuentes.
    """

    return precios_de(organization_id).credits_per_usd
