"""Un tope administrado declara un único alcance y al menos un valor."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.apps.admin.cost_limit_schemas import CostLimitCreate


def test_override_de_organizacion_valido() -> None:
    tenant = uuid4()
    regla = CostLimitCreate.model_validate(
        {"scope": "ORGANIZACION", "organization_id": str(tenant), "max_budget_usd": "4.25"}
    )
    assert regla.organization_id == tenant
    assert regla.max_budget_usd == Decimal("4.25")


@pytest.mark.parametrize(
    "datos",
    [
        {"scope": "ORGANIZACION", "max_turns": 10},
        {"scope": "GLOBAL", "organization_id": str(uuid4()), "max_turns": 10},
        {"scope": "PLAN", "plan_tier": "PRO"},
    ],
)
def test_alcances_incoherentes_se_rechazan(datos: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CostLimitCreate.model_validate(datos)
