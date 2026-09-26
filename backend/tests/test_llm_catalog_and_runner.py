"""Pruebas del catálogo oficial de OpenRouter, el enlace al runner y la telemetría."""

import uuid
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import Numeric, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.service import apply_credit_delta, credit_balance_of
from backend.apps.llm_router.models import LLMModelConfig, LLMUsageEvent, LLMUseCaseEnum
from backend.apps.llm_router.pricing import compute_charge
from backend.apps.llm_router.routing import resolve_model_chain
from backend.apps.organizations.models import Organization
from backend.core.config import settings
from backend.workers.runner.sandbox import StrixSandboxManager
from backend.workers.runner.telemetry import (
    LlmUsageTelemetry,
    extract_token_usage,
    select_runtime_models,
)

pytestmark = pytest.mark.integration

# Catálogo oficial elegido por el Owner: (model_id, coste in, out, markup, prioridad, caso de uso)
#
# `z-ai/glm-5.3` es el primario de toda la plataforma. Va declarado como `ALL` y no
# como `DEEP_PENTEST` porque una fila solo puede tener un caso de uso y `model_id` es
# único; `ALL` es transversal y `resolve_model_chain` lo incluye en cada cadena, así
# que el comportamiento pedido —primario en todas, incluida DEEP_PENTEST— se cumple
# sin duplicar la fila ni relajar la restricción de unicidad.
EXPECTED_CATALOG = [
    ("z-ai/glm-5.3", "0.40", "1.60", "200.00", 1, "ALL"),
    ("openai/gpt-6-astra", "4.00", "18.00", "150.00", 2, "DEEP_PENTEST"),
    ("anthropic/claude-opus-5.5", "5.00", "25.00", "150.00", 3, "DEEP_PENTEST"),
    ("deepseek/deepseek-v4-pro-0813", "1.20", "4.80", "200.00", 4, "DEEP_PENTEST"),
    ("anthropic/claude-fable-5.1", "2.00", "8.00", "150.00", 5, "AUTOFIX"),
    ("openai/gpt-6-sol", "1.50", "6.00", "200.00", 6, "ALL"),
    ("moonshotai/kimi-k3", "0.80", "3.20", "250.00", 7, "ALL"),
    ("deepseek/deepseek-v4.1-flash", "0.15", "0.60", "300.00", 8, "QUICK_SCAN"),
]

# Modelos del catálogo anterior que el Owner retiró. Ninguno debe quedar activo: un
# fallback que enrute a un modelo que producto ya no ofrece es un coste sincobrar.
RETIRED_MODEL_IDS = (
    "anthropic/claude-3.7-sonnet",
    "deepseek/deepseek-r1",
    "openai/o3-mini",
    "openai/gpt-4o",
    "deepseek/deepseek-chat",
    "openrouter/auto",
    "anthropic/claude-3.5-sonnet",
)


