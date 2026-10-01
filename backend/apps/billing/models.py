"""Ledger de créditos append-only e idempotencia de eventos de Stripe."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.database import Base

# El dinero no se guarda en coma flotante. Un ledger con derivas de centavo
# acaba sin cuadrar y sin ninguna forma de explicar la diferencia.
_CREDIT_PRECISION = Numeric(18, 4)


class LedgerReasonEnum(StrEnum):
    """Causas de cada movimiento del ledger. Solo se añaden, nunca se reescriben."""

    SCAN_CONSUMPTION = "SCAN_CONSUMPTION"
    STRIPE_PURCHASE = "STRIPE_PURCHASE"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"
    SIGNUP_BONUS = "SIGNUP_BONUS"
    #: Consumo de un paso del chat con agentes.
    #:
    #: Va en su propio motivo y no como un `SCAN_CONSUMPTION` mas porque el libro contable se
    #: lee por motivo: sin esta distincion, "los escaneos me cuestan 200 creditos al mes" no
    #: tiene respuesta, porque la partida mezcla el analisis ofensivo con las preguntas que un
    #: analista le hace al asistente. Son dos decisiones de gasto distintas, con titulares
    #: distintos y, cuando el gasto dispara, lugares distintos donde mirar.
    CHAT_STEP_CONSUMPTION = "CHAT_STEP_CONSUMPTION"


class CreditLedger(Base):
    """Asiento inmutable del balance de créditos de una organización.

    R4: la tabla es *append-only*. `balance_after` es una fotografía del saldo
    en el momento del asiento, no un saldo derivado: cada fila es verificable de
    forma independiente y la suma de `amount_delta` reconstruye el histórico sin
    recalcular nada. El saldo denormalizado de `organizations.credit_balance` es
    una caché que este servicio mantiene bajo bloqueo pesimista.
    """

    __tablename__ = "credit_ledger"
    __table_args__ = (
        CheckConstraint("amount_delta <> 0", name="ck_credit_ledger_delta_nonzero"),
        CheckConstraint("balance_after >= 0", name="ck_credit_ledger_balance_nonnegative"),
        CheckConstraint(
            "reference_id IS NULL OR reference_id <> ''",
            name="ck_credit_ledger_reference_not_empty",
        ),
        Index("ix_credit_ledger_org_created", "organization_id", "created_at"),
        Index("ix_credit_ledger_reference", "organization_id", "reference_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT y no CASCADE: el rastro financiero sobrevive a su organización. Con
        # CASCADE, borrar un tenant dispara aquí un DELETE que el propio trigger
        # append-only bloquea, y el error resultante habla de un trigger de auditoría
        # en lugar de de la política de borrado. RESTRICT declara la intención en el
        # esquema y falla antes de tocar nada. El borrado de un tenant es lógico:
        # `organizations.deleted_at`.
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount_delta: Mapped[Decimal] = mapped_column(_CREDIT_PRECISION, nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(_CREDIT_PRECISION, nullable=False)
    reason: Mapped[LedgerReasonEnum] = mapped_column(
        SQLEnum(LedgerReasonEnum, name="ledger_reason_enum"), nullable=False, index=True
    )
    reference_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StripeEvent(Base):
    """Evento de Stripe ya procesado, para idempotencia de entregas.

    Stripe reintenta un webhook hasta recibir un `2xx`, y puede entregar el mismo
    evento más de una vez aunque la primera entrega funcionara. La restricción de
    unicidad sobre `event_id` es la que hace la recarga idempotente: el segundo
    intento choca contra ella y se descarta.
    """

    __tablename__ = "stripe_events"
    __table_args__ = (
        Index("ix_stripe_events_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    credits_granted: Mapped[Decimal | None] = mapped_column(_CREDIT_PRECISION, nullable=True)
    #: Importe cobrado, en centavos, leído del payload en el momento de la ingestión.
    #:
    #: `None` cuando el evento no es un cobro. No es `0`: Stripe no cobra cero, y un cero
    #: en una columna de importes se vería como una venta de $0 en la consola de
    #: administración en vez de como un evento que no traía importe.
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# --------------------------------------------------------------------------- #
# Precios de plataforma
# --------------------------------------------------------------------------- #

#: El precio de un crédito, con ocho decimales.
#:
#: ## Por qué `Numeric(18, 8)` y no `Numeric(18, 4)`
#:
#: Porque la paridad no es un saldo: es un **cociente** que multiplica importes en dólares para
#: dar créditos. Con cuatro decimales, una paridad de `0.0125` es representable pero
#: `0.012345678` no, y un precio de LLM por millón de tokens necesita más resolución que un saldo.
#: El saldo sigue en cuatro porque es dinero que se guarda; el cociente, no.
_PRICE_PRECISION = Numeric(18, 8)

#: El identificador de la fila única de precios de plataforma.
#:
#: ## Por qué una fila y no una tabla clave-valor
#:
#: ## Por qué una fila única
#:
#: Porque así no existe la pregunta de «cuál de estos valores está activo», que es la pregunta
#: que hace que una tabla clave-valor necesite una columna `is_active` y una condición de carrera
#: cuando dos operadores editan a la vez. Con una fila y `id` fijo, un cambio es un `UPDATE` sin
#: ambigüedad: siempre hay un precio vigente y es el de esa fila.
#:
#: Y la fila se crea con la migración a partir de la configuración, así que el arranque no
#: depende de que alguien la haya escrito antes.
ID_PRECIOS_PLATAFORMA = 1


class PlatformPricing(Base):
    """Precios vigentes de la plataforma, editables desde la consola de SuperAdmin.

    R1 exige que ningún precio viva en el código. Este modelo es la excepción declarada: el
    precio sale de aquí, y la configuración es solo el **valor de arranque** que la migración
    copia. Los precios comerciales —packs, escalera de descuento y plan Pro— viven en sus propias
    tablas porque tienen forma de lista, no de escalar.

    ## Por qué el precio queda congelado por trabajo, no por lectura

    ## Por qué aquí no hay `credits_per_usd` y sí una copia explícita

    ## Por qué el precio se congela y no se relee

    Porque un escaneo se encola y se cobra en `SCAN_CONSUMPTION`, y el ajuste contra el consumo
    real ocurre en el worker, bastante después. Si el precio se releyera en el worker, cambiarlo
    entre el encolado y la ejecución haría que la reserva y el ajuste usaran precios distintos, y
    el ledger recibiría una corrección que no corresponde a ninguna compra.

    La reserva ya se congela en el asiento del ledger —`credit_ledger` guarda el delta—, así que
    el precio no se relee en el cobro. Lo que sí hay que congelar es en el propio escaneo, para
    poder explicar meses después por qué ese cobro fue el que fue. Ese campo es
    `pentest_runs.precio_creditos_unitario` y lo escribe el servicio de pentests al encolar.

    ## Por qué `credits_per_usd` no se duplica aquí

    ## Por qué una sola fuente de la paridad

    Porque la paridad estaba declarada cuatro veces: en `settings.credits_per_usd`, en
    `llm_router/pricing.DEFAULT_CREDITS_PER_USD`, en `chat/billing.CREDITOS_POR_USD` y en dos
    literales de `billing/summary.py`. Solo dos leían la configuración, así que cambiar
    `CREDITS_PER_USD` hacía que el chat cobrara a 1:1 fijo y los escaneos a lo que dijera la
    variable, sin ningún error en ninguna parte. Al mover el precio a la base, esa divergencia
    pasa a ser un cobro incorrecto en producción y no una nota en un fichero.
    """

    __tablename__ = "platform_pricing"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_platform_pricing_singleton"),
        CheckConstraint("credits_per_usd > 0", name="ck_platform_pricing_credits_per_usd"),
        CheckConstraint("scan_credit_cost > 0", name="ck_platform_pricing_scan_cost"),
        CheckConstraint(
            "quick_scan_credit_multiplier > 0 AND quick_scan_credit_multiplier <= 1",
            name="ck_platform_pricing_quick_multiplier",
        ),
        CheckConstraint(
            "low_credit_balance_threshold >= 0", name="ck_platform_pricing_low_threshold"
        ),
        CheckConstraint(
            "custom_spend_minimum_usd > 0", name="ck_platform_pricing_custom_min_positive"
        ),
        CheckConstraint(
            "custom_spend_maximum_usd > 0", name="ck_platform_pricing_custom_max_positive"
        ),
        # El rango vacío es el fallo que este CHECK evita: un tope por debajo del mínimo hace
        # que `credits_for_spend` devuelva 0 para toda cantidad y que no se pueda comprar nada,
        # sin que nada falle —el esquema responde bien, el checkout también, y el panel
        # simplemente no ofrece nada.
        CheckConstraint(
            "custom_spend_maximum_usd >= custom_spend_minimum_usd",
            name="ck_platform_pricing_custom_range",
        ),
        CheckConstraint(
            "pro_subscription_monthly_usd > 0", name="ck_platform_pricing_pro_price_positive"
        ),
    )

    # `autoincrement=False` y a proposito: sin esto la columna se crea como SERIAL y Postgres
    # inventa una secuencia `platform_pricing_id_seq`. En una fila unica esa secuencia es un
    # peligro, no una comodidad: un `INSERT` que se saltase el `id` agarraria el siguiente valor
    # (2, 3, ...), lo rechazaria el `CHECK (id = 1)` con un error que no dice nada de una
    # secuencia, y el operador acabaria mirando la constraint en vez de al `INSERT`.
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, default=ID_PRECIOS_PLATAFORMA
    )
    # Sin `server_default`, y a proposito: la fila la siembra la migracion con los valores que
    # venian de la configuracion, y ese unico camino legitimo para crearla. Un `server_default`
    # seria un segundo camino silencioso --un `INSERT` que se Saltase la semilla crearia una fila
    # de precios con valores que nadie eligio, y cobrando con ellos. Con `NOT NULL` y sin default,
    # ese `INSERT` falla en vez de inventar un precio.
    credits_per_usd: Mapped[Decimal] = mapped_column(_PRICE_PRECISION, nullable=False)
    scan_credit_cost: Mapped[Decimal] = mapped_column(_PRICE_PRECISION, nullable=False)
    quick_scan_credit_multiplier: Mapped[Decimal] = mapped_column(_PRICE_PRECISION, nullable=False)
    low_credit_balance_threshold: Mapped[Decimal] = mapped_column(_PRICE_PRECISION, nullable=False)
    # Los tres importes comerciales. Van en `Numeric(18, 2)` y no en la escala de ocho
    # decimales de los escalares, porque son importes y no cocientes: son dólares que se
    # cobran por Stripe, y el céntimo es la unidad en la que eso ocurre.
    custom_spend_minimum_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    custom_spend_maximum_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    pro_subscription_monthly_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)

    #: Quién hizo el último cambio. Es la **traza mínima**: con una sola fila, un `updated_by`
    #: no dice qué cambió, solo quién tocó la fila. El histórico completo está en
    #: `platform_price_changes`, que es append-only.
    # `index=True` porque el `CHECKLIST` de la revision de base de datos del proyecto lo pide
    # para toda clave foranea, y aqui no es teorico: ambas columnas son `ON DELETE SET NULL`,
    # asi que borrar un usuario dispara un `UPDATE` buscandolas, y sin indice es un recorrido
    # secuencial sobre el historico entero de precios.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CreditPack(Base):
    """Un paquete de créditos comercializable: cuántos créditos y a qué precio."""

    __tablename__ = "platform_credit_packs"
    __table_args__ = (
        UniqueConstraint("credits", name="uq_credit_packs_credits"),
        CheckConstraint("credits > 0", name="ck_credit_packs_credits_positive"),
        CheckConstraint("amount_usd > 0", name="ck_credit_packs_amount_positive"),
        CheckConstraint("display_order >= 0", name="ck_credit_packs_order"),
        Index("ix_credit_packs_active_order", "is_active", "display_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class VolumeTier(Base):
    """Un tramo de la escalera de descuento por volumen gastado."""

    __tablename__ = "platform_volume_tiers"
    __table_args__ = (
        CheckConstraint("spend_min_usd > 0", name="ck_volume_tiers_spend_positive"),
        # HIGH 5: sin unicidad, dos filas con el mismo umbral satisfacen las dos el
        # `gasto >= minimo` y gana la ultima que llegue, que decide el planificador de
        # Postgres y no el codigo. El mismo gasto de un mismo cliente se cobraria con dos
        # descuentos distintos segun el plan de ejecucion, y ambos serian correctos segun
        # el esquema: no lanza nada y no hay ninguna constraint que lo rechace.
        UniqueConstraint("spend_min_usd", name="uq_volume_tiers_spend_min"),
        # El descuento es una fracción de 0 a 1, no un porcentaje: por eso el tope es 1 y no 100.
        CheckConstraint("discount >= 0 AND discount <= 1", name="ck_volume_tiers_discount_range"),
        CheckConstraint("display_order >= 0", name="ck_volume_tiers_order"),
        # HIGH 4: el indice va por `spend_min_usd`, no por `display_order`.
        #
        # Porque la escalera se lee ordenando por el umbral, y un indice cuyo
        # `ORDER BY` no coincide con su segunda columna no evita el `sort`. Con dos valores
        # de `is_active`, el planificador puede además decidir que un recorrido secuencial
        # ordenado sale mas barato que el indice, y entonces no se usa: el indice existia y
        # no hacia nada.
        Index("ix_volume_tiers_active_min", "is_active", "spend_min_usd"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    spend_min_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    discount: Mapped[Decimal] = mapped_column(
        Numeric(9, 6), nullable=False, server_default="0"
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PlatformPriceChange(Base):
    """Histórico **append-only** de cambios de precio de plataforma.

    ## Por qué una tabla propia y no `audit_log`

    Porque `audit_log.organization_id` es `NOT NULL` con clave foránea a `organizations`, y un
    precio de plataforma **no pertenece a ninguna organización**: pertenece a la plataforma. Las
    tres salidas serían: colgar el asiento de la organización de quien lo cambió —que es
    incorrecto y además aparecería filtrado por cliente en el visor global—, hacer nullable la
    columna —que toca un modelo append-only con su trigger de inmutabilidad y obliga a revisar
    dos consultas—, o una traza propia.

    Y la traza propia es la que además guarda el **valor anterior y el nuevo**, que es lo que
    hace falta para reconstruir por qué un cobro salió como salió. `audit_log` tiene
    `from_state`/`to_state` de 64 caracteres, y un importe con ocho decimales no cabe.

    ## Por qué append-only

    Por R4, igual que el ledger y que la evidencia. Un precio que se puede reescribir no es un
    precio que se pueda auditar, y un precio que no se puede auditar es una decisión comercial
    que nadie puede justificar seis meses después —que es justo lo que pide un SOC 2.
    """

    __tablename__ = "platform_price_changes"
    __table_args__ = (
        CheckConstraint("clave <> ''", name="ck_platform_price_changes_key"),
        CheckConstraint(
            "valor_anterior IS NOT NULL OR valor_nuevo IS NOT NULL",
            name="ck_platform_price_changes_algo_cambio",
        ),
        Index("ix_platform_price_changes_clave_fecha", "clave", "changed_at"),
        # LOW 13: el indice anterior cubre "el historico de esta clave", que es el uso
        # correcto. Este cubre lo que la consola pide de verdad al abrir la seccion, que es
        # "los ultimos cambios" sin filtro: `ORDER BY changed_at DESC LIMIT 50`. Con solo el
        # indice anterior, eso es un `sort` de toda la tabla.
        Index("ix_platform_price_changes_fecha", "changed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Qué se cambió: `credits_per_usd`, `scan_credit_cost`, `credit_pack:25`, `volume_tier:3`…
    clave: Mapped[str] = mapped_column(String(64), nullable=False)
    valor_anterior: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    valor_nuevo: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    # Con indice, por el mismo motivo que `updated_by`, y esta es la que mas duele: esta
    # tabla solo crece y nunca se poda.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Por qué se cambió. Es lo que distingue una corrección de una subida de precio.
    motivo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

# --------------------------------------------------------------------------- #
# Precios negociados por organización
# --------------------------------------------------------------------------- #


class PriceOperationEnum(StrEnum):
    """Las operaciones cuyo precio se puede negociar con un cliente.

    ## Por qué un enum y no una clave de texto libre

    Porque una clave libre admite typos, y un typo en la clave de un precio no da error: da un
    override que **nunca se aplica**, y el cliente sigue pagando el precio de plataforma sin que
    nadie se entere. Con un enum, la clave que no existe no compila.

    ## Por qué estas siete y no otras

    Porque son las que hoy consumen créditos o dólares y que de verdad varían por cliente: lo que
    cuesta un escaneo y una revisión —que es lo que cambia con el número de máquinas—, cómo se
    traduce un crédito en dólares, el recargo sobre el coste de los modelos de lenguaje, y los
    dos precios comerciales. Lo que **no** está aquí es todo lo que es tecnología y no precio:
    el timeout de una revisión, el número máximo de hallazgos, o el límite de la cola.
    """

    #: Créditos por escaneo completo. Es el precio que multiplica con el número de máquinas, y el
    #: que de verdad se negocia con un cliente grande.
    SCAN_CREDIT_COST = "SCAN_CREDIT_COST"
    #: Proporción del escaneo rápido sobre el completo.
    QUICK_SCAN_MULTIPLIER = "QUICK_SCAN_MULTIPLIER"
    #: Cuántos créditos vale un dólar **para este cliente**. Es la palanca más gruesa: mueve el
    #: precio de todo lo demás sin tocar ninguna operación.
    CREDITS_PER_USD = "CREDITS_PER_USD"
    #: Créditos por revisión de pull request.
    PR_REVIEW_CREDITS = "PR_REVIEW_CREDITS"
    #: Recargo sobre el coste base de los modelos de lenguaje, en fracción.
    LLM_MARKUP_PCT = "LLM_MARKUP_PCT"
    #: Precio mensual de la suscripción Pro, en dólares.
    PRO_MONTHLY_USD = "PRO_MONTHLY_USD"
    #: Precio en dólares de un pack de créditos concreto. Es la única que necesita `alcance`: el
    #: pack se identifica por su cantidad de créditos, que es lo que el panel muestra.
    CREDIT_PACK_AMOUNT = "CREDIT_PACK_AMOUNT"


class OrganizationPriceOverride(Base):
    """Un precio pactado con una organización concreta.

    ## Por qué existe esto y no un «precio Enterprise»

    Porque el precio fijo por plan no sirve: dos clientes con el mismo plan tienenzimienta
    máquinas distintas, y a la que tiene mil le cuestan mil escaneos. Un plan no describe el
    consumo. Este modelo dice lo que se pactó **para esa organización y esa operación**.

    ## Por qué es append-only

    Por R4, y porque un precio pactado es un **compromiso**. Si mañana se sube el precio a un
    cliente, lo que se escribe es una fila nueva con `valido_desde` en el futuro; la anterior
    se queda como estaba. Así, dentro de seis meses, la pregunta «¿con qué precio se le cobró
    este escaneo?» tiene respuesta, y la respuesta no depende de que nadie haya editado nada.

    Borrar o editar una fila de esta tabla sería reescribir el acuerdo. No hay ninguna ruta que
    lo haga, y el disparador de la base lo impide.

    ## Por qué `valido_hasta` y no borrar al renovar

    Porque un precio que caduca solo es un precio que no se puede dejar puesto por descuido. Con
    `valido_hasta`, el día de la renovación el override deja de aplicar **solo**, sin que nadie
    tenga que accordarse de retirarlo, y el cliente vuelve al precio de plataforma. Ese momento
    es exactamente donde un forgot dejaría a un cliente pagando el precio viejo, o a la
    plataforma cobrando el nuevo a quien ya se había renewing.

    ## Por qué el índice único es parcial

    Porque hay como mucho **un** override sin fecha de fin por operación y organización: el
    vigente. Los override con `valido_hasta` pueden ser cuantos se quiera, que es el histórico.
    Un `UNIQUE` normal prohibits los dos casos a la vez; un índice único parcial protege
    justamente el que importa: que no haya dos precios vigentes para lo mismo.
    """

    __tablename__ = "organization_price_overrides"
    __table_args__ = (
        CheckConstraint(
            "valido_hasta IS NULL OR valido_hasta > valido_desde",
            name="ck_org_price_override_window",
        ),
        # Un pack sin `alcance` no identifica qué pack es, y un override que no se puede aplicar
        # es un override que no existe.
        CheckConstraint(
            "operacion <> 'CREDIT_PACK_AMOUNT' OR alcance IS NOT NULL",
            name="ck_org_price_override_scope",
        ),
        CheckConstraint("valor IS NOT NULL", name="ck_org_price_override_valor"),
        Index(
            "ix_org_price_override_vigentes",
            "organization_id",
            "operacion",
            "valido_desde",
        ),
        # El **unico** override sin fecha de fin por operacion y organizacion: el vigente.
        #
        # Es un indice parcial y no una restriccion UNIQUE normal porque ambas cosas protege a
        # la vez: un `UNIQUE (organization_id, operacion, alcance)` impediria tener dos
        # overrides *historicos* de la misma operacion, que es justamente el historico de
        # subidas de precio de un cliente. El `WHERE valido_hasta IS NULL` deja pasar todos
        # los cerrados y prohibe solo que haya dos abiertos.
        Index(
            "uq_org_price_override_vigente",
            "organization_id",
            "operacion",
            "alcance",
            unique=True,
            postgresql_where=text("valido_hasta IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operacion: Mapped[PriceOperationEnum] = mapped_column(
        SQLEnum(PriceOperationEnum, name="price_operation_enum"), nullable=False
    )
    #: Solo lo usa `CREDIT_PACK_AMOUNT`, y vale la cantidad de créditos del pack. Es texto y no
    #: una FK porque el pack se identifica por su cantidad —que es su identidad comercial— y
    #: porque un override tiene que sobrevivir a que el pack se desactive.
    alcance: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valor: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    #: Por qué se pactó. Referencia de la cotización, nombre del cliente, lo que haga falta para
    #: que dentro de un año alguien entienda de dónde salió este número.
    motivo: Mapped[str] = mapped_column(String(255), nullable=False)
    valido_desde: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: `None` = vigente hasta que se pacte otro.
    valido_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
