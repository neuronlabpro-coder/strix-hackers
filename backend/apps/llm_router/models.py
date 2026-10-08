"""Modelos del catálogo de LLMs y del registro de consumo."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
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

from backend.apps.organizations.models import PlanTierEnum
from backend.core.database import Base

# Numérico exacto para dinero. `Float` acumula error de coma flotante y un
# ledger de créditos con derivas de centavo es un ledger que no cuadra.
_COST_PRECISION = Numeric(18, 8)
_PCT_PRECISION = Numeric(9, 4)
#: Duración de una ejecución en segundos, con milisegundos. `Numeric` y no `Float`
#: porque se resta de un intervalo real para saber cuánto tardó, y esa resta es la
#: que se compara entre proveedores.
_DURATION_PRECISION = Numeric(12, 3)


class LLMUseCaseEnum(StrEnum):
    """Casos de uso para los que se puede enrutar un modelo concreto."""

    ALL = "ALL"
    QUICK_SCAN = "QUICK_SCAN"
    DEEP_PENTEST = "DEEP_PENTEST"
    AUTOFIX = "AUTOFIX"
    #: Cadenas del chat con agentes.
    #:
    #: Es un valor mas y no una columna booleana aparte. Con `is_active_for_chat` y
    #: `is_active_for_pentest` habria que responder a "puede este modelo usarse en el chat" y a
    #: "puede usarse en un escaneo" por separado, y en cuanto un modelo valiera en los dos habria
    #: que mantener las dos columnas coherentes a mano. Un unico `use_case` con la lista de
    #: casos admittedidos, mas el transversal `ALL`, dice lo mismo sin ese segundo sitio donde
    #: equivocarse.
    CHAT = "CHAT"


class NivelLimiteCosteEnum(StrEnum):
    """Los cuatro niveles de la resolución de topes de gasto, **de más a menos específico**.

    ## Por qué un enum y no un entero de prioridad

    Porque el orden es la regla entera y tiene que poder leerse en la base, en la API y en el
    registro sin mirar el código. `PRIORIDAD` documenta la clase, pero el `str` es el que viaja;
    un número escrito a mano en una fila es un número que alguien acabará reordenando sin querer.

    ## Por qué `GLOBAL` es un nivel y no «la ausencia de niveles»

    Porque el default global **tiene que quedar registrado en la resolución** igual que los otros.
    «No había nada configurado y se usó el del `.env`» es una respuesta que el operador de soporte
    necesita poder ver, y si el default no fuera un nivel no habría ningún sitio donde escribirla.
    """

    ORGANIZACION = "ORGANIZACION"
    OPERACION = "OPERACION"
    PLAN = "PLAN"
    GLOBAL = "GLOBAL"


#: El orden exacto de resolución. El primero que tenga un valor para un campo **gana**, y el resto
#: se registra como descartado con el motivo. Está declarado en un solo sitio porque este orden es
#: la regla comercial y no puede estar escrito en el resolver y en su prueba a la vez.
PRIORIDAD_DE_NIVELES: tuple[NivelLimiteCosteEnum, ...] = (
    NivelLimiteCosteEnum.ORGANIZACION,
    NivelLimiteCosteEnum.OPERACION,
    NivelLimiteCosteEnum.PLAN,
    NivelLimiteCosteEnum.GLOBAL,
)


class OperacionCosteEnum(StrEnum):
    """La clase de trabajo que consume tokens, y para la que se puede fijar un tope.

    No es lo mismo que `ScanModeEnum` porque el tope no lo declara el modo sino **qué se está
    vendiendo**: una revisión de pull request no es un `STANDARD` de pentest aunque se ejecute con
    el mismo modo, y un Enterprise necesita poder pagarle más margen a una que a la otra sin que eso
    dependa de un nombre de modo de escaneo.
    """

    PENTEST_QUICK = "PENTEST_QUICK"
    PENTEST_DEEP = "PENTEST_DEEP"
    PR_REVIEW = "PR_REVIEW"
    CHAT = "CHAT"


class LLMModelConfig(Base):
    """Modelo de OpenRouter con su coste base y su recargo comercial.

    `markup_pct` es un **recargo sobre el coste base**, no un margen sobre el
    precio de venta: 150 significa que el cliente paga 2,5 veces el coste. Con la
    definición habitual de margen, un 150 % sería imposible porque el precio
    tendría que ser negativo. El nombre evita la ambigüedad.

    `priority_order` construye la cadena de resolución: 1 es el primario del caso
    de uso y los siguientes son fallbacks. El motor solo enruta a modelos activos,
    de modo que apagar un modelo en caliente lo retira de la cadena sin borrar su
    histórico de consumo.
    """

    __tablename__ = "llm_model_configs"
    __table_args__ = (
        UniqueConstraint("model_id", name="uq_llm_model_configs_model_id"),
        CheckConstraint("priority_order >= 1", name="ck_llm_model_configs_priority_positive"),
        CheckConstraint(
            "base_cost_input_m >= 0 AND base_cost_output_m >= 0",
            name="ck_llm_model_configs_costs_nonnegative",
        ),
        CheckConstraint(
            "cached_input_cost_m IS NULL OR cached_input_cost_m >= 0",
            name="ck_llm_model_configs_cached_cost_nonnegative",
        ),
        CheckConstraint(
            "context_limit_tokens IS NULL OR context_limit_tokens > 0",
            name="ck_llm_model_configs_context_positive",
        ),
        CheckConstraint(
            "output_limit_tokens IS NULL OR output_limit_tokens > 0",
            name="ck_llm_model_configs_output_limit_positive",
        ),
        CheckConstraint("markup_pct >= 0", name="ck_llm_model_configs_markup_nonnegative"),
        CheckConstraint("display_name <> ''", name="ck_llm_model_configs_display_name"),
        Index("ix_llm_model_configs_routing", "use_case", "is_active", "priority_order"),
        Index(
            "uq_llm_model_configs_default_por_caso",
            "use_case",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_cost_input_m: Mapped[Decimal] = mapped_column(_COST_PRECISION, nullable=False)
    base_cost_output_m: Mapped[Decimal] = mapped_column(_COST_PRECISION, nullable=False)
    #: El precio por millón de tokens de la entrada que el proveedor **sirvió de su caché**.
    #:
    #: ## Por qué `NULL` significa algo y no «no rellenado»
    #:
    #: Porque son dos afirmaciones distintas sobre lo que el proveedor **publica**, y el cálculo del
    #: coste real las necesita separadas:
    #:
    #: - `NULL`: el proveedor no publica precio de caché para este modelo. La caché se valora al
    #:   precio de la entrada normal y `llm_usage_events.cache_price_published` queda en `false`.
    #: - `0`: el proveedor publica precio de caché y es cero. Es un dato, no una ausencia.
    #:
    #: Tratar `NULL` como error dejaría sin medir el coste de cualquier modelo sin caché publicada,
    #: que es la mayoría, y un modelo al que nunca se le pidió caché daría el mismo número que uno
    #: al que se le pidió y el proveedor no la sirvió. Son tres hechos y esta columna los separa.
    cached_input_cost_m: Mapped[Decimal | None] = mapped_column(
        _COST_PRECISION,
        nullable=True,
        comment=(
            "Precio por 1M de la entrada que el proveedor sirvio de su cache. "
            "NULL = el proveedor no publica precio de cache"
        ),
    )
    #: Quién publica el modelo. Es el dato que hace falta para saber **de quién** hay que mirar la
    #: lista de precios cuando la plataforma empiece a gastar de más, y para leer una fila de
    #: consumo sin tener que ir a la configuración del proveedor a buscarlo.
    provider: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Proveedor que publica el modelo",
    )
    #: Ventana de contexto que declara el modelo, si la declara. `NULL` cuando el proveedor no la
    #: publica, que es un dato distinto de «cero»: con cero el motor no podría enviar nada.
    context_limit_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Tokens de contexto que declara el modelo. NULL si el proveedor no lo publica",
    )
    #: Tokens de salida que declara el modelo, si los declara. Misma distinción que arriba.
    output_limit_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Tokens de salida que declara el modelo. NULL si el proveedor no lo publica",
    )
    markup_pct: Mapped[Decimal] = mapped_column(_PCT_PRECISION, nullable=False)
    priority_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    use_case: Mapped[LLMUseCaseEnum] = mapped_column(
        SQLEnum(LLMUseCaseEnum, name="llm_use_case_enum"),
        nullable=False,
        default=LLMUseCaseEnum.ALL,
        server_default=LLMUseCaseEnum.ALL.name,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class LLMUsageEvent(Base):
    """Consumo real de un modelo, necesario para auditar el margen declarado.

    Sin esta tabla, el margen del catálogo sería una intención comercial sin
    respaldo: nadie podría responder cuánto cuesta de verdad operar la
    plataforma ni qué modelo está quemando presupuesto.
    """

    __tablename__ = "llm_usage_events"
    __table_args__ = (
        CheckConstraint("prompt_tokens >= 0", name="ck_llm_usage_prompt_tokens_nonnegative"),
        CheckConstraint(
            "completion_tokens >= 0", name="ck_llm_usage_completion_tokens_nonnegative"
        ),
        CheckConstraint(
            "cached_tokens IS NULL OR cached_tokens >= 0", name="ck_llm_usage_cached_nonnegative"
        ),
        CheckConstraint("base_cost_usd >= 0", name="ck_llm_usage_base_cost_nonnegative"),
        CheckConstraint(
            "provider_cost_usd IS NULL OR provider_cost_usd >= 0",
            name="ck_llm_usage_provider_cost_nonnegative",
        ),
        CheckConstraint(
            "estimated_provider_cost_usd IS NULL OR estimated_provider_cost_usd >= 0",
            name="ck_llm_usage_estimated_cost_nonnegative",
        ),
        CheckConstraint(
            "input_cost_usd IS NULL OR input_cost_usd >= 0",
            name="ck_llm_usage_input_cost_nonnegative",
        ),
        CheckConstraint(
            "cached_input_cost_usd IS NULL OR cached_input_cost_usd >= 0",
            name="ck_llm_usage_cached_cost_nonnegative",
        ),
        CheckConstraint(
            "output_cost_usd IS NULL OR output_cost_usd >= 0",
            name="ck_llm_usage_output_cost_nonnegative",
        ),
        CheckConstraint(
            "budget_max_usd IS NULL OR budget_max_usd > 0", name="ck_llm_usage_budget_max_positive"
        ),
        CheckConstraint(
            "budget_consumed_usd IS NULL OR budget_consumed_usd >= 0",
            name="ck_llm_usage_budget_consumed_nonnegative",
        ),
        Index("ix_llm_usage_model_created", "model_config_id", "created_at"),
        Index("ix_llm_usage_run", "run_id"),
        # El informe de margen agrupa por organización y ordena por fecha, y ese es el único
        # recorrido que hace de verdad: la suma por tenant del periodo. Sin este índice el informe
        # acaba en un `Seq Scan` sobre toda la telemetría en cuanto haya unos miles de filas, que
        # es justo cuando se empieza a mirar.
        Index("ix_llm_usage_org_created", "organization_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_model_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pentest_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    use_case: Mapped[LLMUseCaseEnum] = mapped_column(
        SQLEnum(LLMUseCaseEnum, name="llm_use_case_enum"), nullable=False
    )
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    base_cost_usd: Mapped[Decimal] = mapped_column(_COST_PRECISION, nullable=False)
    net_profit_usd: Mapped[Decimal] = mapped_column(_COST_PRECISION, nullable=False)
    #: Tokens de entrada que el proveedor **sirvió de su caché**.
    #:
    #: `NULL` y no `0` porque son dos afirmaciones distintas: `NULL` es «el motor no publicó el
    #: desglose de caché» y `0` es «publicó que no hubo caché». Tratar el primero como el segundo
    #: haría que un informe de caché dijera «no se pidió caché» cuando en realidad no lo sabemos.
    cached_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Entrada que el proveedor sirvio de su cache. NULL si el motor no lo publica",
    )
    #: ¿El catálogo tenía un precio de caché declarado para este modelo?
    #:
    #: Viaja a la fila de consumo porque es la **única** forma de que un número de coste se pueda
    #: leer sin ir a mirar el catálogo, y porque separa los dos casos que comparten importe: un
    #: modelo al que se le pidió caché y el proveedor no la sirvió da `cached_tokens = 0`, y un
    #: modelo sin precio de caché publicado da `cached_tokens = N` con `false` aquí.
    cache_price_published: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="El catalogo declara un precio de cache para el modelo de este evento",
    )
    #: El proveedor que publica el modelo, copiado del catálogo al escribir el evento.
    #:
    #: Se copia en lugar de consultarse para que la fila se pueda leer sola: un informe de gasto
    #: por proveedor no debería necesitar un `JOIN` con el catálogo para saber de quién es el gasto.
    provider: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Proveedor que publica el modelo, copiado del catalogo al escribir el evento",
    )
    #: Lo que el proveedor **facturó** de verdad, de `run.json.llm_usage.cost`.
    #:
    #: Es la mitad izquierda de la divergencia. `NULL` cuando el modo de ejecución no deja un
    #: artefacto que lo declare —el contenedor no lo publica— y poner `0` afirmaría que el
    #: proveedor no cobró nada, que es una afirmación sobre el dinero que nadie ha hecho.
    provider_cost_usd: Mapped[Decimal | None] = mapped_column(
        _COST_PRECISION,
        nullable=True,
        comment="Coste que el proveedor cobro de verdad, de run.json.llm_usage.cost",
    )
    #: Lo que el catálogo de la plataforma **calcula** que costó ese mismo consumo.
    #:
    #: ## Por qué no es lo mismo que `base_cost_usd`
    #:
    #: Porque `base_cost_usd` es la base sobre la que se aplica el recargo al **cliente**, y esa
    #: fórmula no se toca: el importe está sellado en el `credit_ledger` (*append-only*, R4) y
    #: cambiarlo sería cambiar lo que se cobra. Este campo es el mismo consumo valorado **con el
    #: precio de caché declarado**, que es lo que hace que los dos números sean comparables: sin
    #: caché, el catálogo estima casi cuatro veces lo que el proveedor cobra.
    estimated_provider_cost_usd: Mapped[Decimal | None] = mapped_column(
        _COST_PRECISION,
        nullable=True,
        comment="Coste del proveedor estimado por el catalogo, con el precio de cache declarado",
    )
    input_cost_usd: Mapped[Decimal | None] = mapped_column(_COST_PRECISION, nullable=True)
    cached_input_cost_usd: Mapped[Decimal | None] = mapped_column(_COST_PRECISION, nullable=True)
    output_cost_usd: Mapped[Decimal | None] = mapped_column(_COST_PRECISION, nullable=True)
    #: Cuánto duró la ejecución, en segundos.
    duration_seconds: Mapped[Decimal | None] = mapped_column(
        _DURATION_PRECISION,
        nullable=True,
        comment="Duracion de la ejecucion en segundos, segun el motor",
    )
    #: El tope con el que se lanzó el run, ya resuelto por los cuatro niveles.
    #:
    #: ## Por qué se guarda el tope **resuelto** y no el nivel que lo ganó
    #:
    #: Porque son dos preguntas distintas y aquí solo cabe una sin perder la otra: este campo dice
    #: **cuál** era el tope, y la resolución completa —qué nivel ganó y por qué— se responde
    #: calling a `resolver_limites`. Duplicar aquí la resolución entera sería copiar una regla de
    #: negocio en la fila, y una regla copiada se queda vieja en cuanto la regla cambia.
    budget_max_usd: Mapped[Decimal | None] = mapped_column(
        _COST_PRECISION,
        nullable=True,
        comment="Tope de gasto del proveedor que se aplico al run, ya resuelto",
    )
    #: Cuánto del tope se consumió.
    #:
    #: Es el importe del proveedor cuando el motor lo declara y la estimación del catálogo si no.
    #: No `NULL` en el sentido de «no lo sé»: aquí la intención es que haya un número con el que
    #: comparar el tope, porque un tope que se puede exceder sin medir es un tope que no controla.
    budget_consumed_usd: Mapped[Decimal | None] = mapped_column(
        _COST_PRECISION,
        nullable=True,
        comment=(
            "Consumo del proveedor contra el tope: el importe real, "
            "o la estimacion si no se publico"
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LLMCostLimit(Base):
    """Un tope de gasto del proveedor en uno de los cuatro niveles de resolución.

    ## Por qué una tabla y no cuatro columnas

    Porque los cuatro niveles se resuelven **en el mismo orden** y con la **misma** función, y
    cuatro columnas obligarían a cuatro `if` en el mismo sitio, cada uno con su criterio de
    «vacío». Con una tabla, añadir un nivel es añadir un valor a `NivelLimiteCosteEnum` y nada
    más: el resolver no cambia.

    ## Por qué las filas **no** se borran

    Porque la pregunta «¿por qué este run tuvo un tope de 3,00 y el otro de 25?» solo tiene
    respuesta si la política vigente cuando se lanzó sigue existiendo. Un `UPDATE` de
    `max_budget_usd` reescribe la historia de todos los runs que lo usaron, que es justo el
    problema que la regla de vigencia por fechas viene a evitar: el cambio se inserta y el
    anterior se cierra con `valid_until`.
    """

    __tablename__ = "llm_cost_limits"
    __table_args__ = (
        # Un tope tiene que acotar **algo**. Una fila con los dos a `NULL` no es una política
        # restrictiva: es una fila que se puede escribir para no escribir nada, y cuyo único efecto
        # sería aparecer en el informe de resolución como si hubiera configurado algo.
        CheckConstraint(
            "max_budget_usd IS NOT NULL OR max_turns IS NOT NULL",
            name="ck_llm_cost_limits_al_menos_un_tope",
        ),
        CheckConstraint(
            "max_budget_usd IS NULL OR max_budget_usd > 0",
            name="ck_llm_cost_limits_budget_positive",
        ),
        CheckConstraint(
            "max_turns IS NULL OR max_turns > 0", name="ck_llm_cost_limits_turns_positive"
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_llm_cost_limits_vigencia_coherente",
        ),
        # ## Por qué el alcance tiene que coincidir con el nivel
        #
        # Porque una fila `PLAN` con `organization_id` puesto se leería como un override de
        # organización y ganaría por ser más específica, lo contrario de lo que su nivel dice. Sin
        # esta comprobación, un `INSERT` con un campo de más no falla: guarda, gana y nadie se
        # entera hasta que un cliente recibe un tope que nadie le pactó.
        CheckConstraint(
            "(scope = 'ORGANIZACION') = (organization_id IS NOT NULL)",
            name="ck_llm_cost_limits_organizacion_coherente",
        ),
        CheckConstraint(
            "(scope = 'OPERACION') = (operation IS NOT NULL)",
            name="ck_llm_cost_limits_operacion_coherente",
        ),
        CheckConstraint(
            "(scope = 'PLAN') = (plan_tier IS NOT NULL)", name="ck_llm_cost_limits_plan_coherente"
        ),
        # `GLOBAL` no necesita su propia comprobación: si los tres anteriores son coherentes, un
        # nivel `GLOBAL` con alguno puesto los rechazan.
        # `organization_id` va en el índice porque es la clave foránea y porque es el primer
        # criterio de la resolución por organización.
        Index("ix_llm_cost_limits_organizacion", "organization_id", "scope"),
        # El resto de la resolución va por `(scope, operation, plan_tier)`: son los dos campos que
        # se consultan sin saber la organización.
        Index("ix_llm_cost_limits_alcance", "scope", "operation", "plan_tier"),
        # `created_by` es clave foránea y va indexada por la misma razón que las demás.
        Index("ix_llm_cost_limits_created_by", "created_by"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[NivelLimiteCosteEnum] = mapped_column(
        SQLEnum(NivelLimiteCosteEnum, name="nivel_limite_coste_enum"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    operation: Mapped[OperacionCosteEnum | None] = mapped_column(
        SQLEnum(OperacionCosteEnum, name="operacion_coste_enum"),
        nullable=True,
    )
    plan_tier: Mapped[PlanTierEnum | None] = mapped_column(
        # El **mismo** enum nativo de `organizations.plan_tier`, y no una copia en texto. Dos
        # enums con los mismos valores son dos verdades que divergen el día que se añada un plan:
        # esta columna seguiría aceptando el plan nuevo mientras la organización no, y el `CHECK`
        # de coherencia seguiría pasando porque compara contra su propio enum.
        SQLEnum(PlanTierEnum, name="plan_tier_enum"),
        nullable=True,
    )
    max_budget_usd: Mapped[Decimal | None] = mapped_column(_COST_PRECISION, nullable=True)
    max_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Desde cuándo aplica. Con fecha de futuro, la fila existe pero todavía no manda: es el caso
    #: normal de un aviso de renovación, y por eso la resolución filtra por **las dos** fechas.
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Hasta cuándo aplica. `NULL` es «abierta»: sigue mandando hasta que alguien la cierre.
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = (
    "PRIORIDAD_DE_NIVELES",
    "LLMCostLimit",
    "LLMModelConfig",
    "LLMUsageEvent",
    "LLMUseCaseEnum",
    "NivelLimiteCosteEnum",
    "OperacionCosteEnum",
)