@pytest.mark.asyncio
async def test_official_catalog_is_seeded_in_fallback_order(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    models = (await integration_session.execute(select(LLMModelConfig))).scalars().all()
    by_id = {model.model_id: model for model in models}

    for model_id, cost_in, cost_out, markup, priority, use_case in EXPECTED_CATALOG:
        assert model_id in by_id, f"falta {model_id} en el catálogo"
        model = by_id[model_id]
        assert model.base_cost_input_m == Decimal(cost_in), model_id
        assert model.base_cost_output_m == Decimal(cost_out), model_id
        assert model.markup_pct == Decimal(markup), model_id
        assert model.priority_order == priority, model_id
        assert model.use_case == LLMUseCaseEnum(use_case), model_id
        assert model.is_active is True, model_id
        # El nombre visible no puede quedar vacío: la tabla de `/admin/llm` lo pinta.
        assert model.display_name.strip(), model_id

    # El catálogo activo es exactamente el del Owner, sin sobras ni filas deactivated
    # que la consola seguiría mostrando como si estuvieran disponibles.
    active = {model.model_id for model in models if model.is_active}
    assert active == {model_id for model_id, *_ in EXPECTED_CATALOG}


@pytest.mark.asyncio
async def test_retired_models_are_not_active(
    integration_session: AsyncSession,
) -> None:
    """Un modelo retirado sale de la cadena, pero conserva su historial de consumo.

    No se borra si tiene eventos asociados porque `llm_usage_events` lo referencia; lo
    que se garantiza es que `is_active` es falso, que es lo que `resolve_model_chain`
    filtra.
    """

    assert integration_session is not None
    models = (await integration_session.execute(select(LLMModelConfig))).scalars().all()
    by_id = {model.model_id: model for model in models}
    for retired in RETIRED_MODEL_IDS:
        if retired in by_id:
            assert by_id[retired].is_active is False, retired


@pytest.mark.asyncio
async def test_markup_column_replaced_profit_margin(
    integration_session: AsyncSession,
) -> None:
    """El campo de margen se llama `markup_pct`: es un recargo, no un margen."""

    assert integration_session is not None
    columns = {column.name for column in LLMModelConfig.__table__.columns}
    assert "markup_pct" in columns
    assert "profit_margin_pct" not in columns


@pytest.mark.asyncio
async def test_credit_balance_uses_twelve_four_numeric(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    del integration_session
    assert settings.credits_per_usd == Decimal("1.00")

    # El tipo se compara contra la clase `Numeric` y no contra el tipo concreto de la
    # columna: comprobar `precision` sobre un `TypeEngine` genérico no la convierte en
    # una `Numeric`, y el `cast` documenta que la aserción depende de esa suposición.
    column_type = cast(Numeric, Organization.__table__.columns["credit_balance"].type)
    assert isinstance(column_type, Numeric)
    assert column_type.precision == 12
    assert column_type.scale == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "use_case",
    [LLMUseCaseEnum.ALL, LLMUseCaseEnum.DEEP_PENTEST, LLMUseCaseEnum.QUICK_SCAN],
)
async def test_glm_53_is_the_primary_of_every_chain(
    integration_session: AsyncSession,
    use_case: LLMUseCaseEnum,
) -> None:
    """`z-ai/glm-5.3` es lo primero que se inyecta al contenedor, en toda cadena.

    Es el requisito del Owner y la razón por la que el modelo se declara `ALL`: al ser
    transversal, encabeza las tres cadenas sin duplicar la fila. La comprobación se hace
    sobre la cadena resuelta y no sobre el orden de la tabla, porque lo que importa es
    qué recibe el runner.
    """

    assert integration_session is not None
    chain = await resolve_model_chain(integration_session, use_case)
    assert chain, f"la cadena de {use_case.value} no debe estar vacía"
    assert chain[0].model_id == "z-ai/glm-5.3", use_case.value
    # La cadena sale ordenada por prioridad y solo con modelos activos.
    priorities = [model.priority_order for model in chain]
    assert priorities == sorted(priorities), use_case.value
    assert all(model.is_active for model in chain), use_case.value


@pytest.mark.asyncio
async def test_deep_pentest_chain_keeps_its_specific_fallbacks(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    chain = await resolve_model_chain(integration_session, LLMUseCaseEnum.DEEP_PENTEST)
    ids = [model.model_id for model in chain]
    # Los específicos de DEEP_PENTEST entran detrás del primario `ALL`, por prioridad.
    for specific in (
        "openai/gpt-6-astra",
        "anthropic/claude-opus-5.5",
        "deepseek/deepseek-v4-pro-0813",
    ):
        assert specific in ids, specific
    # `AUTOFIX` no debe colarse en la cadena de pentest: es un caso de uso distinto.
    assert "anthropic/claude-fable-5.1" not in ids


@pytest.mark.asyncio
async def test_quick_scan_chain_keeps_the_flash_fallback(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    chain = await resolve_model_chain(integration_session, LLMUseCaseEnum.QUICK_SCAN)
    ids = [model.model_id for model in chain]
    # El modelo barato existe como último recurso de la cadena rápida. Con el primacy
    # de `glm-5.3` en prioridad 1 no es el primario, y esta prueba lo deja escrito para
    # que una recalibración de prioridades no pase inadvertida.
    assert "deepseek/deepseek-v4.1-flash" in ids
    assert chain[-1].model_id == "deepseek/deepseek-v4.1-flash"
    # Y el resto de la cadena rápida es más caro que el flash: si se eligiera el flash
    # como primario, el resto no tendría sentido.
    assert chain[0].base_cost_input_m > chain[-1].base_cost_input_m


# --------------------------------------------------------------------------- #
# Enlace al runner
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_primary_model_reaches_the_container_as_strix_llm(
    integration_session: AsyncSession,
) -> None:
    """El camino completo: cadena resuelta → sandbox → variable de entorno del motor.

    El motor Strix lee `STRIX_LLM` (`strix/llm/config.py` y `strix/interface/main.py`
    lo consultan con `os.getenv`, y `main.py` aborta el arranque si falta). Inyectar
    con otro nombre no daría error: el contenedor caería al `openai/gpt-5` que el motor
    trae por defecto, cada escaneo correría fuera del catálogo y `llm_usage_events`
    nunca se escribiría. Esta prueba ata la resolución con el nombre exacto que lee el
    motor, que es el detalle que hace que todo el bloque funcione.
    """

    assert integration_session is not None
    chain = await resolve_model_chain(integration_session, LLMUseCaseEnum.DEEP_PENTEST)
    manager = StrixSandboxManager(
        "6f1b1f5c-2f4a-4a2f-9a1c-9c2f1a7d5e10",
        "app.example.com",
        "DEEP",
        client=object(),  # type: ignore[arg-type]
        llm_model=chain[0].model_id,
    )
    environment = manager.container_environment()

    assert environment["STRIX_LLM"] == "z-ai/glm-5.3"
    # `STRIX_LLM_MODEL` no existe para el motor. Si alguien lo añade aquí creyendo que
    # es un alias, el contenedor lo ignorará en silencio y esta aserción no lo detectaría;
    # por eso se comprueba explícitamente que la variable antigua no está.
    assert "STRIX_LLM_MODEL" not in environment


def test_sandbox_injects_the_resolved_model_instead_of_the_default() -> None:
    """El contenedor recibe el modelo resuelto, no el `DEFAULT_STRIX_LLM` fijo."""

    manager = StrixSandboxManager(
        "6f1b1f5c-2f4a-4a2f-9a1c-9c2f1a7d5e10",
        "app.example.com",
        "DEEP",
        client=object(),  # type: ignore[arg-type]
        llm_model="z-ai/glm-5.3",
    )
    environment = manager.container_environment()

    assert environment["STRIX_LLM"] == "z-ai/glm-5.3"
    assert environment["STRIX_LLM"] != settings.default_strix_llm
    assert environment["STRIX_NON_INTERACTIVE"] == "1"


def test_sandbox_keeps_the_default_when_no_model_is_resolved() -> None:
    manager = StrixSandboxManager(
        "6f1b1f5c-2f4a-4a2f-9a1c-9c2f1a7d5e10",
        "app.example.com",
        "STANDARD",
        client=object(),  # type: ignore[arg-type]
    )
    environment = manager.container_environment()
    assert environment["STRIX_LLM"] == settings.default_strix_llm


def test_sandbox_rejects_an_empty_model_slug() -> None:
    with pytest.raises(ValueError):
        StrixSandboxManager(
            "6f1b1f5c-2f4a-4a2f-9a1c-9c2f1a7d5e10",
            "app.example.com",
            "STANDARD",
            client=object(),  # type: ignore[arg-type]
            llm_model="   ",
        )


# --------------------------------------------------------------------------- #
# Selección de modelos y fallback
# --------------------------------------------------------------------------- #


def test_select_runtime_models_returns_the_ordered_chain() -> None:
    primary = LLMModelConfig(
        model_id="a/model",
        display_name="A",
        base_cost_input_m=Decimal("1"),
        base_cost_output_m=Decimal("2"),
        markup_pct=Decimal("100"),
        priority_order=1,
        is_active=True,
        use_case=LLMUseCaseEnum.ALL,
    )
    fallback = LLMModelConfig(
        model_id="b/model",
        display_name="B",
        base_cost_input_m=Decimal("1"),
        base_cost_output_m=Decimal("2"),
        markup_pct=Decimal("100"),
        priority_order=2,
        is_active=True,
        use_case=LLMUseCaseEnum.ALL,
    )
    chain = select_runtime_models([primary, fallback])
    assert chain == ["a/model", "b/model"]


def test_select_runtime_models_ignores_inactive_and_dedupes() -> None:
    models = [
        LLMModelConfig(
            model_id="a/model",
            display_name="A",
            base_cost_input_m=Decimal("1"),
            base_cost_output_m=Decimal("2"),
            markup_pct=Decimal("100"),
            priority_order=1,
            is_active=False,
            use_case=LLMUseCaseEnum.ALL,
        ),
        LLMModelConfig(
            model_id="b/model",
            display_name="B",
            base_cost_input_m=Decimal("1"),
            base_cost_output_m=Decimal("2"),
            markup_pct=Decimal("100"),
            priority_order=2,
            is_active=True,
            use_case=LLMUseCaseEnum.ALL,
        ),
        LLMModelConfig(
            model_id="b/model",
            display_name="B duplicado",
            base_cost_input_m=Decimal("1"),
            base_cost_output_m=Decimal("2"),
            markup_pct=Decimal("100"),
            priority_order=3,
            is_active=True,
            use_case=LLMUseCaseEnum.ALL,
        ),
    ]
    assert select_runtime_models(models) == ["b/model"]


# --------------------------------------------------------------------------- #
# Telemetría de tokens
# --------------------------------------------------------------------------- #


def test_extract_token_usage_reads_a_usage_block() -> None:
    usage = extract_token_usage(
        """
        {"status": "completed", "scan_id": "s-1", "findings": [],
         "usage": {"prompt_tokens": 120000, "completion_tokens": 34000}}
        """
    )
    assert usage is not None
    assert usage.prompt_tokens == 120000
    assert usage.completion_tokens == 34000


def test_extract_token_usage_accepts_alternative_key_names() -> None:
    usage = extract_token_usage(
        '{"status":"completed","scan_id":"s-1","findings":[],'
        '"token_usage":{"input_tokens":50,"output_tokens":10}}'
    )
    assert usage is not None
    assert usage.prompt_tokens == 50
    assert usage.completion_tokens == 10


def test_extract_token_usage_returns_none_when_absent_or_invalid() -> None:
    assert extract_token_usage('{"status":"completed","scan_id":"s","findings":[]}') is None
    assert extract_token_usage("no es json") is None
    assert (
        extract_token_usage(
            '{"status":"completed","scan_id":"s","findings":[],'
            '"usage":{"prompt_tokens":-5,"completion_tokens":10}}'
        )
        is None
    )


def test_extract_token_usage_sums_a_list_of_usage_blocks() -> None:
    usage = extract_token_usage(
        '{"status":"completed","scan_id":"s","findings":[],'
        '"usage":[{"prompt_tokens":10,"completion_tokens":1},'
        '{"prompt_tokens":20,"completion_tokens":2}]}'
    )
    assert usage is not None
    assert usage.prompt_tokens == 30
    assert usage.completion_tokens == 3


# --------------------------------------------------------------------------- #
# Tarificación y deducción tras el run
# --------------------------------------------------------------------------- #


async def _catalog_model(session: AsyncSession, model_id: str) -> LLMModelConfig:
    """Carga un modelo del catálogo sembrado por la migración.

    Las pruebas de telemetría no crean modelos nuevos: el runner solo puede tarificar
    contra modelos que existen en el catálogo, así que usar filas propias no probaría
    el camino real y además rompería el `model_id` único.
    """

    model = (
        await session.execute(select(LLMModelConfig).where(LLMModelConfig.model_id == model_id))
    ).scalar_one()
    return model


async def _funded_tenant(session: AsyncSession, balance: str) -> Organization:
    """Crea una organización cuyo saldo viene del ledger, nunca de la columna.

    Escribir `credit_balance` a mano dejaría el saldo denormalizado sin asiento que lo
    respalde, y `credit_balance_of` —que suma el ledger— no coincidiría con la columna.
    En producción el saldo de partida es siempre un bono de alta.
    """

    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Telemetría {suffix}", slug=f"tel-{suffix}")
    session.add(organization)
    await session.flush()
    if Decimal(balance) > 0:
        session.add(
            CreditLedger(
                organization_id=organization.id,
                amount_delta=Decimal(balance),
                balance_after=Decimal(balance),
                reason=LedgerReasonEnum.SIGNUP_BONUS,
            )
        )
        organization.credit_balance = Decimal(balance)
    await session.commit()
    return organization


@pytest.mark.asyncio
async def test_telemetry_charges_the_ledger_and_records_usage(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization = await _funded_tenant(integration_session, "1000")
    model = await _catalog_model(integration_session, "anthropic/claude-3.7-sonnet")
    before = await credit_balance_of(integration_session, organization.id)

    telemetry = LlmUsageTelemetry(
        model=model,
        use_case=LLMUseCaseEnum.DEEP_PENTEST,
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )
    charged = await telemetry.charge(
        session=integration_session,
        organization_id=organization.id,
        run_id=None,
        credits_per_usd=settings.credits_per_usd,
    )

    assert charged is not None
    assert charged.client_cost_credits == Decimal("7.5000")
    assert await credit_balance_of(integration_session, organization.id) == before - Decimal("7.5")

    entry = (
        await integration_session.execute(
            select(CreditLedger)
            .where(CreditLedger.organization_id == organization.id)
            .where(CreditLedger.reason == LedgerReasonEnum.ADMIN_ADJUSTMENT)
        )
    ).scalars().all()
    assert len(entry) == 1
    assert entry[0].amount_delta == -Decimal("7.5000")

    usage_events = (
        await integration_session.execute(
            select(LLMUsageEvent).where(LLMUsageEvent.model_config_id == model.id)
        )
    ).scalars().all()
    assert len(usage_events) == 1
    assert usage_events[0].prompt_tokens == 1_000_000
    assert usage_events[0].base_cost_usd == Decimal("3.00")
    assert usage_events[0].net_profit_usd == Decimal("4.50")


@pytest.mark.asyncio
async def test_telemetry_charges_nothing_without_token_usage(
    integration_session: AsyncSession,
) -> None:
    """Sin datos de tokens no hay cargo: inventar un coste sería tarificar al aire."""

    assert integration_session is not None
    organization = await _funded_tenant(integration_session, "100")
    model = await _catalog_model(integration_session, "deepseek/deepseek-chat")

    telemetry = LlmUsageTelemetry(
        model=model,
        use_case=LLMUseCaseEnum.QUICK_SCAN,
        prompt_tokens=None,
        completion_tokens=None,
    )
    charged = await telemetry.charge(
        session=integration_session,
        organization_id=organization.id,
        run_id=None,
        credits_per_usd=settings.credits_per_usd,
    )

    assert charged is None
    assert await credit_balance_of(integration_session, organization.id) == Decimal("100")
    usage_events = (
        await integration_session.execute(
            select(LLMUsageEvent).where(LLMUsageEvent.model_config_id == model.id)
        )
    ).scalars().all()
    assert usage_events == []


@pytest.mark.asyncio
async def test_telemetry_refunds_the_overcharged_reserve(
    integration_session: AsyncSession,
) -> None:
    """El escaneo se cobra por adelantado; la telemetría compensa la diferencia.

    El tenant debe acabar pagando exactamente el consumo real: ni el doble por
    cobrar consumo y reserva, ni de menos por no ajustar lo que se cobró de más.
    """

    assert integration_session is not None
    organization = await _funded_tenant(integration_session, "100")
    model = await _catalog_model(integration_session, "openai/o3-mini")
    reserved = Decimal("10")
    starting = await credit_balance_of(integration_session, organization.id)

    # El router descuenta la reserva antes de encolar el escaneo.
    await apply_credit_delta(
        session=integration_session,
        organization_id=organization.id,
        amount=-reserved,
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id="run-referencia",
    )
    await integration_session.commit()

    charged = await LlmUsageTelemetry(
        model=model,
        use_case=LLMUseCaseEnum.DEEP_PENTEST,
        prompt_tokens=1_000_000,
        completion_tokens=0,
    ).charge(
        session=integration_session,
        organization_id=organization.id,
        run_id=None,
        credits_per_usd=settings.credits_per_usd,
        reserved_credits=reserved,
    )

    # 1,10 USD de coste con 150 % de recargo = 2,75 USD = 2,75 créditos. La reserva
    # de 10 sobraba, así que se devuelven 7,25 y el saldo final es 100 - 2,75.
    assert charged is not None
    assert charged.client_cost_credits == Decimal("2.7500")
    assert await credit_balance_of(integration_session, organization.id) == (
        starting - Decimal("2.75")
    )


@pytest.mark.asyncio
async def test_telemetry_collects_consumption_above_the_reserve(
    integration_session: AsyncSession,
) -> None:
    """Si el motor gastó más de lo reservado, la diferencia se cobra.

    El caso inverso al reembolso: sin esto, un escaneo que consuma más de lo
    reservado sería un unchecked gratis para la plataforma.
    """

    assert integration_session is not None
    organization = await _funded_tenant(integration_session, "100")
    model = await _catalog_model(integration_session, "openai/o3-mini")
    reserved = Decimal("1")
    starting = await credit_balance_of(integration_session, organization.id)

    await apply_credit_delta(
        session=integration_session,
        organization_id=organization.id,
        amount=-reserved,
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id="run-sobreconsumo",
    )
    await integration_session.commit()

    charged = await LlmUsageTelemetry(
        model=model,
        use_case=LLMUseCaseEnum.DEEP_PENTEST,
        prompt_tokens=1_000_000,
        completion_tokens=0,
    ).charge(
        session=integration_session,
        organization_id=organization.id,
        run_id=None,
        credits_per_usd=settings.credits_per_usd,
        reserved_credits=reserved,
    )

    assert charged is not None
    assert charged.client_cost_credits == Decimal("2.7500")
    # Se cobran 2,75 en total: 1 de reserva más 1,75 de exceso.
    assert await credit_balance_of(integration_session, organization.id) == (
        starting - Decimal("2.75")
    )


@pytest.mark.asyncio
async def test_telemetry_writes_no_ledger_entry_when_the_charge_is_exact(
    integration_session: AsyncSession,
) -> None:
    """Si la reserva coincide con el consumo, el ledger no ensucia con un asiento de cero."""

    assert integration_session is not None
    organization = await _funded_tenant(integration_session, "100")
    model = await _catalog_model(integration_session, "openai/o3-mini")
    exact = Decimal("2.75")

    charged = await LlmUsageTelemetry(
        model=model,
        use_case=LLMUseCaseEnum.DEEP_PENTEST,
        prompt_tokens=1_000_000,
        completion_tokens=0,
    ).charge(
        session=integration_session,
        organization_id=organization.id,
        run_id=None,
        credits_per_usd=settings.credits_per_usd,
        reserved_credits=exact,
    )

    assert charged is not None
    entries = (
        await integration_session.execute(
            select(CreditLedger).where(
                CreditLedger.organization_id == organization.id
            )
        )
    ).scalars().all()
    # Solo el bono de alta: ni el asiento del consumo ni el del ajuste.
    assert [entry.reason for entry in entries] == [LedgerReasonEnum.SIGNUP_BONUS]
    # El evento de uso sí se registra aunque no haya asiento: el margen es auditable
    # aunque el movimiento de créditos sea cero.
    events = (
        await integration_session.execute(
            select(LLMUsageEvent).where(LLMUsageEvent.model_config_id == model.id)
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].net_profit_usd == Decimal("1.65")


def test_compute_charge_matches_the_documented_markup_for_the_seed() -> None:
    """El catálogo sembrado y la calculadora cuentan la misma historia."""

    for model_id, cost_in, _cost_out, markup, _priority, _use_case in EXPECTED_CATALOG:
        charge = compute_charge(
            base_cost_input_m=Decimal(cost_in),
            base_cost_output_m=Decimal("0"),
            markup_pct=Decimal(markup),
            prompt_tokens=1_000_000,
            completion_tokens=0,
            credits_per_usd=Decimal("1.00"),
        )
        assert charge.markup_multiplier == Decimal(1) + Decimal(markup) / Decimal(100), model_id
