"""Saca los precios de la configuración y del código y los pone en la base de datos.

## Qué hace

Crea las tres tablas de precios de plataforma y **copia** en ellas los valores que hoy viven en
`Settings` y en `billing/schemas.py`. No borra nada de donde venía: la configuración sigue
leyéndose como valor de arranque, y el catálogo comercial sigue teniendo sus constantes.

## Por qué se copian y no se borran

Porque una migración que borra el precio de su origen no es reversible, y este proyecto despliega
la aplicación y la base por separado. Con la copia:

- La base tiene el precio, que es lo que se edita desde la consola.
- La configuración sigue puesta, y si la fila de la base **no existe** —una base recién creada
  donde la migración aún no corrió, o una fila borrada por error— la aplicación cobra con la
  configuración en vez de arrancar sin precios.

Ese segundo punto es la razón de que el orden sea: primero la tabla, después el reseeding desde
la configuración, y solo más adelante (otro despliegue) la lectura desde la base. Es el patron
*expand-migrate-contract* de la skill, y por eso esta migración **solo** añade y puebla.

## Por qué la fila de precios es única y tiene `id` fijo

Porque así no existe la pregunta de «cuál de estos valores está activo». Con una tabla
clave-valor haría falta una columna `is_active` y habría una carrera entre dos operadores
editando a la vez. Con `CHECK (id = 1)` hay exactamente un sitio donde está el precio vigente, y
un `UPDATE` no tiene a quién interpretar.

## Por qué el descuento es una fracción y no un porcentaje

Porque `price_for_credits` multiplica por `1 - descuento` y por `discount / (1 - descuento)`. Un
porcentaje obligaría a dividir por cien en cada uno de esos sitios, y un solo olvidarse cambia el
precio de un cliente sin que nada falle. La fracción hace la operación y la inversión en la misma
unidad, y el `CHECK` lo deja claro.

## Por qué `platform_price_changes` nace aquí y no con la primera edición

Porque una traza de cambios que empieza el día de la primera edición tiene un hueco: los precios
que se hayan tocado antes de que existiera no quedan registrados. Nace con la tabla y se puebla
con **un asiento por cada precio copiado**, con el valor anterior y el nuevo, para que la
historia del catálogo empiece en su origen y no en su primera corrección.

## Por qué no se toca `audit_log`

Porque `audit_log.organization_id` es `NOT NULL` con clave foránea a `organizations`, y un precio
de plataforma no pertenece a ninguna organización. Colgar el asiento de la organización de quien
lo editó lo haría aparecer filtrado por ese cliente en el visor global, y hacer nullable la
columna tocaría un modelo append-only con su trigger de inmutabilidad. La traza nueva es de
alcance plataforma y guarda el importe anterior y el nuevo, que en `audit_log` no caben —sus
`from_state`/`to_state` son de 64 caracteres—.

## Por qué el disparador de inmutabilidad se crea en esta migración y no en otra

Porque R4 exige que la evidencia sea inmutable, y una tabla de histórico de precios sin disparador
es una tabla que alguien acaba actualizando para «corregir una errata». Va aquí, junto a la
tabla, para que no exista un estado intermedio en el que la tabla existe y no está protegida.
"""

from __future__ import annotations

from collections.abc import Sequence

import uuid
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3a4b5c6d7e8"
down_revision: str = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Los precios de arranque, escritos a mano.
#:
#: ## Por qué están aquí y no se leen de la configuración de la migración
#:
#: Porque una migración se ejecuta contra **cualquier** base, y leer la configuración del proceso
#: que la aplica significaría que el resultado depende de qué variables tiene el servidor que
#: corre Alembic. La fila debe salir igual en un portátil, en Dokploy y en una base restaurada de
#: hace seis meses. Los valores son los de `Settings` por defecto, y se contrastan con lo que
#: hubiera en `.env` de quien migra: si el `.env` traía `QUICK_SCAN_CREDIT_MULTIPLIER=0.3`, la
#: fila nace con `0.3` y no con el `0.5` del default, que es lo que evita que el despliegue
#: empiece a cobrar distinto de lo que cobraba.
#:
#: ## Por qué no se copia de la configuración y ya
#:
#: ## Por qué el valor viene de una constante y no de `settings`
#:
#: Porque la migración tiene que ser **reproducible**: el mismo fichero, aplicado a dos bases,
#: tiene que sembrar el mismo precio. Si leyera `settings`, aplicar el mismo fichero con dos
#: `.env` distintos sembraría precios distintos, y una migración cuyo resultado depende del
#: entorno no es una migración, es un script.
PRECIOS_DE_ARRANQUE = {
    "credits_per_usd": "1.00",
    "scan_credit_cost": "10",
    "quick_scan_credit_multiplier": "0.3",
    "low_credit_balance_threshold": "0",
}

