"""Pruebas del motor de routing de LLMs, del margen y del consumo."""

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.llm_router.models import LLMModelConfig, LLMUseCaseEnum
from backend.apps.llm_router.pricing import compute_charge
from backend.apps.llm_router.routing import (
    LLMAllModelsInactiveError,
    next_in_chain,
    resolve_model_chain,
)
from backend.apps.organizations.models import User
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _superuser(session: AsyncSession) -> User:
    suffix = uuid.uuid4().hex
    user = User(
        email=f"llm-{suffix}@example.com",
        hashed_password="not-used",
        full_name="LLM Admin",
        email_verified=True,
        is_superuser=True,
    )
    session.add(user)
    await session.commit()
    return user


def _model(
    model_id: str,
    *,
    priority: int,
    use_case: LLMUseCaseEnum = LLMUseCaseEnum.ALL,
    is_active: bool = True,
    cost_in: str = "3.00",
    cost_out: str = "15.00",
    margin: str = "150.00",
) -> LLMModelConfig:
    return LLMModelConfig(
        model_id=model_id,
        display_name=model_id,
        base_cost_input_m=Decimal(cost_in),
        base_cost_output_m=Decimal(cost_out),
        markup_pct=Decimal(margin),
        priority_order=priority,
        is_active=is_active,
        use_case=use_case,
    )


async def _deactivate_everything(session: AsyncSession) -> None:
    """Apaga el catálogo sembrado para aislar el comportamiento de la prueba."""

    await session.execute(update(LLMModelConfig).values(is_active=False))
    await session.commit()


# --------------------------------------------------------------------------- #
# Cálculo de margen
# --------------------------------------------------------------------------- #


def test_compute_charge_applies_markup_over_base_cost() -> None:
    """150 % de recargo significa 2.5x el coste base, no 1.5/0.5."""

    charge = compute_charge(
        base_cost_input_m=Decimal("3.00"),
        base_cost_output_m=Decimal("15.00"),
        markup_pct=Decimal("150.00"),
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )

    assert charge.base_cost_usd == Decimal("3.000000")
    assert charge.client_price_usd == Decimal("7.500000")
    assert charge.net_profit_usd == Decimal("4.500000")
    assert charge.markup_multiplier == Decimal("2.5")


def test_compute_charge_sums_input_and_output() -> None:
    charge = compute_charge(
        base_cost_input_m=Decimal("3.00"),
        base_cost_output_m=Decimal("15.00"),
        markup_pct=Decimal("0.00"),
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )

    assert charge.base_cost_usd == Decimal("18.000000")
    assert charge.client_price_usd == Decimal("18.000000")
    assert charge.net_profit_usd == Decimal("0.000000")
    assert charge.net_profit_pct == Decimal("0.0000")


def test_price_minus_cost_always_equals_profit() -> None:
    """El redondeo no puede abrir una diferencia entre precio, coste y beneficio."""

    for margin in ("7.50", "33.33", "150.00", "275.00"):
        for tokens in (0, 1, 7, 999, 123_456):
            charge = compute_charge(
                base_cost_input_m=Decimal("3.14159"),
                base_cost_output_m=Decimal("15.92653"),
                markup_pct=Decimal(margin),
                prompt_tokens=tokens,
                completion_tokens=tokens,
            )
            assert charge.client_price_usd - charge.base_cost_usd == charge.net_profit_usd, (
                margin,
                tokens,
            )


def test_zero_tokens_costs_nothing_and_reports_no_profit_percentage() -> None:
    charge = compute_charge(
        base_cost_input_m=Decimal("3.00"),
        base_cost_output_m=Decimal("15.00"),
        markup_pct=Decimal("150.00"),
        prompt_tokens=0,
        completion_tokens=0,
    )

    assert charge.base_cost_usd == Decimal("0E-6")
    assert charge.net_profit_usd == Decimal("0E-6")
    assert charge.net_profit_pct == Decimal("0.0000")
    assert charge.client_cost_credits == Decimal("0.0000")


def test_compute_charge_credits_follow_configured_rate() -> None:
    charge = compute_charge(
        base_cost_input_m=Decimal("2.00"),
        base_cost_output_m=Decimal("2.00"),
        markup_pct=Decimal("100.00"),
        prompt_tokens=1_000_000,
        completion_tokens=0,
        credits_per_usd=Decimal("10.00"),
    )

    # 2.00 USD de coste, 100 % de recargo -> 4.00 USD -> 40 créditos.
    assert charge.client_price_usd == Decimal("4.000000")
    assert charge.client_cost_credits == Decimal("40.0000")


