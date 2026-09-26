"""Fase 5 · Importe en dólares de los eventos de Stripe

## Por qué esta columna

La consola de SuperAdmin muestra las ventas con su importe en dólares, y
`stripe_events` no lo guardaba: registraba **qué** evento se procesó, a qué organización y
cuántos créditos acreditó, pero no cuánto se cobró.

Las dos salidas sin la columna eran malas:

- **Inventar un cero.** En una tabla de ventas, una columna que dice «$0,00» para todas
  las filas informa de que la plataforma no ha facturado nada. Es una conclusión falsa y
  cara.
- **Preguntarle a Stripe al pintar.** Habría que pedir el importe de cada fila visible cada
  vez que se abre la vista: una llamada de red por fila, con el coste y el fallo de red
  que implica.

La columna se rellena **en el momento de la ingestión**, que es el único punto donde el
payload de Stripe existe. Después del procesamiento, obtener el importe solo es posible
volviendo a preguntar al proveedor.

## Por qué es `NULL` y no `0` por defecto

No todos los eventos de Stripe son un cobro: una suscripción, un aviso de cuenta o una
sesión caducada no traen importe. `NULL` los distingue de un cobro, y `0` se reserva para
un caso que no existe —Stripe no cobra cero— en vez de ser un relleno que se confunda con
un dato. La vista de ventas traduce `NULL` como «no aplica» y no como un número.

## Por qué no es una migración de datos

No hay historical que rellenar a posteriori: el payload de los eventos ya procesados se
descartó, y ni siquiera estaba guardado del que recuperarlo. Los eventos anteriores a esta
migración mostrarán «importe no registrado», que es exactamente lo cierto. Volver a
preguntarle a Stripe por cada evento histórico sería una operación de recuperación de datos
—posible, pero con coste y sin garantía— que se decide por negocio, no por un `ALTER TABLE`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9c0d1e2f3a4"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stripe_events",
        sa.Column("amount_cents", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stripe_events", "amount_cents")
