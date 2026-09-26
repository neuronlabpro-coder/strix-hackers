"""Esquemas de la consola de SuperAdmin.

## Por qué el saldo es `Decimal` y no `float`

`Organization.credit_balance` es `Numeric(12, 4)`. Pasarlo por `float` al cliente y de
vuelta convierte 249,9999 en 250,0 y 250,0001 en 250,0, que es exactamente la clase de
error que hace que un cliente crea que le han acreditado de más o de menos. El saldo que
ve el cliente tiene que ser el mismo que hay en la base, así que viaja como `Decimal` y se
formatea en el cliente para mostrar, nunca para calcular.

## Por qué `member_count` viene del servidor

Contar miembros por fila de la tabla de organizaciones es una consulta por tenant, que con
mil workspaces son mil consultas para pintar una tabla. El servidor lo resuelve con un
`GROUP BY` y devuelve el número ya contado. El coste de un campo que evita N+1 es de unos
30 bytes por fila.

## Aviso de contrato: los importes viajan como cadena

`credit_balance` y `credits_granted` son `Decimal` y Pydantic los serializa como **cadena**:
`"42.5"`, no `42.5`. Hasta la consola de SuperAdmin, `credit_balance` se serializaba como
número en coma flotante.

El cliente **debe** leerlos como cadena y no como `number`. Un número JSON es un binario en
coma flotante, y `0.1` no es exactamente `0.1`: 42,10 créditos viajarían como
`42.099999999999994` y el saldo que ve el operador no cuadraría con el del ledger. La cadena
no tiene ese problema y es la convención de JSON para dinero.

Cambiarlo rompe a cualquier consumidor que comparara contra un número, y aquí solo había un
consumidor: el panel, que se escribe contra este contrato.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.apps.organizations.models import PlanTierEnum


class DependencyStatusEnum(StrEnum):
    """Estado agregado de una dependencia de infraestructura."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"


class DependencyHealth(BaseModel):
    """Estado de una dependencia concreta, con su latencia observada."""

    status: Literal["online", "offline"]
    latency_ms: int = Field(ge=0)


class InfrastructureHealthResponse(BaseModel):
    """Sondeo de PostgreSQL y Redis para la consola de SuperAdmin."""

    status: DependencyStatusEnum
    database: DependencyHealth
    cache: DependencyHealth
    checked_at: datetime


class AdminOrganizationItem(BaseModel):
    """Organización registrada con su plan, su saldo y su estado de baja.

    `deleted_at` va explícito para que el panel pueda distinguir tres estados que a simple
    vista se confunden: un tenant que nunca se dio de baja, uno que se dio de baja y uno
    que se deactivated a mano. El último no es lo mismo que el segundo y no tiene fecha.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    plan_tier: PlanTierEnum
    credit_balance: Decimal
    is_active: bool
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    member_count: int = Field(default=0, ge=0)

    @property
    def lifecycle(self) -> str:
        """`active`, `deleted` o `deactivated`.

        Se calcula y no se guarda: depende de dos columnas y un booleano que se pueden
        desincronizar. El panel lo usa para pintar y para decidir si ofrece "reactivar" o
        "dar de baja", que son acciones distintas.
        """

        if self.deleted_at is not None:
            return "deleted"
        return "active" if self.is_active else "deactivated"


class AdminOrganizationPage(BaseModel):
    """Página de organizaciones registradas."""

    items: list[AdminOrganizationItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class AdminOrganizationPlanUpdate(BaseModel):
    """Cambio de plan de un tenant.

    Se **rechaza** bajar de plan con un saldo que el plan destino no cubre. Sin esa
    comprobación, un downgrade deja al cliente con un saldo que su plan no permite gastar,
    y el error no aparece hasta el escaneo que ya no se puede lanzar.
    """

    model_config = ConfigDict(extra="forbid")

    plan_tier: PlanTierEnum


class AdminCreditGrant(BaseModel):
    """Inyección administrativa de créditos.

    ## Por qué el motivo no se elige

    El motivo del asiento lo fija el servidor en `ADMIN_ADJUSTMENT`. Un endpoint que
    aceptara el motivo del cliente dejaría escribir `STRIPE_PURCHASE` en el ledger, que es
    la clase de asiento de la que se deduce que hubo un cobro. El texto libre va en un
    campo aparte, y es una nota interna: no participa en ningún cálculo.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=4)
    note: str = Field(default="", max_length=255)


