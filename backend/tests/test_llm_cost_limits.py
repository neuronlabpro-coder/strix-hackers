"""Contratos de selección y precedencia de topes de coste."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from backend.apps.llm_router.cost_limits import (
    CandidatoLimite,
    filtros_de_alcance,
    resolver_limites,
)
from backend.apps.llm_router.models import LLMCostLimit, NivelLimiteCosteEnum, OperacionCosteEnum
from backend.apps.organizations.models import PlanTierEnum


def test_la_consulta_admite_los_alcances_aplicables_sin_abrir_otro_tenant() -> None:
    tenant = uuid4()
    otro_tenant = uuid4()
    consulta = select(LLMCostLimit).where(
        filtros_de_alcance(
            organization_id=tenant,
            operation=OperacionCosteEnum.PENTEST_QUICK,
            plan_tier=PlanTierEnum.PRO,
        )
    )
    sql = str(
        consulta.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert " OR " in sql
    assert str(tenant) in sql
    assert str(otro_tenant) not in sql


def test_default_global_administrado_precede_al_fallback_del_entorno() -> None:
    regla = CandidatoLimite(
        scope=NivelLimiteCosteEnum.GLOBAL,
        max_budget_usd=Decimal("4.25"),
        max_turns=19,
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        valid_until=None,
    )
    resultado = resolver_limites(
        (regla,),
        organization_id=uuid4(),
        operation=OperacionCosteEnum.PENTEST_QUICK,
        plan_tier=PlanTierEnum.PRO,
        default_max_budget_usd=Decimal("3"),
        default_max_turns=10,
        ahora=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert resultado.max_budget_usd == Decimal("4.25")
    assert resultado.max_turns == 19
    assert resultado.origen_presupuesto.nivel is NivelLimiteCosteEnum.GLOBAL


def test_precedencia_por_campo_org_operacion_plan_global() -> None:
    tenant = uuid4()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reglas = (
        CandidatoLimite(
            scope=NivelLimiteCosteEnum.GLOBAL,
            max_budget_usd=Decimal("8"),
            max_turns=80,
            valid_from=now,
            valid_until=None,
        ),
        CandidatoLimite(
            scope=NivelLimiteCosteEnum.PLAN,
            plan_tier=PlanTierEnum.PRO,
            max_budget_usd=Decimal("6"),
            max_turns=60,
            valid_from=now,
            valid_until=None,
        ),
        CandidatoLimite(
            scope=NivelLimiteCosteEnum.OPERACION,
            operation=OperacionCosteEnum.PENTEST_QUICK,
            max_budget_usd=Decimal("4"),
            max_turns=None,
            valid_from=now,
            valid_until=None,
        ),
        CandidatoLimite(
            scope=NivelLimiteCosteEnum.ORGANIZACION,
            organization_id=tenant,
            max_budget_usd=None,
            max_turns=20,
            valid_from=now,
            valid_until=None,
        ),
    )
    result = resolver_limites(
        reglas,
        organization_id=tenant,
        operation=OperacionCosteEnum.PENTEST_QUICK,
        plan_tier=PlanTierEnum.PRO,
        default_max_budget_usd=Decimal("10"),
        default_max_turns=100,
        ahora=now,
    )
    assert result.max_budget_usd == Decimal("4")
    assert result.origen_presupuesto.nivel is NivelLimiteCosteEnum.OPERACION
    assert result.max_turns == 20
    assert result.origen_turnos.nivel is NivelLimiteCosteEnum.ORGANIZACION
