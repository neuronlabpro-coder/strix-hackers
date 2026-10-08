"""Los acuerdos y productos conservan dinero y vigencia como datos validados."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.apps.commercial.schemas import EnterpriseAgreementCreate, PentestProductUpdate


def test_acuerdo_individual_con_override_explicito() -> None:
    acuerdo = EnterpriseAgreementCreate.model_validate(
        {
            "price_monthly_usd": "1200",
            "seats": 12,
            "included_credits": "500",
            "discount_pct": "10",
            "features": {"supply_chain": False, "container_scanning": True},
        }
    )
    assert acuerdo.price_monthly_usd == Decimal("1200")
    assert acuerdo.features["supply_chain"] is False


def test_producto_no_acepta_creditos_negativos() -> None:
    with pytest.raises(ValidationError):
        PentestProductUpdate.model_validate({"credits_required": "-1"})