#: El motivo que se registra en la traza de cada precio sembrado.
#:
#: ## Por qué el texto va en la traza y no solo en el nombre de la clave
#:
#: Porque «sembrado por la migración» y «subido un 20 % por el operador» son cosas que la columna
#: `clave` no puede distinguir: las dos escriben `scan_credit_cost`. Sin el motivo, dentro de seis
#: meses no hay forma de saber si un precio cambió por una decisión comercial o porque la
#: migración copió lo que ya había.
MOTIVO_CONFIG = "sembrado por la migracion desde la configuracion"
MOTIVO_CATALOGO = "sembrado por la migracion desde el catalogo del codigo"

#: Los packs que hoy viven en `billing/schemas.CREDIT_PACKS`.
PACKS_DE_ARRANQUE = ((25, "25.00"), (100, "100.00"), (250, "250.00"))

#: La escalera de descuento de hoy: `(gasto mínimo, descuento)`.
TRAMOS_DE_ARRANQUE = (
    ("10.00", "0.00"),
    ("251.00", "0.10"),
    ("1001.00", "0.15"),
    ("3001.00", "0.20"),
    ("5001.00", "0.25"),
)


def upgrade() -> None:
    # ---------------------------------------------------------------- precios escalares
    op.create_table(
        "platform_pricing",
        # Sin SERIAL: ver la nota del modelo. Es una fila unica con `id` fijo, y una secuencia
        # que se avanza sola solo sirve para fabricar `id` que el `CHECK` va a rechazar.
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("credits_per_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("scan_credit_cost", sa.Numeric(18, 8), nullable=False),
        sa.Column("quick_scan_credit_multiplier", sa.Numeric(18, 8), nullable=False),
        sa.Column("low_credit_balance_threshold", sa.Numeric(18, 8), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("id = 1", name="ck_platform_pricing_singleton"),
        sa.CheckConstraint("credits_per_usd > 0", name="ck_platform_pricing_credits_per_usd"),
        sa.CheckConstraint("scan_credit_cost > 0", name="ck_platform_pricing_scan_cost"),
        sa.CheckConstraint(
            "quick_scan_credit_multiplier > 0 AND quick_scan_credit_multiplier <= 1",
            name="ck_platform_pricing_quick_multiplier",
        ),
        sa.CheckConstraint(
            "low_credit_balance_threshold >= 0", name="ck_platform_pricing_low_threshold"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # El indice de la clave foranea de `updated_by`, por simetria con el de `actor_user_id`:
    # ambos son `ON DELETE SET NULL` y ambos buscaria un recorrido secuencial al borrar un
    # usuario. En esta tabla es una sola fila, asi que el indice no aporta mucho; se crea para
    # que el esquema y el modelo no se separen, que es lo que `alembic check` vigila.
    op.create_index(
        "ix_platform_pricing_updated_by", "platform_pricing", ["updated_by"]
    )

    # ---------------------------------------------------------------- packs
    op.create_table(
        "platform_credit_packs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("amount_usd", sa.Numeric(18, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("credits > 0", name="ck_credit_packs_credits_positive"),
        sa.CheckConstraint("amount_usd > 0", name="ck_credit_packs_amount_positive"),
        sa.CheckConstraint("display_order >= 0", name="ck_credit_packs_order"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credits", name="uq_credit_packs_credits"),
    )
    op.create_index(
        "ix_credit_packs_active_order", "platform_credit_packs", ["is_active", "display_order"]
    )

    # ---------------------------------------------------------------- escalera de descuento
    op.create_table(
        "platform_volume_tiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("spend_min_usd", sa.Numeric(18, 2), nullable=False),
        sa.Column("discount", sa.Numeric(9, 6), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("spend_min_usd > 0", name="ck_volume_tiers_spend_positive"),
        sa.CheckConstraint("discount >= 0 AND discount <= 1", name="ck_volume_tiers_discount_range"),
        sa.CheckConstraint("display_order >= 0", name="ck_volume_tiers_order"),
        sa.PrimaryKeyConstraint("id"),
        # Sin esta unicidad, dos filas con el mismo umbral satisfacen las dos el
        # `gasto >= minimo` y gana la ultima que llegue, que decide el planificador y no el
        # codigo: el mismo gasto se cobraria con dos descuentos distintos segun el plan.
        sa.UniqueConstraint("spend_min_usd", name="uq_volume_tiers_spend_min"),
    )
    # Por `spend_min_usd` y no por `display_order`: es como se ordena la escalera. Un indice
    # cuyo `ORDER BY` no coincide con su segunda columna no evita el `sort`.
    op.create_index(
        "ix_volume_tiers_active_min", "platform_volume_tiers", ["is_active", "spend_min_usd"]
    )

    # ---------------------------------------------------------------- traza append-only
    op.create_table(
        "platform_price_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clave", sa.String(length=64), nullable=False),
        sa.Column("valor_anterior", sa.Numeric(18, 8), nullable=True),
        sa.Column("valor_nuevo", sa.Numeric(18, 8), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("motivo", sa.String(length=255), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("clave <> ''", name="ck_platform_price_changes_key"),
        sa.CheckConstraint(
            "valor_anterior IS NOT NULL OR valor_nuevo IS NOT NULL",
            name="ck_platform_price_changes_algo_cambio",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # `ON DELETE SET NULL` dispara un `UPDATE` por cada fila cuyo actor se borra; sin indice
    # es un recorrido secuencial sobre una tabla que solo crece.
    op.create_index(
        "ix_platform_price_changes_actor_user_id",
        "platform_price_changes",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_platform_price_changes_clave_fecha", "platform_price_changes", ["clave", "changed_at"]
    )
    # La vista natural de la consola es "los ultimos cambios" sin filtro de clave.
    op.create_index("ix_platform_price_changes_fecha", "platform_price_changes", ["changed_at"])

    # El disparador de inmutabilidad, en la misma migración que la tabla: no existe un estado
    # intermedio en el que la traza exista y no esté protegida.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_platform_price_changes_append_only()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'platform_price_changes es append-only; no admite % ni TRUNCATE',
                TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # `FOR EACH STATEMENT` y con `TRUNCATE` en la lista, por dos motivos a la vez.
    #
    # El primero es que **un `TRUNCATE` no dispara un trigger de fila**: no hay filas que
    # disparar. Con el disparador por fila, `TRUNCATE platform_price_changes` pasaba entero,
    # sin error y sin dejar asiento, que es un hueco real de R4 — la traza que documenta qué
    # precio rigió cada cobro se podía vaciar de un plumazo y no había forma de saber que pasó.
    # Este proyecto ya ha pagado ese bug dos veces, en `agent_jobs` y en las evidencias de
    # vulnerabilidad, y por eso el motivo está escrito aquí y no en un ticket.
    #
    # El segundo es el coste: por sentencia el motor no se instancia una vez por fila, y en una
    # tabla que solo crece y nunca se poda, esa diferencia se nota.
    #
    # Y lo que **cuesta** es el `OLD.id` del mensaje, que en un trigger de sentencia es `NULL`.
    # Se pierde el identificador de la fila en el error. Es un dato de diagnóstico y no compensa
    # dejar `TRUNCATE` abierto; a cambio, el mensaje dice el motivo real, que es que la tabla no
    # admite esa operación.
    op.execute(
        """
        CREATE TRIGGER trg_protect_platform_price_changes_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON platform_price_changes
        FOR EACH STATEMENT EXECUTE FUNCTION protect_platform_price_changes_append_only();
        """
    )

    # La fila unica de precios tampoco se puede borrar: `CHECK (id = 1)` garantiza que si hay
    # fila es la unica, pero no que haya fila. Un `DELETE` —o un `TRUNCATE`, que aqui si pasa—
    # dejaria la plataforma sin precios y el respaldo de `billing/schemas.py` empezaria a
    # cobrar en silencio, que es exactamente lo que este trabajo queria eliminar.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_platform_pricing_unique_row()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'platform_pricing no se borra ni se trunca: cambia el precio con un UPDATE sobre id = 1';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_platform_pricing_single_row
        BEFORE DELETE OR TRUNCATE ON platform_pricing
        FOR EACH STATEMENT EXECUTE FUNCTION protect_platform_pricing_unique_row();
        """
    )

    # ---------------------------------------------------------------- siembra
    # La siembra lleva `noqa: S608` y el motivo está aquí, en el fichero.
    #
    # ## Por qué la excepción a S608
    #
    # ## Por qué la excepción a S608
    #
    # Porque no hay nada que interpolar desde fuera. Cada valor viene de una constante de este
    # mismo módulo —`PRECIOS_DE_ARRANQUE`, `PACKS_DE_ARRANQUE`, `TRAMOS_DE_ARRANQUE`— y
    # `CLAVES` es una tupla de literales escrita aquí. La consulta no lee una variable de
    # entorno, ni una cabecera, ni un parámetro: aplicar este fichero a dos bases produce
    # exactamente las mismas sentencias, que es justo lo que hace que una migración sea
    # reproducible.
    #
    # Y el patrón es el que ya usa la migración del hash de invitación
    # (`c2d3e4f5a6b7`, líneas 120 y 123): la excepción se pide **junto a la razón**, no en un
    # `bandit.ini` global que nadie lee.

    # Los datos van **después** del esquema y en su propia transacción implícita, para que un
    # fallo al sembrar no deje tablas a medio poblar sin que se note: la fila de precios es la
    # que la aplicación lee al arrancar.
    # Las sentencias se **arman en una variable** y se ejecutan después, en vez de ir
    # encadenando literales dentro de la llamada.
    #
    # ## Por qué una variable y no un `noqa`
    #
    # ## Por qué se arman aparte
    #
    # Porque la exención que pedía la regla S608 era un límite de la herramienta, no una
    # propiedad del código: el lint señalaba la concatenación, no la inyección. Los parámetros
    # vinculados quitan la concatenación, y con ella la exención: ya no hay nada que exceptuar.

    # Y el motivo se escribe aqui y no en un archivo de configuracion del linter, porque una
    # excepcion que vive lejos del codigo que la necesita es una excepcion que nadie vuelve a
    # leer antes de borrarla.

    vinculo = op.get_bind()
    vinculo.execute(
        sa.text(
            "INSERT INTO platform_pricing (id, credits_per_usd, scan_credit_cost, "
            "quick_scan_credit_multiplier, low_credit_balance_threshold) "
            "VALUES (1, :credits_per_usd, :scan_credit_cost, :quick_scan_credit_multiplier, "
            ":low_credit_balance_threshold)"
        ),
        {clave: Decimal(valor) for clave, valor in PRECIOS_DE_ARRANQUE.items()},
    )
    # Los identificadores de los packs y los tramos se generan **aquí**, en Python, y no con
    # `gen_random_uuid()` dentro de la sentencia.
    #
    # ## Por qué se generan en Python y no en la base
    #
    # Porque los asientos de la traza necesitan citar el identificador de la fila a la que se
    # refieren, y con `gen_random_uuid()` en la sentencia esa fila no existe todavía para
    # Python: habría que leerla después y el asiento quedaría con una clave inventada.
    #
    # ## Por qué la clave del asiento lleva el UUID y no el valor
    #
    # ## Por qué el identificador y no el valor
    #
    # Porque el valor es exactamente lo que el operador va a editar. Con la clave atada al
    # importe, subir el umbral de un tramo de $251 a $300 creaba una clave nueva y dejaba la
    # vieja apuntando a un tramo que ya no existía: el histórico diría que hubo un tramo de
    # $251 cuando ahora no lo hay, y no habría forma de enlazar el asiento con la fila actual
    # salvo por la fecha. Con el UUID, el enlace deja de mentir, y la columna `motivo` es la
    # que explica en palabras qué pasó.
    ids_de_packs = [uuid.uuid4() for _ in PACKS_DE_ARRANQUE]
    ids_de_tramos = [uuid.uuid4() for _ in TRAMOS_DE_ARRANQUE]

    vinculo.execute(
        sa.text(
            "INSERT INTO platform_credit_packs (id, credits, amount_usd, is_active, display_order) "
            "VALUES (:id, :credits, :importe, true, :orden)"
        ),
        [
            {
                "id": ids_de_packs[orden],
                "credits": creditos,
                "importe": Decimal(importe),
                "orden": orden,
            }
            for orden, (creditos, importe) in enumerate(PACKS_DE_ARRANQUE)
        ],
    )
    vinculo.execute(
        sa.text(
            "INSERT INTO platform_volume_tiers (id, spend_min_usd, discount, is_active, "
            "display_order) VALUES (:id, :gasto, :descuento, true, :orden)"
        ),
        [
            {
                "id": ids_de_tramos[orden],
                "gasto": Decimal(gasto),
                "descuento": Decimal(descuento),
                "orden": orden,
            }
            for orden, (gasto, descuento) in enumerate(TRAMOS_DE_ARRANQUE)
        ],
    )

    # Un asiento por cada precio copiado. `valor_anterior` es `NULL` porque **no había** precio
    # anterior: el anterior era la configuración, y el asiento lo dice con el `NULL` en vez de
    # mentir con un valor que nadie editó.
    #
    # ## Por qué la siembra usa `sa.text()` con parámetros y no una cadena armada
    #
    # ## Por qué se ejecuta con parámetros vinculados
    #
    # Porque es la forma en la que no hay nada que excepcionar. Con `sa.text()` y parámetros
    # vinculados, el **valor viaja al servidor como parámetro** y nunca pasa por el texto de la
    # sentencia: no hay concatenación que un lint pueda señalar ni que nadie pueda inyectar. La
    # primera version de esta siembra interpolaba los numeros dentro de un f-string y necesitaba
    # una excepcion para explicar que venian de constantes; con parametros vinculados, la
    # explicacion desaparece porque el problema no llega a existir.

    vinculo.execute(
        sa.text(
            "INSERT INTO platform_price_changes (id, clave, valor_anterior, valor_nuevo, motivo) "
            "VALUES (:id, :clave, NULL, :valor, :motivo)"
        ),
        [
            {
                "id": uuid.uuid4(),
                "clave": clave,
                "valor": Decimal(valor),
                "motivo": MOTIVO_CONFIG,
            }
            for clave, valor in PRECIOS_DE_ARRANQUE.items()
        ],
    )
    vinculo.execute(
        sa.text(
            "INSERT INTO platform_price_changes (id, clave, valor_anterior, valor_nuevo, motivo) "
            "VALUES (:id, :clave, NULL, :valor, :motivo)"
        ),
        [
            {
                "id": uuid.uuid4(),
                # El identificador de la fila, no su valor. El importe es justo lo que el
                # operador va a editar: atar la clave a él crearía una clave nueva en cada
                # cambio y dejaría la vieja apuntando a un pack que ya no existe con ese
                # precio, sin forma de volver a enlazarlos salvo por la fecha.
                "clave": f"credit_pack:{ids_de_packs[orden]}",
                "valor": Decimal(importe),
                "motivo": MOTIVO_CATALOGO,
            }
            for orden, (creditos, importe) in enumerate(PACKS_DE_ARRANQUE)
        ],
    )
    vinculo.execute(
        sa.text(
            "INSERT INTO platform_price_changes (id, clave, valor_anterior, valor_nuevo, motivo) "
            "VALUES (:id, :clave, NULL, :valor, :motivo)"
        ),
        [
            {
                "id": uuid.uuid4(),
                # El identificador y no el umbral: el umbral es el numero que el operador
                # edita. Con la clave atada a el, subir un tramo de $251 a $300 dejaba en el
                # historico la noticia de un tramo de $251 que ya no existe, y el asiento nuevo
                # no se podia enlazar con la fila salvo por la fecha. El `motivo` es lo que
                # explica en palabras que paso; el identificador es lo que engancha las filas.
                "clave": f"volume_tier:{ids_de_tramos[orden]}",
                "valor": Decimal(descuento),
                "motivo": MOTIVO_CATALOGO,
            }
            for orden, (gasto, descuento) in enumerate(TRAMOS_DE_ARRANQUE)
        ],
    )


def downgrade() -> None:
    # El orden es el inverso del de `upgrade`, y el disparador cae antes que su función: al revés
    # la caída de la tabla ya se llevaría el disparador, pero dejarlo puesto sobre una función
    # que ya no existe deja la base con una dependencia rota que `DROP TABLE` no avisa.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_protect_platform_price_changes_append_only "
        "ON platform_price_changes"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_platform_price_changes_append_only")
    op.execute("DROP TRIGGER IF EXISTS trg_protect_platform_pricing_single_row ON platform_pricing")
    op.execute("DROP FUNCTION IF EXISTS protect_platform_pricing_unique_row")
    op.drop_index("ix_platform_price_changes_fecha", table_name="platform_price_changes")
    op.drop_index(
        "ix_platform_price_changes_actor_user_id", table_name="platform_price_changes"
    )
    op.drop_index("ix_platform_price_changes_clave_fecha", table_name="platform_price_changes")
    op.drop_table("platform_price_changes")
    op.drop_index("ix_volume_tiers_active_min", table_name="platform_volume_tiers")
    op.drop_table("platform_volume_tiers")
    op.drop_index("ix_credit_packs_active_order", table_name="platform_credit_packs")
    op.drop_table("platform_credit_packs")
    op.drop_index("ix_platform_pricing_updated_by", table_name="platform_pricing")
    op.drop_table("platform_pricing")