def test_compute_charge_rejects_invalid_inputs() -> None:
    for kwargs in (
        {"prompt_tokens": -1},
        {"completion_tokens": -1},
        {"markup_pct": Decimal("-1")},
        {"base_cost_input_m": Decimal("-1")},
        {"base_cost_output_m": Decimal("-1")},
        {"credits_per_usd": Decimal("0")},
    ):
        base = {
            "base_cost_input_m": Decimal("3.00"),
            "base_cost_output_m": Decimal("15.00"),
            "markup_pct": Decimal("150.00"),
            "prompt_tokens": 10,
            "completion_tokens": 10,
        }
        base.update(kwargs)
        with pytest.raises(ValueError):
            compute_charge(**base)  # pyright: ignore[reportArgumentType]


# --------------------------------------------------------------------------- #
# Cadena de resolución y fallback
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_chain_orders_by_priority_and_filters_use_case(
    integration_session: AsyncSession,
) -> None:
    """La cadena se ordena por prioridad y excluye inactivos y casos ajenos.

    La migración siembra tres modelos `ALL` que también participan, así que la
    aserción es sobre el invariante global y el orden relativo de los modelos de
    la prueba, no sobre la lista completa.
    """

    assert integration_session is not None
    await _deactivate_everything(integration_session)
    integration_session.add_all(
        [
            _model("fallback-b", priority=30, use_case=LLMUseCaseEnum.ALL),
            _model("primary", priority=10, use_case=LLMUseCaseEnum.QUICK_SCAN),
            _model("quick-primary", priority=11, use_case=LLMUseCaseEnum.QUICK_SCAN),
            _model("quick-secondary", priority=12, use_case=LLMUseCaseEnum.QUICK_SCAN),
            _model(
                "inactive",
                priority=1,
                use_case=LLMUseCaseEnum.QUICK_SCAN,
                is_active=False,
            ),
            _model("autofix-only", priority=1, use_case=LLMUseCaseEnum.AUTOFIX),
        ]
    )
    await integration_session.commit()

    chain = await resolve_model_chain(integration_session, LLMUseCaseEnum.QUICK_SCAN)
    ordered_ids = [model.model_id for model in chain]

    assert [model.priority_order for model in chain] == sorted(
        model.priority_order for model in chain
    )
    assert "inactive" not in ordered_ids
    assert "autofix-only" not in ordered_ids
    assert ordered_ids.index("primary") < ordered_ids.index("quick-primary")
    assert ordered_ids.index("quick-primary") < ordered_ids.index("quick-secondary")
    assert ordered_ids.index("quick-secondary") < ordered_ids.index("fallback-b")


@pytest.mark.asyncio
async def test_resolve_chain_fails_closed_when_every_model_is_inactive(
    integration_session: AsyncSession,
) -> None:
    """Sin modelos activos la cadena falla, en lugar de recurrir a un default."""

    assert integration_session is not None
    await _deactivate_everything(integration_session)

    with pytest.raises(LLMAllModelsInactiveError):
        await resolve_model_chain(integration_session, LLMUseCaseEnum.DEEP_PENTEST)


@pytest.mark.asyncio
async def test_deactivating_the_primary_falls_back_to_the_next_model(
    integration_session: AsyncSession,
) -> None:
    """Apagar el primario en caliente devuelve la cadena al siguiente."""

    assert integration_session is not None
    await _deactivate_everything(integration_session)
    primary = _model("only-primary", priority=1)
    fallback = _model("only-fallback", priority=2)
    integration_session.add_all([primary, fallback])
    await integration_session.commit()

    first = await resolve_model_chain(integration_session, LLMUseCaseEnum.ALL)
    assert [model.model_id for model in first] == ["only-primary", "only-fallback"]

    primary.is_active = False
    await integration_session.commit()

    second = await resolve_model_chain(integration_session, LLMUseCaseEnum.ALL)
    assert [model.model_id for model in second] == ["only-fallback"]


def test_next_in_chain_walks_the_fallback_order() -> None:
    first = _model("a", priority=1)
    second = _model("b", priority=2)
    third = _model("c", priority=3)
    chain = [first, second, third]

    assert next_in_chain(chain, first) is second
    assert next_in_chain(chain, second) is third
    assert next_in_chain(chain, third) is None


