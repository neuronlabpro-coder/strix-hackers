"""anade_stealth_space_bunny_alpha

Revision ID: e9b1d2f3a4c5
Revises: d4e5f6a7b8c9
Create Date: 2026-09-29 07:45:00

Añade `stealth/space-bunny-alpha` al catálogo como **primario de prioridad 1**, a petición del
Owner, para poder hacer pruebas sin coste.

## Qué es y qué se ha comprobado

Es un identificador real de OpenRouter. Se ha contrastado contra el catálogo del proveedor —460
modelos— antes de escribir nada, porque un `model_id` inventado no falla al arrancar: falla a
mitad de un pentest, cuando ya se ha gastado la cuota y el cliente está esperando. Lo que
devuelve el proveedor:

- `id`: `stealth/space-bunny-alpha`, nombre "Space Bunny Alpha".
- `pricing`: `0` de entrada y `0` de salida. Gratis de verdad, no «descuento».
- `context_length`: 1 000 000 de tokens, `max_completion_tokens` 524 288.
- `supported_parameters` incluye `tools`, `tool_choice` y `response_format`, que es lo que
  necesita Strix para conducting el escaneo: sin `tools` el modelo no puede llamar a sus
  herramientas y el pentest no avanza aunque la llamada HTTP funcione.

## Por qué `use_case = ALL` y no uno concreto

Porque lo que se pidió es que sea **el primero**, sin distinguir el tipo de análisis. Y
`model_id` es único y una fila solo admite un caso de uso, así que duplicar la fila es
imposible. `ALL` es la forma de expresar «primario de todas sin duplicar»: `resolve_model_chain`
incluye los modelos `ALL` en cada cadena, así que encabeza también `DEEP_PENTEST`,
`QUICK_SCAN` y `AUTOFIX`. Es el mismo argumento que sostiene `z-ai/glm-5.3` en `d5e6f7a8b9c0`, y
por eso la prioridad —no la especificidad del caso de uso— es lo que decide quién encabeza.

## Por qué hay que desplazar las prioridades y no basta con insertar con prioridad 1

Porque `resolve_model_chain` desempata por **alfabeto** cuando dos modelos comparten
prioridad: `.order_by(priority_order, model_id)`. Y `z-ai/glm-5.3` ordena **después** de
`stealth/space-bunny-alpha` —`s` va antes que `z`—, así que un empate dejaría a `glm-5.3`
encabezando la cadena y el modelo nuevo no sería primario de nada. Insertar con prioridad 1 y
subir el resto una posición es lo que hace que el cambio sea real y no declarativo.

## Por qué los costes y el recargo van a cero

Porque son los del proveedor, y lo que cuesta a la plataforma es lo que dice **su** catálogo:
`calcular_coste_base` multiplica tokens por `base_cost_*_m` y nunca mira lo que cobre
OpenRouter. Con los dos a cero el coste base sale `0`, el precio con recargo sale `0`, y el
cargo en créditos es `0.0000`.

Eso es justo lo que hace útil el modelo para pruebas: **no consume saldo del tenant**. Y es
también su riesgo: un recargo mayor que cero aplicado sobre un coste base cero seguiría dando
cero, de modo que el `markup_pct` no protege de nada aquí. Se pone a cero de forma explícita
para que el número no se lea después como un margen decidido.

## La advertencia que esta migración deja escrita

El Owner lo/free describe como **gratis durante unos días**. Eso significa que este catálogo es
un **arreglo temporal de pruebas**, no una decisión de producto: en cuanto el proveedor deje de
facturarlo como gratuito, quien lo use como primario seguirá emitting cargos de cero sobre
consumo real, y el margen de esa operación será el que el proveedor decida, sin que la
plataforma lo sepa. Por eso el `downgrade` desactiva en lugar de borrar, y por eso el modelo
está aislado en una migración con nombre propio: revertirla es una operación de un paso y
visible, no una edición de un seed de los que ya están aplicados.

Cuando termine la ventana gratuita hay que migrarlo de vuelta, con los precios reales que
publique el proveedor. Ese paso es una decisión comercial y no se toma aquí.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9b1d2f3a4c5"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MODEL_ID = "stealth/space-bunny-alpha"
DISPLAY_NAME = "Space Bunny Alpha"

#: Lo que publica OpenRouter para este modelo, verificado contra su catálogo el 2026-09-29.
COST_INPUT_M = "0.00000000"
COST_OUTPUT_M = "0.00000000"
#: Cero sobre un coste base cero no cobraría nada distinto, pero se escribe explícitamente para
#: que nadie lo lea después como un margen dejado ahí a propósito. Ver el docstring.
MARKUP_PCT = "0.0000"


def upgrade() -> None:
    connection = op.get_bind()

    ya_esta = connection.execute(
        sa.text("SELECT 1 FROM llm_model_configs WHERE model_id = :model_id"),
        {"model_id": MODEL_ID},
    ).scalar()

    # El desplazamiento solo ocurre si el modelo no estaba. Reaplicar la migración con el
    # modelo ya presente **no** debe volver a subir las prioridades: hacerlo movería al
    # primario dos veces y dejaría la cadena descuadrada respecto a lo que dice el catálogo.
    if not ya_esta:
        connection.execute(
            sa.text(
                """
                UPDATE llm_model_configs
                SET priority_order = priority_order + 1
                WHERE is_active
                """
            )
        )

    connection.execute(
        sa.text(
            """
            INSERT INTO llm_model_configs (
                id, model_id, display_name, base_cost_input_m, base_cost_output_m,
                markup_pct, priority_order, is_active, use_case
            ) VALUES (
                gen_random_uuid(), :model_id, :display_name, :cost_input, :cost_output,
                :markup, 1, true, 'ALL'
            )
            ON CONFLICT (model_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                base_cost_input_m = EXCLUDED.base_cost_input_m,
                base_cost_output_m = EXCLUDED.base_cost_output_m,
                markup_pct = EXCLUDED.markup_pct,
                priority_order = EXCLUDED.priority_order,
                is_active = true,
                use_case = EXCLUDED.use_case
            """
        ),
        {
            "model_id": MODEL_ID,
            "display_name": DISPLAY_NAME,
            "cost_input": COST_INPUT_M,
            "cost_output": COST_OUTPUT_M,
            "markup": MARKUP_PCT,
        },
    )


def downgrade() -> None:
    """Desactiva el modelo de pruebas y **no** restaura las prioridades.

    Dos razones para no tocar `priority_order` al revertir:

    - No se sabe qué filas subió esta migración y cuáles ya venían así. Restar una
      unidad a todos los activos dejaría la cadena en un orden que nunca existió, que es peor
      que dejar el hueco que deja la desactivación.
    - Desactivar es reversible y exacto. El modelo sale de la cadena por `is_active`, su
      histórico de consumo se queda intacto y volver a activarlo lo devuelve al sitio que
      tenía.

    Y una razón que no es de orden sino de integridad: `llm_usage_events.model_config_id` es
    `ON DELETE CASCADE`. Borrar la fila del catálogo borraría también todos los eventos de
    consumo de ese modelo, y con ellos el único registro de lo que costó cada escaneo de
    prueba. Un `DELETE` aquí destruiría auditoría financiera, así que esta migración no
    borra nada.
    """

    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE llm_model_configs SET is_active = false WHERE model_id = :model_id"),
        {"model_id": MODEL_ID},
    )
