"""fase6_agentes_de_escaneo

Revision ID: f0a1b2c3d4e5
Revises: e9b1d2f3a4c5
Create Date: 2026-09-29 09:30:00

Crea `scanner_agents` y `agent_jobs`: los agentes de escaneo que corren dentro de la red del
cliente y los encargos que ejecutan.

## Por qué un agente y no un escaneo desde el SaaS

Porque el cerco de salida del sandbox no lo permite. `scripts/harden_runner_egress.sh` deja
pasar DNS, HTTPS y conexiones establecidas, y descarta en silencio el resto —el 22, el 80, el
8080—. Ese cerco es correcto y no se modifica: es lo que impide que un agente comprometido
saque la clave del proveedor por un puerto alto.

Pero significa que desde el SaaS no se alcanza la red privada de nadie, que es donde están los
contenedores de base de datos, los servicios internos y la mayor parte del valor que un cliente
quiere contratar. La salida no es abrir el cerco: es que el escaneo ocurra dentro de la red del
cliente, con un componente que el cliente instala y que ve lo que el cliente ve.

## Por qué el agente no recibe la clave del proveedor

Porque corre en la red del cliente, con los permisos del cliente. Es el entorno menos confiable
de todo el sistema para la plataforma, y una clave de inferencia ahí significaría que
comprometer la red de un cliente compromete la facturación de todos. El agente hace la parte
determinista —descubrir, medir puertos, inventariar la imagen— y publica hechos; el
razonamiento ocurre en la plataforma.

## Por qué `agent_id` es `SET NULL` y no `CASCADE`

Porque revocar un agente no puede borrar la evidencia de lo que ya escaneó. Con `CASCADE`, dar
de baja a un agente que falló en un escaneo borraría el registro de ese escaneo, y con él el
hueco en la postura del cliente. `scanner_agents.status = REVOKED` no borra filas precisamente
por esto.

## Por qué el resultado lleva huella y por qué hay un trigger

Porque el resultado **es la prueba**: dice qué puertos había abiertos, y dentro de seis meses lo
que vale es poder demostrar que el registro no se tocó después. Dos mecanismos:

- `result_digest`, el SHA-256 de la forma canónica del JSON, que se comprueba al leer.
- Un `trigger` que rechaza el `UPDATE` de un trabajo ya terminal. Va en la base y no en el
  código porque el código se puede evitar escribiendo SQL.

La huella no es una firma criptográfica y no pretende serlo: no protege contra un atacante que
pueda escribir en la base. Protege contra el caso real, que es la corrección posterior de un
resultado hecha por la propia plataforma con su propia API.

## Los enums se crean con `op.execute` y no con `sa.Enum`

Porque `CREATE TYPE x AS ENUM 'A', 'B'` en una sola línea es un error de sintaxis en
PostgreSQL, y `sa.Enum` no emite esa forma. Además la lista de valores se repite aquí a
propósito: una migración que importa el modelo de la aplicación se rompe el día que el modelo
cambie, y una migración tiene que describir **el** estado de la base en ese punto, no el estado
de un fichero de Python.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "e9b1d2f3a4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AGENT_STATUSES = ("ACTIVE", "REVOKED")
AGENT_JOB_KINDS = ("CONTAINER_SCAN", "NETWORK_SCAN")
AGENT_JOB_STATUSES = ("QUEUED", "CLAIMED", "RUNNING", "COMPLETED", "FAILED")


def _valores(nombres: tuple[str, ...]) -> str:
    """La lista de valores de un `CREATE TYPE AS ENUM`, con sus comillas.

    El parentesis no es decorativo: PostgreSQL rechaza `CREATE TYPE x AS ENUM 'A', 'B'` como
    error de sintaxis. Los nombres vienen de literales del modulo, no de entrada externa, asi
    que no hay escapado que hacer aqui; se documenta para que nadie lo tome por un olvido si
    algun dia esta funcion empieza a recibir datos de fuera.
    """

    return "(" + ", ".join(f"'{nombre}'" for nombre in nombres) + ")"


# El trigger de R4 para `agent_jobs`. Rechaza DELETE y TRUNCATE siempre, y UPDATE cuando el
# trabajo ya esta terminal, con una excepcion: el paso a terminal **si** se permite, porque es
# la unica forma de que un resultado exista. Lo que no se permite es cambiarlo despues.
_INMUTABLE = """
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

    IF OLD.status IN ('COMPLETED', 'FAILED') THEN
        IF OLD.id IS DISTINCT FROM NEW.id
           OR OLD.organization_id IS DISTINCT FROM NEW.organization_id
           OR OLD.agent_id IS DISTINCT FROM NEW.agent_id
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
    op.execute(f"CREATE TYPE agent_status_enum AS ENUM {_valores(AGENT_STATUSES)}")
    op.execute(f"CREATE TYPE agent_job_kind_enum AS ENUM {_valores(AGENT_JOB_KINDS)}")
    op.execute(f"CREATE TYPE agent_job_status_enum AS ENUM {_valores(AGENT_JOB_STATUSES)}")

    op.create_table(
        "scanner_agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*AGENT_STATUSES, name="agent_status_enum", create_type=False),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=255), nullable=True),
        sa.Column("agent_version", sa.String(length=32), nullable=True),
        sa.Column("platform_hint", sa.String(length=64), nullable=True),
        sa.Column(
            "enrolled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        # Unico y a la vez indice de busqueda: cada peticion del agente busca por hash.
        sa.UniqueConstraint("token_hash", name="uq_scanner_agents_token_hash"),
        sa.UniqueConstraint("organization_id", "name", name="uq_scanner_agents_org_name"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_scanner_agents_name_not_empty"),
    )
    op.create_index("ix_scanner_agents_org_enrolled", "scanner_agents", ["organization_id", "enrolled_at"])
    op.create_index("ix_scanner_agents_org_revoked", "scanner_agents", ["organization_id", "revoked_at"])
    op.create_index("ix_scanner_agents_organization_id", "scanner_agents", ["organization_id"])

    op.create_table(
        "agent_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "kind",
            postgresql.ENUM(*AGENT_JOB_KINDS, name="agent_job_kind_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("target", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*AGENT_JOB_STATUSES, name="agent_job_status_enum", create_type=False),
            nullable=False,
            server_default="QUEUED",
        ),
        sa.Column("priority_order", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        # SET NULL y no CASCADE: revocar un agente no puede borrar la evidencia de lo que ya
        # escaneo. Ver el docstring.
        sa.ForeignKeyConstraint(["agent_id"], ["scanner_agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("priority_order >= 1", name="ck_agent_jobs_priority_positive"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_agent_jobs_attempts_nonnegative"),
    )
    op.create_index("ix_agent_jobs_org_status", "agent_jobs", ["organization_id", "status"])
    op.create_index(
        "ix_agent_jobs_queue", "agent_jobs", ["status", "priority_order", "created_at"]
    )
    op.create_index("ix_agent_jobs_lease", "agent_jobs", ["status", "lease_expires_at"])
    op.create_index("ix_agent_jobs_agent_status", "agent_jobs", ["agent_id", "status"])
    op.create_index("ix_agent_jobs_organization_id", "agent_jobs", ["organization_id"])
    op.create_index("ix_agent_jobs_status", "agent_jobs", ["status"])

    op.execute(_INMUTABLE)
    op.execute("DROP TRIGGER IF EXISTS trg_protect_agent_job_result ON agent_jobs;")
    # BEFORE, y no AFTER: un AFTER deja que la escritura se aplique antes de que el trigger se
    # queje, y en una sentencia el error llega tarde para deshacerla sola.
    op.execute(
        """
        CREATE TRIGGER trg_protect_agent_job_result
        BEFORE UPDATE OR DELETE ON agent_jobs
        FOR EACH ROW EXECUTE FUNCTION protect_agent_job_result();
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_protect_agent_job_result_truncate ON agent_jobs;")
    # `FOR EACH STATEMENT` porque un TRUNCATE no tiene filas, y un trigger de fila no se
    # dispara nunca para el. Sin este segundo trigger, `TRUNCATE agent_jobs` pasaria entero.
    op.execute(
        """
        CREATE TRIGGER trg_protect_agent_job_result_truncate
        BEFORE TRUNCATE ON agent_jobs
        FOR EACH STATEMENT EXECUTE FUNCTION protect_agent_job_result();
        """
    )


def downgrade() -> None:
    """Rechaza el rollback para no debilitar la protección de evidencia.

    A diferencia de `c3f8a1d9e2b4`, aquí **no** se puede deshacer limpiamente: los resultados
    de escaneo son evidencia de lo que el cliente pagó por obtener, y un `DROP TABLE` los
    llevaria por delante. Quien quiera deshacerlo tiene que hacerlo a mano, en la base, y con
    la decision de perder los registros escrita.
    """

    raise NotImplementedError(
        "Los resultados de los agentes de escaneo son evidencia y esta migracion no se "
        "deshace. Si hace falta rehacerla, se deja la tabla y se corrige hacia adelante."
    )