class AdminCreditGrantResult(BaseModel):
    """Resultado de una inyección, con el saldo resultante.

    Se devuelve el saldo y no solo el asiento porque la pregunta del operador tras inyectar
    es "¿ya tiene los créditos?", y obligarle a recargar la lista para averiguarlo convierte
    una comprobación de un segundo en una de cinco.
    """

    organization_id: UUID
    granted: Decimal
    balance_after: Decimal
    ledger_entry_id: UUID


class AdminUserItem(BaseModel):
    """Usuario registrado, con los workspaces a los que pertenece."""

    id: UUID
    email: str
    full_name: str
    is_superuser: bool
    is_active: bool
    email_verified: bool
    created_at: datetime

    #: Nombres de los workspaces. Un usuario puede pertenecer a varios y el panel los
    #: muestra en una celda en vez de una columna con un `JOIN` que repetiría el email.
    organizations: list[str] = Field(default_factory=list)
    organization_count: int = Field(default=0, ge=0)


class AdminUserPage(BaseModel):
    """Página de usuarios registrados."""

    items: list[AdminUserItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class AdminSaleItem(BaseModel):
    """Un evento de Stripe ya procesado, con los créditos que acreditó.

    ## Por qué no hay importe en dólares

    `stripe_events` no guarda cuánto se cobró: guarda qué evento se procesó, a qué
    organización y cuántos créditos acreditó. El importe **no está en la tabla** y
    sacarlo obligaría a preguntar a Stripe por cada fila de la vista.

    El campo se declara igualmente y llega a `None` siempre, en vez de omitirse del
    esquema, para que el cliente pueda distinguir "este dato no existe" de "el backend se
    olvidó de mandarlo". Un `0` en su lugar se vería como una venta de $0.

    `credits_granted` viene de la propia columna del evento y no de un `JOIN` con el
    ledger: esa desnormalización ya existía, y enlazarla por `session_id` añadiría una
    consulta y la posibilidad de que las dos copias diverjan.
    """

    id: UUID
    event_id: str
    event_type: str
    session_id: str | None
    organization_id: UUID | None
    organization_name: str | None
    credits_granted: Decimal | None
    created_at: datetime

    @property
    def is_credit_purchase(self) -> bool:
        """`True` si el evento acreditó créditos.

        La vista separa las compras de los demás eventos de Stripe —una suscripción, un
        aviso, una sesión caducada— porque en una lista mezclada el operador tiene que
        leer el `event_type` de cada fila para saber si hubo dinero.
        """

        return self.credits_granted is not None


class AdminSalePage(BaseModel):
    """Historial de transacciones de Stripe."""

    items: list[AdminSaleItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    #: Suma de los créditos de la página, no de todo el histórico. Paginada, porque
    #: sumar sobre la tabla entera para pintar una página es un `SUM` que crece sin que
    #: el usuario pueda nunca ver la diferencia.
    total_credits: Decimal


class AdminMetric(BaseModel):
    """Una métrica del resumen global, con su etiqueta ya resuelta.

    `format` la decide el servidor para que el panel no tenga que saber que el MRR va en
    centavos y los créditos en decimales. Un panel que formatea por su cuenta acaba
    enseñando un MRR en dólares con dos decimales que en realidad son centavos.
    """

    key: str
    value: Decimal
    format: Literal["currency", "credits", "count"]
    hint_key: str


class AdminOverviewResponse(BaseModel):
    """Resumen global de la plataforma."""

    metrics: list[AdminMetric]
    infrastructure: InfrastructureHealthResponse
    generated_at: datetime


class AdminAuditItem(BaseModel):
    """Una entrada del rastro forense, con su actor y su entidad."""

    id: UUID
    organization_id: UUID | None
    organization_name: str | None
    actor_user_id: UUID | None
    actor_email: str | None
    action: str
    entity_type: str
    entity_id: UUID
    from_state: str | None
    to_state: str | None
    created_at: datetime


class AdminAuditPage(BaseModel):
    """Visor global de auditoría."""

    items: list[AdminAuditItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
