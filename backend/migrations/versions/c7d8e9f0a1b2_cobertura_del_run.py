"""La cobertura del escaneo, en `pentest_runs.coverage`.

## Qué añade

Una columna `JSONB` nullable en `pentest_runs` que guarda lo que el motor dice que revisó y
**lo que no pudo revisar**, tal como viene en su `coverage.json`.

## Por qué hace falta, y por qué no se puede resolver en el panel

Con cero hallazgos en `vulnerabilities`, el panel tiene dos cosas distintas que decir y ninguna
forma de distinguirlas:

- «el escaneo funcionó y no encontró nada explotable» — el motor revisó 18 superficies y
  cerró 17 con `no_issue_found` o `ruled_out`;
- «el motor no pudo revisar esto» — dejó un `gap` con `needs_follow_up`, que es información
  honesta que **debe** llegar a la interfaz y no tragarse.

Las dos se ven idénticas desde `vulnerabilities`. Con `coverage` el panel puede decir la
primera como lo que es —un resultado— y la segunda como lo que es —un límite del escaneo—,
que es justo la separación que hace `blocked`/`no bloqueado` en la tarjeta de disponibilidad
del despliegue.

## Por qué `JSONB` y no columnas

El motor publica un documento jerárquico (`summary`, `completeness`, `gaps[]`, `agents[]`) y
sus claves se mueven entre versiones: `surfaces_reviewed` y `findings_filed` cambiaron de forma
entre 1.6 y 1.7. Aplanarlo en columnas obligaría a migrar el histórico cada vez que el motor
añada un campo, y el primer campo que se añadiera se perdería en silencio.

`JSONB` sí requiere migrar si cambia el **tipo**, no si añade claves, y el documento se guarda
tal cual: lo que el motor escribió, sin traducción intermedia.

## Por qué es `NULL` y no `{}` en los runs viejos

Porque un `{}` en un run anterior a esta migración significaría «el motor no dijo nada», que
es falso: lo que pasó es que **no había dónde decirlo**. El panel distingue `NULL` (sin dato
de cobertura) de un documento con `surfaces_reviewed: 0`, que sí afirma que no se revisó nada,
y esa distinción es la que evita que un run antiguo se pinte como un escaneo limpio.

## Reversión

`DROP COLUMN` y nada más: no se toca ninguna fila de `vulnerabilities` ni el ledger, así que la
reversión pierde el dato de cobertura de los runs ya ingeridos y nada más. Es un
`reversible=True` normal, y quien lo ejecute tiene que saber que está borrando el historico de
cobertura, no una referencia.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "d9e0f1a2b3c4"
branch_labels: str | None = None
depends_on: str | None = None

COLUMNA = "coverage"
TABLA = "pentest_runs"


def upgrade() -> None:
    op.add_column(
        TABLA,
        sa.Column(COLUMNA, postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(TABLA, COLUMNA)