def test_is_transient_status_classifies_provider_errors() -> None:
    from backend.apps.llm_router.routing import is_transient_status

    for code in (408, 429, 500, 502, 503, 504, 599):
        assert is_transient_status(code) is True, code
    for code in (400, 401, 403, 404, 422):
        assert is_transient_status(code) is False, code


def test_classify_provider_error_separates_transient_failures() -> None:
    import httpx

    from backend.apps.llm_router.routing import classify_provider_error

    transient, reason = classify_provider_error(httpx.ReadTimeout("tardó"))
    assert transient is True and reason == "timeout"

    transient, reason = classify_provider_error(httpx.ConnectError("no conecta"))
    assert transient is True and reason == "transport"

    response = httpx.Response(429, request=httpx.Request("POST", "https://openrouter.ai"))
    transient, reason = classify_provider_error(
        httpx.HTTPStatusError("429", request=response.request, response=response)
    )
    assert transient is True and reason == "http_429"

    response = httpx.Response(401, request=httpx.Request("POST", "https://openrouter.ai"))
    transient, reason = classify_provider_error(
        httpx.HTTPStatusError("401", request=response.request, response=response)
    )
    assert transient is False and reason == "http_401"

    transient, reason = classify_provider_error(ValueError("error de negocio"))
    assert transient is False and reason == "ValueError"


# --------------------------------------------------------------------------- #
# Consola de SuperAdmin
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_can_manage_llm_models(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    user = await _superuser(integration_session)
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/v1/admin/llm/",
            json={
                "model_id": "mistralai/mistral-large",
                "display_name": "Mistral Large",
                "base_cost_input_m": "2.00",
                "base_cost_output_m": "6.00",
                "markup_pct": "120.00",
                "priority_order": 4,
                "use_case": "DEEP_PENTEST",
            },
            headers=headers,
        )
        model_id = created.json()["id"]
        updated = await client.patch(
            f"/api/v1/admin/llm/{model_id}",
            json={"markup_pct": "200.00", "is_active": False, "priority_order": 5},
            headers=headers,
        )
        listed = await client.get("/api/v1/admin/llm/", headers=headers)

    assert created.status_code == 201
    assert created.json()["markup_pct"] == "120.0000"
    assert created.json()["is_active"] is True
    assert created.json()["use_case"] == "DEEP_PENTEST"
    assert updated.status_code == 200
    assert updated.json()["markup_pct"] == "200.0000"
    assert updated.json()["is_active"] is False
    assert updated.json()["priority_order"] == 5
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] >= 4
    assert any(item["model_id"] == "mistralai/mistral-large" for item in payload["items"])
    first = payload["items"][0]
    assert {
        "model_id",
        "display_name",
        "base_cost_input_m",
        "base_cost_output_m",
        "markup_pct",
        "priority_order",
        "is_active",
        "use_case",
        "usage",
    } <= set(first)
    assert {
        "runs",
        "prompt_tokens",
        "completion_tokens",
        "base_cost_usd",
        "net_profit_usd",
    } <= set(first["usage"])


