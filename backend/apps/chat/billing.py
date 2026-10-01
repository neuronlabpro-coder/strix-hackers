"""Liquidación del consumo de un paso del chat.

## Por qué el coste sale de `compute_charge` y no de una fórmula propia

Porque `compute_charge` **ya** es la regla del proyecto: toma los tres números del catálogo —
`base_cost_input_m`, `base_cost_output_m` y `markup_pct`—, calcula coste base, precio al
cliente y beneficio, y redondea a microdólar antes de convertir a créditos para que el error no
se acumule. Escribir aquí una aritmética propia significaría dos fórmulas de marguen que
divergen en la sexta cifra decimal, y el sitio donde se detecta es una discusión de
facturación.

## Por qué se cobra por **paso** y no al cerrar la conversación

Porque el consumo ocurre durante la conversación, y un cierre único obligaría a decidir
qué pasa si el usuario cierra la pestaña a mitad de una respuesta: o se cobra de más por algo
que no llegó a entregarse, o se regala el trabajo ya hecho. Cobrando al guardar cada mensaje
del asistente, cada fila de `chat_messages` tiene su precio y su asiento, y la suma cuadra con
el saldo sin depender de que la conversación se cerrara.

## Por qué el modelo se resuelve con `use_case="CHAT"` y no se pasa a mano

Porque el modelo que responde no siempre es el que se pidió. La cadena se resuelve por
prioridad con reintento encadenado cuando un fallo mejora cambiando de modelo, así que el
precio del mensaje lo determina el modelo que **realmente** respondió. Registrar en el mensaje
ese `model_id` y no el pedido es lo que hace que una disputa de factura se responda con el
número, y no con "el total cuadra".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import LedgerReasonEnum
from backend.apps.billing.pricing import credits_per_usd as paridad_vigente
from backend.apps.billing.service import InsufficientCreditsError, apply_credit_delta
from backend.apps.llm_router.models import LLMModelConfig, LLMUseCaseEnum
from backend.apps.llm_router.pricing import ChargeBreakdown, compute_charge
from backend.apps.llm_router.routing import LLMAllModelsInactiveError, resolve_model_chain

#: La paridad **no** es una constante de este modulo.
#:
#: ## Por qué aquí no hay constante
#:
#: Porque era el cuarto sitio donde la paridad estaba escrita, y era el único que
#: **miente**: el comentario de al lado decía que el valor se reexportaba del módulo de
#: atribución para no repetirlo, y en la línea siguiente había un `Decimal("1")` escrito a
#: mano. Nadie lo notó porque el `default` de `CREDITS_PER_USD` en la configuración también
#: es `1.00`, así que los dos valores coincidían por casualidad.
#:
#: La casualidad es exactamente lo que hace peligroso un duplicado: el día que alguien
#: subiera la paridad en la configuración para cobrar más créditos por dólar, este módulo
#: seguiría pagando a 1:1, y el desajuste aparecería en la conciliación de fin de mes, en
#: forma de miles de euros sin destinatario. Con `paridad_vigente()` no hay segundo valor
#: que pueda quedar viejo: si la fila de la base cambia, el chat cambia con ella en la
#: misma lectura.
#:
#: Y la paridad se lee en el **momento del cobro**, no al importar el módulo, que es lo que
#: hace que una edición de precio desde el panel se aplique sin reiniciar el proceso.

_CUATRO_DECIMALES: Decimal = Decimal("0.0001")


class ChatNoModelAvailableError(RuntimeError):
    """No hay ningun modelo activo para el caso de uso `CHAT`.

    De dominio, no `HTTPException`: la ruta la traduce. El mensaje dice lo que hay que hacer
    porque el otro sintoma posible —un chat que se queda pensando sin responder— no dice nada.
    """


#: No hay un `ChatInsufficientCreditsError` propio, y es deliberado.
#:
#: `apply_credit_delta` ya lanza `InsufficientCreditsError` con lo obligatorio y lo disponible,
#: y conoce el caso de sobregiro que la comprobacion previa no cubre: dos peticiones simultaneas
#: que leen el mismo saldo y ambas lo dan por bueno. Un error propio seria una segunda taxonomia
#: para lo mismo, y la ruta tendria que conocer las dos. Se reexporta el de la facturacion para
#: que quien importe este modulo no tenga que saber de donde sale.
InsufficientCreditsError = InsufficientCreditsError


@dataclass(frozen=True)
class ChatStepCharge:
    """Lo que se cobró por un paso, y con qué datos.

    Se devuelve entero y no se queda solo en la base, para que la ruta pueda responder al
    cliente lo que ha costado su pregunta **antes** de que sepinte en el libro contable. Un
    saldo que baja sin explicación en pantalla se lee como un error.
    """

    #: Modelo que realmente respondió, no el que se pidió.
    model_id: str
    prompt_tokens: int
    completion_tokens: int
    #: Precio al cliente, en dólares, ya con el margen aplicado.
    client_price_usd: Decimal
    #: Coste real del proveedor, en dólares.
    base_cost_usd: Decimal
    #: Beneficio: la diferencia entre lo cobrado y lo que cuesta.
    net_profit_usd: Decimal
    #: Créditos efectivamente descontados, redondeados a la escala del saldo.
    credits: Decimal
    #: Asiento del ledger, para poder cotejarlo contra el mensaje.
    ledger_entry_id: uuid.UUID


def _a_creditos(importe_usd: Decimal) -> Decimal:
    """Dólares a créditos con la escala del saldo.

    El redondeo es `ROUND_HALF_UP` y no el por defecto del `Decimal` porque el saldo es
    `numeric(12, 4)` y el redondeo del banco es medio hacia arriba. Con el redondeo a la baja
    por defecto, cada paso redondearia hacia cero y un chat de mil mensajes regalaria
    sistemáticamente una parte del cobro.
    """

    return (importe_usd * paridad_vigente()).quantize(_CUATRO_DECIMALES, rounding=ROUND_HALF_UP)


async def resolve_chat_model(session: AsyncSession) -> LLMModelConfig:
    """El primer modelo activo de la cadena de `CHAT`.

    Delega en `resolve_model_chain`, que ya sabe como combinar el caso de uso especifico con los
    transversales `ALL` y ordenarlos por prioridad. Reimplementar esa combinacion aqui
    significaria que anadir un modelo transversal al chat exigiria tocar dos sitios, y el que se
    olvide deja al chat sin modelo o con el mas caro.
    """

    try:
        cadena = await resolve_model_chain(session, LLMUseCaseEnum.CHAT)
    except LLMAllModelsInactiveError as error:
        # `resolve_model_chain` **lanza** cuando no encuentra cadena; no devuelve una lista
        # vacia. Comprobar `if not cadena` despues seria codigo inalcanzable, y lo es de forma
        # silenciosa: nunca falla la prueba que lo cubre porque la rama no se puede tomar.
        #
        # Se traduce aqui y no en la ruta para que el mensaje sea el del chat, que es mas
        # accionable que el del router: el del router nombra un caso de uso generico, y el
        # usuario que esta configuring el chat necesita saber exactamente que casilla marcar.
        raise ChatNoModelAvailableError(
            "No hay ningun modelo activo para el chat. Activa uno en la consola de modelos con "
            "el caso de uso CHAT, o activa uno transversal con caso de uso ALL."
        ) from error
    return cadena[0]


def calcular_coste_de_paso(
    modelo: LLMModelConfig, *, tokens_in: int, tokens_out: int
) -> ChargeBreakdown:
    """El desglose de lo que cuesta un paso, sin tocar la base de datos.

    Vive separado de la liquidacion para que el calculo se pueda probar con numeros sin
    levantar una sesion, y para que la politica —que numeros entran y como se redondean— se lea
    en un sitio.
    """

    return compute_charge(
        base_cost_input_m=modelo.base_cost_input_m,
        base_cost_output_m=modelo.base_cost_output_m,
        markup_pct=modelo.markup_pct,
        prompt_tokens=tokens_in,
        completion_tokens=tokens_out,
        credits_per_usd=paridad_vigente(),
    )


async def liquidar_paso_de_chat(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    reference_id: str,
    model: LLMModelConfig,
    tokens_in: int,
    tokens_out: int,
) -> ChatStepCharge:
    """Cobra un paso del chat y deja el asiento en el ledger.

    ## Por qué no se comprueba el saldo aquí

    Porque `apply_credit_delta` ya lo hace, con `SELECT ... FOR UPDATE` sobre la organización y
    con su `InsufficientCreditsError`. Reimplementar la comprobación aquí para dar un error
    "bonito" significaría tener dos caminos de gasto de créditos, y el sobregiro entre
    peticiones simultáneas solo lo cierra el que lleva el bloqueo. Delegar es lo que garantiza
    que el chat y los escaneos gastan con las mismas reglas.

    ## Por qué el precio se calcula antes de aplicar el delta

    Para poder devolver el desglose al cliente. Un saldo que baja sin que la respuesta diga
    cuánto costó se lee como un error, y el usuario no tiene forma de saber si cobró de más.

    ## Por qué `reference_id` es obligatorio y único

    Porque el ledger es append-only y no admite correcciones. Sin una referencia estable, un
    reintento del mismo paso crearía un segundo asiento por el mismo trabajo, y la única forma
    de distinguirlo después sería leer los mensajes y sumar, que es exactamente lo que el
    ledger evita.
    """

    if tokens_in < 0 or tokens_out < 0:
        raise ValueError("Los tokens consumidos no pueden ser negativos")

    desglose = calcular_coste_de_paso(model, tokens_in=tokens_in, tokens_out=tokens_out)
    creditos = _a_creditos(desglose.client_price_usd)

    if creditos == Decimal("0.0000"):
        raise ValueError(
            "Un paso con coste cero no se liquida: un asiento de ceroCredits viola la "
            "invariante del ledger, que solo admite movimientos distintos de cero."
        )

    asiento = await apply_credit_delta(
        session=session,
        organization_id=organization_id,
        amount=-creditos,
        reason=LedgerReasonEnum.CHAT_STEP_CONSUMPTION,
        reference_id=reference_id,
    )

    return ChatStepCharge(
        model_id=model.model_id,
        prompt_tokens=tokens_in,
        completion_tokens=tokens_out,
        client_price_usd=desglose.client_price_usd,
        base_cost_usd=desglose.base_cost_usd,
        net_profit_usd=desglose.net_profit_usd,
        credits=creditos,
        ledger_entry_id=asiento.id,
    )
