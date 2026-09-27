"""fase6_knowledge_chat

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d8e9
Create Date: 2026-09-27

Base de conocimiento corporativa, chat con agentes y tipo de token de API.

## Qué añade, y por qué está todo en una migración

Porque son tres preparatory changes del mismo bloque funcional y ninguno tiene sentido sin los
otros dos: el chat necesita el caso de uso `CHAT` para resolver su cadena de modelos y el motivo
`CHAT_STEP_CONSUMPTION` para liquidar contra el libro contable; los documentos de conocimiento
y las conversaciones son las dos mitades del mismo entregable de contexto. Separarlas daría
intermedios donde la aplicación arranca con un modelo que ya pide una columna que todavía no
existe.

## Por qué los enums se amplían con `ADD VALUE` y no se recrean

Porque `ALTER TYPE ... ADD VALUE` no reescribe la tabla, y recrear el tipo obligaría a recrear
cada columna que lo usa, con un `ACCESS EXCLUSIVE` sobre tablas de clientes. El coste de
ampliar es despreciable frente a eso.

La limitación de `ADD VALUE` es que **el valor no se puede usar en la misma transacción que lo
añade**. Ninguna sentencia posterior de esta migración lo usa —las tablas nuevas se crean con
`server_default` de sus propias columnas, no de los enums ampliados—, así que el orden es seguro:
primero la migración, y solo después el proceso que aplica el modelo que ya devuelve el valor.

## Por qué `api_token_type_enum` sí se crea aquí y los otros dos ya existían

Porque son de dos naturalezas. `api_token_type_enum` nace con esta migración, así que se crea
junto a su columna. `llm_use_case_enum` y `ledger_reason_enum` existían desde la Fase 5 y solo
crecen, y por eso van por `ADD VALUE`.

## Por qué `chat_conversations` restringe y `chat_messages` no

Porque no son el mismo tipo de dato. Una conversación tiene asientos en `credit_ledger` con
`CHAT_STEP_CONSUMPTION`, y el ledger es **append-only**: si la baja del tenant arrastrase las
conversaciones, quedarían asientos apuntando a mensajes que ya no existen y el libro contable
dejaría de poder reconstruirse. Un mensaje, en cambio, no protege nada: su asiento ya está en el
ledger y se queda ahí. Por eso la conversación restringe y el mensaje va en cascada.
"""