@pytest.mark.asyncio
async def test_admin_cannot_rename_a_model_identifier(
    integration_session: AsyncSession,
) -> None:
    """`model_id` no es mutable: los registros de consumo lo citan como identidad."""

    assert integration_session is not None
    user = await _superuser(integration_session)
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/api/v1/admin/llm/", headers=headers)
        model_id = listed.json()["items"][0]["id"]
        renamed = await client.patch(
            f"/api/v1/admin/llm/{model_id}",
            json={"model_id": "otro/proveedor"},
            headers=headers,
        )
        empty = await client.patch(
            f"/api/v1/admin/llm/{model_id}", json={}, headers=headers
        )
        missing = await client.patch(
            "/api/v1/admin/llm/00000000-0000-0000-0000-000000000000",
            json={"is_active": False},
            headers=headers,
        )

    assert renamed.status_code == 422
    assert empty.status_code == 422
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_admin_rejects_duplicate_model_and_invalid_values(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    user = await _superuser(integration_session)
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    body = {
        "model_id": "zhipu/glm-4-plus",
        "display_name": "GLM",
        "base_cost_input_m": "0.60",
        "base_cost_output_m": "0.60",
        "markup_pct": "80.00",
        "priority_order": 6,
        "use_case": "ALL",
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/v1/admin/llm/", json=body, headers=headers)
        duplicate = await client.post("/api/v1/admin/llm/", json=body, headers=headers)
        negative = await client.post(
            "/api/v1/admin/llm/",
            json={**body, "model_id": "x/negativo", "markup_pct": "-10.00"},
            headers=headers,
        )
        zero_priority = await client.post(
            "/api/v1/admin/llm/",
            json={**body, "model_id": "x/prioridad", "priority_order": 0},
            headers=headers,
        )
        negative_cost = await client.post(
            "/api/v1/admin/llm/",
            json={**body, "model_id": "x/coste", "base_cost_input_m": "-1.00"},
            headers=headers,
        )
        bad_format = await client.post(
            "/api/v1/admin/llm/",
            json={**body, "model_id": "sin-prefijo-de-proveedor"},
            headers=headers,
        )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert negative.status_code == 422
    assert zero_priority.status_code == 422
    assert negative_cost.status_code == 422
    assert bad_format.status_code == 422


@pytest.mark.asyncio
async def test_llm_admin_requires_superuser(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    from backend.apps.organizations.models import (
        Membership,
        Organization,
        RoleEnum,
    )

    suffix = uuid.uuid4().hex
    organization = Organization(name=f"LLM {suffix}", slug=f"llm-{suffix}")
    regular = User(
        email=f"regular-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Regular",
        email_verified=True,
    )
    integration_session.add_all([organization, regular])
    await integration_session.flush()
    integration_session.add(
        Membership(organization_id=organization.id, user_id=regular.id, role=RoleEnum.ADMIN)
    )
    await integration_session.commit()
    headers = {
        "Authorization": f"Bearer {create_access_token({'sub': str(regular.id)})}",
        "X-Organization-Id": str(organization.id),
    }
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/api/v1/admin/llm/", headers=headers)
        anonymous = await client.get("/api/v1/admin/llm/")

    assert listed.status_code == 403
    assert anonymous.status_code == 401


@pytest.mark.asyncio
async def test_seed_contains_valid_models_in_fallback_order(
    integration_session: AsyncSession,
) -> None:
    """Invariantes del catálogo sembrado, no una lista fija de slugs.

    El catálogo oficial cambia por decisión de producto —el Bloque 5.2 lo sustituyó dos
    veces en el mismo día—, así que fijar aquí la lista completa haría que cada cambio
    rompiese una prueba que no está probando el catálogo sino las reglas que lo
    gobiernan: precios no negativos, recargo no negativo y prioridades sin empates.

    Los modelos retirados se conservan deactivated para no perder su historial de
    consumo, así que las invariantes de cadena se comprueban sobre los **activos**: los
    inactivos no entran en `resolve_model_chain` y su prioridad es irrelevante. Exigir
    unicidad sobre todas las filas obligaría a borrar el historial de lo retirado cada
    vez que el Owner cambia de catálogo.
    """

    assert integration_session is not None
    models = (await integration_session.execute(select(LLMModelConfig))).scalars().all()
    assert models, "el catálogo sembrado no puede estar vacío"
    active = [model for model in models if model.is_active]
    assert active, "el catálogo no puede quedarse sin ningún modelo activo"

    for model in models:
        assert model.base_cost_input_m >= 0, model.model_id
        assert model.base_cost_output_m >= 0, model.model_id
        assert model.markup_pct >= 0, model.model_id
        assert model.use_case in set(LLMUseCaseEnum), model.model_id
        assert model.priority_order >= 1, model.model_id
        assert model.display_name.strip(), model.model_id

    # La cadena de resolución depende de que las prioridades de los activos sean
    # únicas: dos modelos activos con la misma prioridad harían ambiguo cuál es el
    # primario y el desempate passaría a depender del alfabeto del slug.
    priorities = [model.priority_order for model in active]
    assert len(set(priorities)) == len(priorities), "hay prioridades duplicadas entre activos"

    # El primario de cada caso de uso es el de menor prioridad, y `resolve_model_chain`
    # tiene que devolverlo primero. Es la misma regla que usa el runner.
    chain = await resolve_model_chain(integration_session, LLMUseCaseEnum.DEEP_PENTEST)
    assert chain
    assert chain[0].priority_order == min(model.priority_order for model in chain)
