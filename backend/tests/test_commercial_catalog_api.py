"""Productos sembrados y acuerdo Enterprise limitado a su tenant."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.service import apply_credit_delta
from backend.apps.commercial.models import PentestProduct
from backend.apps.commercial.service import features_for_organization
from backend.apps.llm_router.cost_limits import limites_de, operacion_de_scan_mode
from backend.apps.organizations.models import Organization, PlanTierEnum
from backend.apps.pentests.models import ScanModeEnum, TargetTypeEnum
from backend.apps.pentests.schemas import PentestCreate
from backend.apps.pentests.service import queue_pentest
from backend.core.config import settings

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_catalogo_y_override_enterprise_aislado(integration_session: AsyncSession) -> None:
    productos = (await integration_session.execute(select(PentestProduct))).scalars().all()
    assert {item.slug for item in productos} >= {
        "rightsized-pentest",
        "full-audit",
        "enterprise-pentest",
    }
    org_a = Organization(
        name="Acuerdo A", slug=f"acuerdo-a-{uuid.uuid4().hex}", plan_tier=PlanTierEnum.PRO
    )
    org_b = Organization(
        name="Acuerdo B", slug=f"acuerdo-b-{uuid.uuid4().hex}", plan_tier=PlanTierEnum.PRO
    )
    integration_session.add_all((org_a, org_b))
    await integration_session.commit()
    try:
        from datetime import UTC, datetime

        from backend.apps.commercial.models import EnterpriseAgreement

        integration_session.add(
            EnterpriseAgreement(
                organization_id=org_a.id,
                features={"supply_chain": True},
                valid_from=datetime.now(UTC),
            )
        )
        await integration_session.commit()
        a = await features_for_organization(integration_session, org_a.id)
        b = await features_for_organization(integration_session, org_b.id)
        assert a is not None and a["supply_chain"] is True
        assert b is not None and b["supply_chain"] is False
    finally:
        await integration_session.execute(
            delete(Organization).where(Organization.id.in_((org_a.id, org_b.id)))
        )
        await integration_session.commit()


@pytest.mark.asyncio
async def test_producto_asociado_fija_creditos_y_topes(integration_session: AsyncSession) -> None:
    producto = await integration_session.scalar(
        select(PentestProduct).where(PentestProduct.slug == "rightsized-pentest")
    )
    assert producto is not None
    modo_anterior = producto.scan_mode
    creditos_anteriores = producto.credits_required
    presupuesto_anterior = producto.max_budget_usd
    turnos_anteriores = producto.max_turns
    producto.scan_mode = "QUICK"
    producto.credits_required = Decimal("7")
    producto.max_budget_usd = Decimal("4.25")
    producto.max_turns = 17
    await integration_session.commit()
    org = Organization(name="Producto de prueba", slug=f"producto-{uuid.uuid4().hex}")
    integration_session.add(org)
    await integration_session.flush()
    await apply_credit_delta(
        session=integration_session,
        organization_id=org.id,
        amount=Decimal("100"),
        reason=LedgerReasonEnum.SIGNUP_BONUS,
    )
    await integration_session.commit()
    try:
        run = await queue_pentest(
            integration_session,
            org,
            PentestCreate(
                target_type=TargetTypeEnum.DOMAIN,
                target_identifier="example.com",
                scan_mode=ScanModeEnum.QUICK,
            ),
            lambda _run_id: "task-de-prueba",
        )
        cargo = await integration_session.scalar(
            select(CreditLedger.amount_delta).where(
                CreditLedger.organization_id == org.id,
                CreditLedger.reference_id == str(run.id),
                CreditLedger.reason == LedgerReasonEnum.SCAN_CONSUMPTION,
            )
        )
        assert cargo == Decimal("-7")
        limites = await limites_de(
            integration_session,
            organization_id=org.id,
            operation=operacion_de_scan_mode("QUICK"),
            default_max_budget_usd=settings.strix_max_budget_usd,
            default_max_turns=settings.strix_max_turns,
            scan_mode="QUICK",
        )
        assert limites.max_budget_usd == Decimal("4.25")
        assert limites.max_turns == 17
    finally:
        producto.scan_mode = modo_anterior
        producto.credits_required = creditos_anteriores
        producto.max_budget_usd = presupuesto_anterior
        producto.max_turns = turnos_anteriores
        await integration_session.commit()
