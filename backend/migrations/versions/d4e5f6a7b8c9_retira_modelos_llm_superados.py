from datetime import datetime
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _momento(texto: str) -> datetime:
    """Convierte la marca de tiempo del catalogo retirado en un `datetime` real.

    No se pasa la cadena tal cual al parametro porque `asyncpg` no la acepta como
    `timestamptz`: rechaza `2026-09-26 06:42:00.125031+00` con `invalid input for query argument`
    y el mensaje no dice que parametro es ni por que. Un `datetime` ya tipado no falla, y de paso
    la conversion ocurre al importar la migracion, que es cuando se lee, en vez de al ejecutarla,
    que es cuando ya hay una transaccion abierta.
    """
    return datetime.fromisoformat(texto)

# (id, model_id, display_name, coste_in, coste_out, markup, prioridad, caso de uso,
#  created_at, updated_at)
#
# Los identificadores y las marcas de tiempo van **fijos** y no se vuelven a generar al
# deshacer. El `id` importa: es la clave foranea de `llm_usage_events`, y reutilizar el
# mismo UUID es lo que permite que un `downgrade` deje la base como estaba de verdad. Un
# `gen_random_uuid()` en el `downgrade` daria una fila con el mismo `model_id` y otro
# `id`, que es una fila distinta para todo lo que mire la FK.
CATALOGO_RETIRADO = [
    (
        "ff2fb44a-0cc1-4b4c-b6b8-97121f0304c7",
        "anthropic/claude-3.7-sonnet",
        "Claude 3.7 Sonnet",
        "3.00000000",
        "15.00000000",
        "150.0000",
        1,
        "DEEP_PENTEST",
        "2026-09-26T06:42:00.125031+00:00",
        "2026-09-26T06:42:00.125031+00:00",
    ),
    (
        "405b5ece-214b-4dae-b5d9-f780a07dcef3",
        "deepseek/deepseek-r1",
        "DeepSeek R1",
        "0.55000000",
        "2.19000000",
        "200.0000",
        2,
        "DEEP_PENTEST",
        "2026-09-26T06:42:00.125031+00:00",
        "2026-09-26T06:42:00.125031+00:00",
    ),
    (
        "10dfbce4-fd42-4dcd-9b61-2630232b4eea",
        "openai/o3-mini",
        "o3-mini",
        "1.10000000",
        "4.40000000",
        "150.0000",
        3,
        "DEEP_PENTEST",
        "2026-09-26T06:42:00.125031+00:00",
        "2026-09-26T06:42:00.125031+00:00",
    ),
    (
        "73ce1793-013c-4f96-a9cf-6a475d2471c1",
        "openai/gpt-4o",
        "GPT-4o",
        "2.50000000",
        "10.00000000",
        "150.0000",
        4,
        "ALL",
        "2026-09-25T20:45:26.439041+00:00",
        "2026-09-25T20:45:26.439041+00:00",
    ),
    (
        "ec494482-3c19-43f1-ae46-85398954c183",
        "deepseek/deepseek-chat",
        "DeepSeek Chat",
        "0.14000000",
        "0.28000000",
        "300.0000",
        5,
        "QUICK_SCAN",
        "2026-09-25T20:45:26.439041+00:00",
        "2026-09-25T20:45:26.439041+00:00",
    ),
]

MODELOS = [fila[1] for fila in CATALOGO_RETIRADO]


def upgrade() -> None:
    conexion = op.get_bind()

    # --- El guard: ningun modelo puede tener historial de consumo ------------------
    #
    # Se consulta antes de borrar, no despues, porque despues el `CASCADE` ya habria
    # borrado los eventos y no habria forma de saber que habia.
    con_uso = conexion.execute(
        text(
            """
            SELECT m.model_id, count(e.id) AS eventos,
                   coalesce(sum(e.base_cost_usd), 0) AS coste_usd
              FROM llm_model_configs m
              JOIN llm_usage_events e ON e.model_config_id = m.id
             WHERE m.model_id = ANY(:modelos)
             GROUP BY m.model_id
             ORDER BY m.model_id
            """
        ),
        {"modelos": MODELOS},
    ).fetchall()

    if con_uso:
        detalle = ", ".join(
            f"{fila.model_id} ({fila.eventos} eventos, {fila.coste_usd} USD)"
            for fila in con_uso
        )
        raise RuntimeError(
            "no se retiran modelos que tienen historial de consumo: la FK de "
            "llm_usage_events.model_config_id es ON DELETE CASCADE y borrarlos "
            f"borraria su registro de coste. Afectan: {detalle}. Si lo que se quiere "
            "es que el motor deje de usarlos, desactivalos con "
            "PATCH /admin/llm-models/{id}, que no borra nada."
        )

    conexion.execute(
        text("DELETE FROM llm_model_configs WHERE model_id = ANY(:modelos)"),
        {"modelos": MODELOS},
    )