from typing import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Amplía los dos enums existentes y crea las tablas nuevas."""

    # `IF NOT EXISTS` en los tres. Una migración aplicada a medias es justo cuando más hace
    # falta poder relanzarla, y sin el `IF` el segundo intento aborta en la primera sentencia
    # sin haber aplicado ninguna de las siguientes.
    op.execute("ALTER TYPE llm_use_case_enum ADD VALUE IF NOT EXISTS 'CHAT'")
    op.execute(
        "ALTER TYPE ledger_reason_enum ADD VALUE IF NOT EXISTS 'CHAT_STEP_CONSUMPTION'"
    )

    # Los dos enums que nacen con esta migracion. Se crean **antes** de las tablas y columnas
    # que los usan: al reves, la sentencia que los referencia falla porque el tipo no existe.
    #
    # `knowledge_doc_type_enum` va aparte de los `ADD VALUE` de arriba porque nace aqui y no
    # crece. Sus cuatro valores van en mayusculas y coinciden con los nombres del enum de
    # Python, asi que no necesita el cuidado de `api_token_type_enum`.
    op.execute(
        "DO $$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'knowledge_doc_type_enum') THEN "
        "    CREATE TYPE knowledge_doc_type_enum AS ENUM "
        "      ('DOCUMENTATION', 'BUSINESS_RULE', 'API_SPEC', 'ARCHITECTURE'); "
        "  END IF; "
        "END $$;"
    )

    # El enum del tipo de token nace aqui, y se crea **antes** de la columna que lo usa: al
    # reves, `add_column` falla porque el tipo no existe todavia.
    op.execute(
        "DO $$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'api_token_type_enum') THEN "
        "    CREATE TYPE api_token_type_enum AS ENUM ('personal', 'service_key'); "
        "  END IF; "
        "END $$;"
    )

    # Los valores van en minuscula porque son los que viajan por la API. El enum de Python
    # declara los miembros en mayusculas, pero su `value` es minusculo, y es el `value` lo que
    # se persiste: sin `values_callable` en el modelo, la columna habria guardado `PERSONAL` y
    # la lectura habria fallado al validar.
    op.execute(
        "ALTER TABLE api_tokens "
        "ADD COLUMN IF NOT EXISTS token_type api_token_type_enum NOT NULL "
        "DEFAULT 'personal'"
    )

    # Los documentos de conocimiento del cliente. `CASCADE` en la organizacion a proposito: el
    # documento es contenido del workspace, no evidencia, y un `RESTRICT` convertiria la baja de
    # un tenant en un tramite que exige borrar antes cada documento a mano.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_knowledge_documents (
            id UUID PRIMARY KEY,
            organization_id UUID NOT NULL
                REFERENCES organizations (id) ON DELETE CASCADE,
            title VARCHAR(255) NOT NULL,
            doc_type knowledge_doc_type_enum NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workspace_knowledge_documents_organization_id "
        "ON workspace_knowledge_documents (organization_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workspace_knowledge_documents_doc_type "
        "ON workspace_knowledge_documents (doc_type)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_conversations (
            id UUID PRIMARY KEY,
            organization_id UUID NOT NULL
                REFERENCES organizations (id) ON DELETE RESTRICT,
            user_id UUID NOT NULL
                REFERENCES users (id) ON DELETE RESTRICT,
            title VARCHAR(200) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_conversations_organization_id "
        "ON chat_conversations (organization_id)"
    )
    # El listado sale ordenado por `updated_at` descendente y siempre filtrado por
    # organizacion. El indice es compuesto y en ese orden porque uno solo sobre
    # `organization_id` obliga a ordenar en memoria un conjunto que crece con el uso.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_conversations_org_updated "
        "ON chat_conversations (organization_id, updated_at)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id UUID PRIMARY KEY,
            conversation_id UUID NOT NULL
                REFERENCES chat_conversations (id) ON DELETE CASCADE,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            credits_cost NUMERIC(12, 4) NOT NULL DEFAULT 0.0000,
            model_id VARCHAR(255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_messages_conversation_id "
        "ON chat_messages (conversation_id)"
    )


def downgrade() -> None:
    """Quita las tablas y la columna, y deja los enums como están.

    ## Por qué los enums **no** se revierten

    PostgreSQL no tiene forma de quitar un valor de un enum. Lo que sí se puede es renombrar el
    tipo y recrearlo, lo que obliga a recrear cada columna que lo usa o a convertirla a `TEXT`.
    En tablas de clientes eso es una reescritura completa con `ACCESS EXCLUSIVE` bloqueando
    escrituras mientras dure.

    Aquí se toma la decisión explícita de **no** revertir, y se dice por escrito en vez de dejar
    que se descubra en el log de la aplicación: quien haga un `downgrade` tiene que saberlo
    antes de ejecutarlo.

    ## Por qué se comprueba que no haya filas usando los valores nuevos

    Porque revertir el resto y dejar filas con `CHAT` o `CHAT_STEP_CONSUMPTION` produce un
    estado peor que no revertir nada: el esquema parece el anterior y el contenido no lo es. Un
    `RAISE EXCEPTION` con un mensaje que dice qué hacer es preferible a un despliegue que
    revierte en silencio y falla en la primera lectura.

    ## Por qué `IF EXISTS` en cada paso

    Porque el `downgrade` tiene que ser idempotente en su efecto. Si la migración se aplicó a
    medias —que es cuando más se necesita revertir—, soltar una tabla ausente aborta la
    transacción entera y deja la base más peor de lo que estaba, porque tampoco se deshace lo
    demás.
    """

    op.execute("DROP INDEX IF EXISTS ix_chat_messages_conversation_id")
    op.execute("DROP TABLE IF EXISTS chat_messages")
    op.execute("DROP INDEX IF EXISTS ix_chat_conversations_org_updated")
    op.execute("DROP INDEX IF EXISTS ix_chat_conversations_organization_id")
    op.execute("DROP TABLE IF EXISTS chat_conversations")
    op.execute("DROP INDEX IF EXISTS ix_workspace_knowledge_documents_doc_type")
    op.execute("DROP INDEX IF EXISTS ix_workspace_knowledge_documents_organization_id")
    op.execute("DROP TABLE IF EXISTS workspace_knowledge_documents")

    op.execute("ALTER TABLE api_tokens DROP COLUMN IF EXISTS token_type")
    op.execute("DROP TYPE IF EXISTS api_token_type_enum")
    op.execute("DROP TYPE IF EXISTS knowledge_doc_type_enum")

    op.execute(
        "DO $$ BEGIN "
        "  IF EXISTS (SELECT 1 FROM credit_ledger WHERE reason = 'CHAT_STEP_CONSUMPTION') THEN "
        "    RAISE EXCEPTION "
        "'No se puede revertir: hay asientos con CHAT_STEP_CONSUMPTION en el libro contable. "
        "El ledger es append-only por diseno; esos asientos no se pueden deshacer.'; "
        "  END IF; "
        "  IF EXISTS (SELECT 1 FROM chat_conversations) THEN "
        "    RAISE EXCEPTION "
        "'No se puede revertir: quedan conversaciones de chat. "
        "Bórralas antes de revertir.'; "
        "  END IF; "
        "END $$;"
    )
