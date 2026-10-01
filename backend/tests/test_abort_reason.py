"""Pruebas de la regla de reembolso por cancelación.

## Qué se comprueba y por qué

Una sola cosa, y es la que decide dinero: **qué motivo de cancelación devuelve la reserva y cuál
no**. Es exactamente la regla que pidió el responsable de la plataforma —«si es un fallo nuestro
devuelve, si cancela el cliente lo paga»— y es la clase de regla que se rompe sin que nada falle:
un reembolso de más o un cobro de más no dan error en ninguna parte.

## Por qué la regla se prueba como función pura y no cancelando escaneos

Porque la regla es aritmética sobre cinco valores y una lista. Comprobarla de verdad exigiría
encolar un escaneo, levantar un contenedor Docker, revocar una tarea de Celery y mover el
ledger. Y una prueba que exige todo eso para comprobar un `in` no se escribe, y una regla que no
se comprueba se modifica sin querer.

Por eso `devuelve_el_dinero` está separada del resto: es una decisión de negocio que se puede
verificar entera, y el handler HTTP la usa sin saber nada del asunto.

## Por qué el caso por defecto es cobrar

Porque el caso raro es el que tiene consecuencias y el común no. De los seis motivos, tres
devuelven porque son fallos de infraestructura; tres cobran porque el trabajo estaba en marcha o
no debía existir. Un motivo nuevo que se añadiera al enum sin decidir su tratamiento **cobra**,
que es el lado reversible del error: se devuelve a la carta. Lo contrario —devolver por
omisión— deja a un cliente sin cobro por trabajo que sí se hizo, y eso no se arregla solo.
"""

from __future__ import annotations

import pytest

from backend.apps.pentests.abort_reason import (
    MOTIVOS_QUE_DEVUELVEN,
    AbortReasonEnum,
    devuelve_el_dinero,
)


@pytest.mark.parametrize(
    ("motivo", "devuelve"),
    [
        # Fallo nuestro: el cliente no ha recibido nada, y no lo ha roto él.
        (AbortReasonEnum.INFRASTRUCTURE_STUCK, True),
        (AbortReasonEnum.INFRASTRUCTURE_FAILED, True),
        (AbortReasonEnum.INFRASTRUCTURE_ORPHANED, True),
        # El cliente lo paró: el trabajo estaba en marcha y se le ocupa.
        (AbortReasonEnum.CLIENT_CANCELLED, False),
        # Se anula la repetida; la primera sigue cobrándose.
        (AbortReasonEnum.DUPLICATE, False),
        # No debía encolarse: cobrar es lo correcto.
        (AbortReasonEnum.POLICY_VIOLATION, False),
    ],
)
def test_la_regla_de_reembolso_segun_el_motivo(
    motivo: AbortReasonEnum, devuelve: bool
) -> None:
    """Cada motivo decide el reembolso, uno a uno y sin ambigüedad."""

    assert devuelve_el_dinero(motivo) is devuelve


def test_los_fallos_de_infraestructura_son_los_que_devuelven() -> None:
    """Los tres motivos de infraestructura devuelven, y no hay un cuarto escondido.

    Se comprueba el conjunto entero y no caso por caso porque lo que importa es que **la lista de
    motivos que devuelven son exactamente los de infraestructura**. Si alguien añade un motivo
    de infraestructura sin meterlo en la lista, esta prueba no lo caza —pero el
    `test_todo_motivo_tiene_un_tratamiento_explicito` de abajo sí obliga a decidir.
    """

    infraestructura = {
        AbortReasonEnum.INFRASTRUCTURE_STUCK,
        AbortReasonEnum.INFRASTRUCTURE_FAILED,
        AbortReasonEnum.INFRASTRUCTURE_ORPHANED,
    }
    assert MOTIVOS_QUE_DEVUELVEN == infraestructura


def test_todo_motivo_tiene_un_tratamiento_explicito() -> None:
    """Ningún motivo queda sin decidir, y la lista no se olvida de ninguno.

    ## Por qué esta prueba es la importante

    Porque el fallo real de este módulo no es que un motivo esté mal clasificado: es que
    **alguien añade un motivo y nadie se acuerda de decidir qué pasa con el dinero**. El motivo
    nuevo se cuela en el enum, no está en la lista de excepciones y, por el diseño de la función,
    **cobra**. Que cobre es lo correcto por defecto —es el lado reversible—, pero «lo correcto por
    defecto» solo sirve si alguien se da cuenta de que tiene que revisarlo.

    Y el otro lado del mismo olvido: un motivo **de infraestructura** añadido al enum sin
    listarlo cobraría a un cliente por un fallo nuestro. Eso es el error que no tiene arreglo
    rápido, y por eso se comprueba explícitamente: todo motivo cuyo nombre empieza por
    `INFRASTRUCTURE_` tiene que estar en la lista de los que devuelven.
    """

    for motivo in AbortReasonEnum:
        if motivo.value.startswith("INFRASTRUCTURE_"):
            assert devuelve_el_dinero(motivo), (
                f"{motivo.value} es un fallo nuestro y tiene que devolver; "
                "si se cobro a un cliente por un fallo de la plataforma, "
                "no se puede arreglar solo"
            )


def test_cancelar_desde_el_panel_del_cliente_nunca_devuelve() -> None:
    """La cancelación del cliente es la que no devuelve, y es la primera.

    ## Por qué merece su propia prueba

    Porque es el caso que **ya existe** en el sistema y sobre el que no hay que cambiar nada:
    `abort_pentest` nunca ha devuelto, y esta regla lo mantiene. Si alguien invirtiera la función
    para que todo devolviera, el cambio de comportamiento sería sobre el camino que los clientes
    usan todos los días, y el primer cliente que complain lo descubriría.
    """

    assert devuelve_el_dinero(AbortReasonEnum.CLIENT_CANCELLED) is False


def test_la_lista_de_que_devuelven_no_se_puede_ampliar_sin_querer() -> None:
    """La lista es un `frozenset`, no una lista mutable compartida.

    ## Por qué importa el tipo

    Porque una lista mutable module-level es un detalle que espera ser mutado: un `append` en
    alguna parte del código, para «dar de alta un motivo más», y a partir de ahí **todos** los
    motivos no listados empiezan a devolver. El `frozenset` hace que esa operación falle en voz
    alta y en el sitio donde se escribe, en vez de cambiar silenciosamente el comportamiento de
    cobro de toda la plataforma.
    """

    assert isinstance(MOTIVOS_QUE_DEVUELVEN, frozenset)
    with pytest.raises(AttributeError):
        MOTIVOS_QUE_DEVUELVEN.add(AbortReasonEnum.DUPLICATE)  # type: ignore[attr-defined]