def downgrade() -> None:
    conexion = op.get_bind()

    # El `SELECT` nombra las columnas una a una en vez de usar `SELECT *`, por dos motivos que ya
    # han costado una vez:
    #
    # 1. `SELECT * FROM unnest(...)` devuelve tantas columnas como arrays se le pasen, y la tabla
    #    tiene **once**. El `unnest` no lleva `is_active` porque los cinco nacieron inactivos y no
    #    hay nada que restaurar de ese campo; con `SELECT *` el `INSERT` se quejaba de tener mas
    #    columnas destino que expresiones, y el fallo llegaba desde `unnest` sin decir cual.
    # 2. Nombrar las columnas del `unnest` con un alias hace que el orden de los arrays este
    #    escrito en la consulta. Con `SELECT *` el orden solo lo define la posicion, y mover un
    #    array por accidente cambia de significado la fila sin que nada avise.
    #
    # `ON CONFLICT DO NOTHING` a proposito: un `downgrade` tiene que ser seguro de ejecutar
    # aunque el `upgrade` se hubiera aplicado a medias. Reinsertar sobre un `model_id` que ya
    # existe lanzaria una violacion de unicidad que haria fallar el `downgrade` justo cuando se
    # esta intentando arreglar algo.
    conexion.execute(
        text(
            """
            INSERT INTO llm_model_configs (
                id, model_id, display_name,
                base_cost_input_m, base_cost_output_m, markup_pct,
                priority_order, is_active, use_case, created_at, updated_at
            )
            SELECT
                u.id, u.model_id, u.display_name,
                u.base_cost_input_m, u.base_cost_output_m, u.markup_pct,
                u.priority_order, false, u.use_case, u.created_at, u.updated_at
            FROM unnest(
                CAST(:ids          AS uuid[]),
                CAST(:modelos      AS text[]),
                CAST(:nombres      AS text[]),
                CAST(:coste_in     AS numeric[]),
                CAST(:coste_out    AS numeric[]),
                CAST(:markups      AS numeric[]),
                CAST(:prioridades  AS integer[]),
                -- `use_case` no es un `text` sino un tipo enum de PostgreSQL
                -- (`llm_use_case_enum`), asi que el array va casteado a ese tipo. Con
                -- `text[]` PostgreSQL rechaza el INSERT con "column use_case is of type
                -- llm_use_case_enum but expression is of type text": un `text[]` no se
                -- convierte solo a un enum, hay que pedir la conversion de forma explicita.
                CAST(:casos        AS llm_use_case_enum[]),
                CAST(:creados      AS timestamptz[]),
                CAST(:actualizados AS timestamptz[])
            ) AS u(
                id, model_id, display_name,
                base_cost_input_m, base_cost_output_m, markup_pct,
                priority_order, use_case, created_at, updated_at
            )
            ON CONFLICT (model_id) DO NOTHING
            """
        ),
        {
            "ids": [f[0] for f in CATALOGO_RETIRADO],
            "modelos": MODELOS,
            "nombres": [f[2] for f in CATALOGO_RETIRADO],
            "coste_in": [f[3] for f in CATALOGO_RETIRADO],
            "coste_out": [f[4] for f in CATALOGO_RETIRADO],
            "markups": [f[5] for f in CATALOGO_RETIRADO],
            "prioridades": [f[6] for f in CATALOGO_RETIRADO],
            "casos": [f[7] for f in CATALOGO_RETIRADO],
            "creados": [_momento(f[8]) for f in CATALOGO_RETIRADO],
            "actualizados": [_momento(f[9]) for f in CATALOGO_RETIRADO],
        },
    )
    # `is_active` va en el `SELECT` como `false` y no en un `UPDATE` detras: los cinco nacieron
    # inactivos, y un `downgrade` no debe devolver un modelo al enrutado sin que nadie lo haya
    # pedido. Un `UPDATE` posterior seria una segunda pasada por la misma tabla para escribir un
    # valor que ya se sabe.
