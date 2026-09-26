"""Catálogo de eventos que un endpoint de webhook puede suscribirse.

## Por qué el catálogo es explícito y no se deriva de las rutas

Un webhook es un contrato a largo plazo: quien lo registra decide qué hace su sistema
cuando llegue `pentest.completed`, y rompe el día que ese nombre cambia. Derivar el
catálogo de las rutas del backend lo convertiría en un contrato que cambia con cada
refactorización, sin que nadie lo decidiera. Aquí los nombres son **estables por
decisión** y añadir un evento es añadir una entrada, no renombrar algo que ya se
entregó.

## Los nombres terminan en pasado

`pentest.completed`, no `pentest.complete`. Un webhook describe **algo que ya pasó**, y
el tiempo verbal en pasado evita la ambigüedad de un `issue.updated` que puede ser una
creación o una transición de estado. Cuando haga falta distinguir transiciones, se
agrega otro evento en vez de sobrecargar el existente: quien recibe
`vulnerability.status_changed` ya sabe que el resto de campos no cambiaron.

## El grupo no es parte del nombre

Los eventos llevan el recurso delante (`pentest.`, `vulnerability.`), así que el grupo se
obtiene partiendo el nombre. El panel agrupa por ahí, y por eso añadir un grupo nuevo no
obliga a tocar el frontend.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class EventType(StrEnum):
    """Los eventos que la plataforma emite hoy.

    Cada valor es una promesa sobre lo que lleva el payload. Cambiar el payload de un
    evento existente rompe a quien ya lo consume, de modo que **añadir campos es
    válido y quitar o renombrar no lo es**.
    """

    # Pentests
    PENTEST_COMPLETED = "pentest.completed"
    PENTEST_FAILED = "pentest.failed"
    PENTEST_TIMED_OUT = "pentest.timed_out"
    PENTEST_ABORTED = "pentest.aborted"

    # Vulnerabilidades
    VULNERABILITY_CREATED = "vulnerability.created"
    VULNERABILITY_STATUS_CHANGED = "vulnerability.status_changed"
    VULNERABILITY_AUTOFIXED = "vulnerability.autofixed"

    # Revisiones de PR
    PR_REVIEW_COMPLETED = "pr_review.completed"
    PR_REVIEW_FAILED = "pr_review.failed"
    PR_COMMENT_POSTED = "pr_review.comment_posted"
    PR_AUTOFIX_OPENED = "pr_review.autofix_opened"

    # Créditos
    CREDITS_PURCHASED = "billing.credits_purchased"
    CREDITS_LOW = "billing.credits_low"

    # Ping: no es un evento de dominio, es la prueba de conexión. Está en el catálogo
    # porque el endpoint tiene que poder suscribirse a él para funcionar, y porque
    # quien lo recibe necesita poder distinguir una prueba de un evento real.
    PING = "ping"


@dataclass(frozen=True, slots=True)
class EventDefinition:
    """Un evento y sus metadatos de presentación.

    `label_key` se **deriva** del nombre del evento y no se escribe a mano. Catorce cadenas
    escritas a mano acabaron con seis que no coincidían con la acción real: el catálogo
    decía `events.pentest.timedOut` para un evento llamado `pentest.timed_out`. La
    incoherencia no se vio porque el panel reconstruía la ruta por su cuenta y nunca leía
    la clave. Con la ruta derivada, una traducción que falte se nota —el panel enseña el
    `defaultValue`— y una que sobra la detecta la auditoría.

    `group` se deriva por el mismo motivo: duplicarlo permitiría que un evento apareciera
    en el grupo equivocado sin que nada lo señalara.
    """

    event_type: EventType
    description_key: str

    @property
    def label_key(self) -> str:
        """La ruta de traducción, derivada de la forma `eventos.<recurso>.<accion>`.

        El panel la consume **tal cual** y no la reconstruye. `ping` no lleva punto, así
        que la partición da una acción vacía y una ruta montada a mano quedaría
        `events.ping.` con un punto de más que no resuelve a nada.
        """

        return f"events.{self.group}.{self.action}"

    @property
    def group(self) -> str:
        return self.event_type.value.split(".", 1)[0]

    @property
    def action(self) -> str:
        """La parte posterior al punto, o el nombre entero si no lo hay.

        `ping` no se parecería a ningún otro evento, así que no encaja en el esquema
        `recurso.accion` y se declara aparte. La propiedad devuelve el nombre entero en
        ese caso para que quien la use no tenga que comprobar si hubo partición.
        """

        _, separador, accion = self.event_type.value.partition(".")
        return accion if separador else self.event_type.value


# El orden es el que ve el panel: pentests primero porque es lo que un cliente de Fenix
# quiere recibir casi siempre, y lo raro al final.
EVENT_CATALOG: Final[tuple[EventDefinition, ...]] = (
    EventDefinition(EventType.PENTEST_COMPLETED, "events.pentest.completedHint"),
    EventDefinition(EventType.PENTEST_FAILED, "events.pentest.failedHint"),
    EventDefinition(EventType.PENTEST_TIMED_OUT, "events.pentest.timedOutHint"),
    EventDefinition(EventType.PENTEST_ABORTED, "events.pentest.abortedHint"),
    EventDefinition(EventType.VULNERABILITY_CREATED, "events.vulnerability.createdHint"),
    EventDefinition(
        EventType.VULNERABILITY_STATUS_CHANGED, "events.vulnerability.statusChangedHint"
    ),
    EventDefinition(
        EventType.VULNERABILITY_AUTOFIXED, "events.vulnerability.autofixedHint"
    ),
    EventDefinition(EventType.PR_REVIEW_COMPLETED, "events.prReview.completedHint"),
    EventDefinition(EventType.PR_REVIEW_FAILED, "events.prReview.failedHint"),
    EventDefinition(
        EventType.PR_COMMENT_POSTED, "events.prReview.commentPostedHint"
    ),
    EventDefinition(
        EventType.PR_AUTOFIX_OPENED, "events.prReview.autofixOpenedHint"
    ),
    EventDefinition(
        EventType.CREDITS_PURCHASED, "events.billing.creditsPurchasedHint"
    ),
    EventDefinition(EventType.CREDITS_LOW, "events.billing.creditsLowHint"),
    EventDefinition(EventType.PING, "events.pingHint"),
)

KNOWN_EVENTS: Final[frozenset[str]] = frozenset(
    definition.event_type.value for definition in EVENT_CATALOG
)

#: Eventos agrupados por recurso, que es como los presenta el selector. Es un
#: `MappingProxyType` y no un dict para que ningún importador pueda mutarlo en caliente y
#: dejar el panel y el validador usando catálogos distintos.
EVENTS_BY_GROUP: Final[MappingProxyType[str, tuple[EventDefinition, ...]]] = (
    MappingProxyType(
        {
            group: tuple(definition for definition in EVENT_CATALOG if definition.group == group)
            for group in dict.fromkeys(definition.group for definition in EVENT_CATALOG)
        }
    )
)

#: Eventos de dominio, es decir, los que no son la prueba de conexión. Sirve para lo que
#: emite la plataforma: un ping nunca se emite por iniciativa propia.
DOMAIN_EVENTS: Final[tuple[EventType, ...]] = tuple(
    definition.event_type
    for definition in EVENT_CATALOG
    if definition.event_type is not EventType.PING
)


def event_is_known(value: str) -> bool:
    """Indica si el valor es un evento del catálogo.

    Comparación exacta y sin normalizar: un nombre con espacios es un error del cliente y
    merece un `422` que lo diga, no un evento que nunca llega.
    """

    return value in KNOWN_EVENTS


def unknown_events(values: list[str]) -> list[str]:
    """Devuelve los que no pertenecen al catálogo, en el orden recibido.

    Se usa para que el `422` pueda enumerar los concretos, en vez de un "evento
    inválido" que obliga al cliente a comparar listas a mano.
    """

    return [value for value in values if not event_is_known(value)]


def normalize_events(values: list[str]) -> list[str]:
    """Valida, deduplica y ordena por el orden del catálogo.

    Ordenar por el catálogo y no alfabéticamente es lo que hace que la fila guardada sea
    comparable: dos peticiones con los mismos eventos en distinto orden producen la misma
    lista, y el panel no ve dos endpoints idénticos como distintos.
    """

    rejected = unknown_events(values)
    if rejected:
        raise ValueError(f"Eventos no reconocidos: {', '.join(rejected)}")
    unicos = set(values)
    return [
        definition.event_type.value
        for definition in EVENT_CATALOG
        if definition.event_type.value in unicos
    ]
