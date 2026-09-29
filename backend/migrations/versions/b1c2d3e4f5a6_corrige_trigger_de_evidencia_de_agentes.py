"""corrige_trigger_de_evidencia_de_agentes

Revision ID: b1c2d3e4f5a6
Revises: f0a1b2c3d4e5
Create Date: 2026-09-29 10:05:00

Saca `agent_id` de la lista de campos inmutables del trigger de `agent_jobs`.

## Por qué: un `ON DELETE SET NULL` dispara el trigger que él mismo provoca

La migración `f0a1b2c3d4e5` puso `agent_jobs.agent_id` con `ON DELETE SET NULL` para que revocar
o borrar un agente no borrara la evidencia de lo que ya escaneó. Y en la misma migración puso un
trigger que rechaza el `UPDATE` de un trabajo ya terminado, con `agent_id` entre los campos
protegidos.

Las dos decisiones son correctas por separado y **se contradicen juntas**, y la contradicción no
se ve leyendo el esquema: en PostgreSQL, la acción referencial `SET NULL` se implementa issuing
un `UPDATE` sobre la tabla que referencia. Es decir, al borrar un `scanner_agents` con trabajos
completados, PostgreSQL hace el `UPDATE agent_jobs SET agent_id = NULL` **por su cuenta**, ese
`UPDATE` dispara el trigger, el trigger ve un trabajo terminal al que le ha cambiado un campo
protegido y aborta la operación.

El resultado era que **no se podía borrar un agente que hubiera ejecutado algún escaneo**. Que es
precisamente el caso para el que existe `SET NULL`: dar de baja el portátil donde estaba
instalado. La protección de la evidencia dejó inservible la operación de revocación, y sin el
test que lo coge, nadie se habría enterado: el código se leía bien y el esquema también.

Lo detectó `test_revocar_un_agente_no_borra_los_trabajos_que_ejecuto`, que borra el agente a
propósito y se queda con la excepción del trigger. Ese test existe por otra razón —comprobar
que la evidencia sobrevive— y por casualidad cubría este fallo.

## Por qué quitar `agent_id` es lo correcto y no aflojar la protección

Porque `agent_id` **no es evidencia**. La evidencia de un escaneo es el `result`, su
`result_digest`, el `error_message` y, sobre todo, a qué organización y a qué objetivo
pertenece: eso es lo que hace que el informe sea del cliente y no de otro. Qué instancia de
agente lo ejecutó es **atribución operacional**, y se pierde de forma deliberada cuando la fila
del agente se borra.

Y quitarla no abre un agujero real, porque el campo protegido que sí importa sigue ahí:
`organization_id` continúa en la lista, así que un trabajo terminado **no se puede mover de
tenant**. Un intruso que quisiera colgar un escaneo de otro cliente no tendría que tocar
`agent_id`; tendría que tocar `organization_id`, y ahí el trigger lo para. Lo que se permite es
exactamente lo único que la base de datos hace por su cuenta al borrar el agente.

## Por qué una migración nueva y no editar `f0a1b2c3d4e5`

Porque `f0a1b2c3d4e5` ya está aplicada por Tailscale. Reescribir un fichero de migración
aplicado deja el repositorio y la base discrepando sin que `alembic check` lo detecte —esa
comprobación compara el esquema, no el cuerpo de los triggers—, y el resultado es que una
instalación nueva tendría la protección correcta y la existente no. La única dirección que la
historia de la base permite deshacer es hacia adelante.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: La misma funcion de `f0a1b2c3d4e5` sin `agent_id` en la comparacion. Va completa y no como
#: parche sobre la anterior porque una funcion de trigger se reemplaza entera, y dejar la
#: comparacion repartida entre dos migraciones haria que nadie pudiera leer el estado final sin
#: subir por las dos.
_CORREGIDA = """
CREATE OR REPLACE FUNCTION protect_agent_job_result()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION
            'Los resultados de escaneo de un agente son evidencia y no pueden truncarse.';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'Los resultados de escaneo de un agente son evidencia y no pueden eliminarse.';
    END IF;

    -- `agent_id` NO se compara, y es deliberado: la accion referencial `ON DELETE SET NULL` de
    -- `scanner_agents` lo pone a null issuing un UPDATE en esta misma tabla, asi que
    -- compararlo haria que borrar un agente con escaneos fuera imposible. La atribucion al
    -- tenant (`organization_id`) si se compara, y por tanto un trabajo terminado no se puede
    -- mover de cliente. Ver el docstring de la migracion.
    IF OLD.status IN ('COMPLETED', 'FAILED') THEN
        IF OLD.id IS DISTINCT FROM NEW.id
           OR OLD.organization_id IS DISTINCT FROM NEW.organization_id
           OR OLD.kind IS DISTINCT FROM NEW.kind
           OR OLD.target IS DISTINCT FROM NEW.target
           OR OLD.result IS DISTINCT FROM NEW.result
           OR OLD.result_digest IS DISTINCT FROM NEW.result_digest
           OR OLD.error_message IS DISTINCT FROM NEW.error_message
           OR OLD.completed_at IS DISTINCT FROM NEW.completed_at
           OR OLD.attempt_count IS DISTINCT FROM NEW.attempt_count THEN
            RAISE EXCEPTION
                'El resultado de un trabajo de agente ya terminado es inmutable: un escaneo '
                'registra lo que habia cuando se hizo, y reescribirlo despues destruye la '
                'evidencia que el cliente pago por obtener.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_CORREGIDA)
    # No hace falta recrear el trigger: una funcion de trigger se resuelve en tiempo de
    # ejecucion, asi que sustituir su cuerpo cambia el comportamiento de los triggers que ya
    # apuntan a ella. Se dejan como estaban a proposito, para que un despliegue con los dos
    # triggers ausentes siga siendo visible como tal en lugar de fingir que se repararon aqui.
    #
    # Y el cambio es **solo** `agent_id`. `attempt_count` sigue protegido, aunque tampoco es
    # evidencia: quitarlo de mas no arregla nada aqui, y un arreglo que cambia mas campos de los
    # que el fallo necesita es un arreglo que se lleva por delante preguntas que no estaban
    # sobre la mesa.


def downgrade() -> None:
    """No se deshace.

    Volver a comparar `agent_id` reintroduce el fallo que esta migracion corrige, y un
    `downgrade` que devuelve el sistema a un estado defectuoso no es un `downgrade`: es dejar
    una trampa armada. Si hay que volver atras, se corrige el modelo y se aplica hacia
    adelante.
    """

    raise NotImplementedError(
        "Volver a proteger `agent_id` haria imposible borrar un agente que haya ejecutado "
        "escaneos, por la accion referencial SET NULL. Esta migracion no se deshace."
    )
