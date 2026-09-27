"""fase6_remediation_proposed

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-09-27

Añade el estado `REMEDIATION_PROPOSED` y la columna `remediation_pr_url`.

## Por qué el enum se amplía en vez de reemplazar un estado

Porque "hay un PR abierto que lo arregla" y "está arreglado" son afirmaciones distintas que se
contradicen. El PR puede cerrarse sin fusionarse, o fusionarse y que el arreglo no solucione.
Meter el hallazgo en `FIXED` al abrir el PR haría que el panel afirmara que está resuelto
cuando lo único que ha ocurrido es que alguien ha propuesto una solución.

`ALTER TYPE ... ADD VALUE` **no** puede ir dentro de la misma transacción que su uso, porque
PostgreSQL no puede usar un valor de enum recién añadido hasta que la transacción que lo añadió
termine. La migración no lo usa en ninguna sentencia posterior, y el modelo sí empieza a
devolverlo, así que el orden es seguro: primero la migración, luego el proceso que la aplica.

## Por qué `remediation_pr_url` no lleva indice

Porque se lee siempre por `id` de la vulnerabilidad, que ya tiene su indice primario. Un indice
sobre esta columna solo serviría para una consulta del estilo "todos los hallazgos con PR
abierta", que no existe en el producto: el filtro que se usa es por organización y por estado,
y los dos tienen sus indices compuestos. Añadir un indice aqui sería escribir sin necesitar.

## Por qué la columna es `String(512)` y no `Text`

Una URL de PR de GitHub o GitLab ronda los 150 caracteres; 512 es el margen razonable y, a
diferencia de `Text`, **no** se puede truncar en silencio: una URL cortada es un enlace que
lleva a la página equivocada, y eso es peor que un error al guardar.

"""

from typing import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a5b6c7d8e9"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Amplía el enum de estado y añade la columna de la propuesta."""

    op.execute("ALTER TYPE issue_status_enum ADD VALUE IF NOT EXISTS 'REMEDIATION_PROPOSED'")
    op.execute(
        "ALTER TABLE vulnerabilities "
        "ADD COLUMN IF NOT EXISTS remediation_pr_url VARCHAR(512)"
    )
    # El parche generado va en su propia columna y **no** en `autofix_patch_diff`. Ese campo
    # es evidencia forense y el trigger `trg_protect_vulnerability_evidence` lo hace
    # inmutable junto con el PoC y el CVSS, que es lo correcto: lo que escribió el motor
    # durante el escaneo no puede reescribirse después.
    #
    # Un diff generado en la plataforma es un borrador, no un hecho: no ocurrió en el escaneo,
    # no se ha aplicado y puede rehacerse. Escribirlo en la columna de evidencia contaminaría
    # el registro forense, y la alternativa —dejarlo sin guardar— obligaría a reconstruirlo
    # para mostrarlo. Dos columnas porque son dos hechos distintos.
    op.execute(
        "ALTER TABLE vulnerabilities "
        "ADD COLUMN IF NOT EXISTS remediation_patch_diff TEXT"
    )


def downgrade() -> None:
    """Quita la columna y revierte el enum al conjunto anterior.

    ## Por qué el enum **no** se puede revertir

    PostgreSQL no tiene forma de quitar un valor de un enum. Lo que sí se puede es renombrar
    los tipos y recrearlos, y eso exige recrear la columna que los usa —o cambiarla por
    `TEXT` con una conversión—, lo que en una tabla con datos de clientes significa una
    reescritura completa y un `ACCESS EXCLUSIVE` que bloquea las escrituras durante el
    tiempo que dure.

    Aquí se toma la decisión explícita de **no** revertir el enum y se dice por escrito: los
    hallazgos que quedaron en `REMEDIATION_PROPOSED` se quedan con un estado que el código
    anterior no reconoce, y quien haga un `downgrade` de esto tiene que saberlo antes de
    ejecutarlo, no descubrirlo en el log de la aplicación. La alternativa —dejar el downgrade
    fallando a propósito— sería peor: un despliegue que no se puede revertir es peor que uno
    que se revierte con un aviso explícito que hay que leer.
  ## Por qué no se deja el enum sin revertir, en silencio

  Porque el `downgrade` de esta migración no deshace el `ADD VALUE`, y eso es una
  sorpresa para quien rehaga la base desde cero esperando el esquema anterior. Se
  prefiere un estado extra que el código nuevo reconoce y el anterior ignora —que es
  un `error` de dominio al leer, no corrupción de datos— a un esquema que parece el
  viejo y miente sobre lo que contiene.
    """

    # `IF EXISTS` en las dos columnas. Un `downgrade` tiene que ser idempotente en su efecto:
    # si una migración se aplicó a medias —que es cuando más se necesita revertir—, soltar una
    # columna ausente aborta la transacción entera y deja la base **más** peor de lo que
    # estaba, porque tampoco se deshace la otra. Con `IF EXISTS` cada paso va por su cuenta.
    op.execute("ALTER TABLE vulnerabilities DROP COLUMN IF EXISTS remediation_patch_diff")
    op.execute("ALTER TABLE vulnerabilities DROP COLUMN IF EXISTS remediation_pr_url")
    op.execute(
        "DO $$ BEGIN "
        "  IF EXISTS (SELECT 1 FROM vulnerabilities "
        "              WHERE status = 'REMEDIATION_PROPOSED') THEN "
        "    RAISE EXCEPTION "
        "'No se puede revertir: hay hallazgos en REMEDIATION_PROPOSED. "
        "Pasalos a OPEN antes de revertir.'; "
        "  END IF; "
        "END $$;"
    )
