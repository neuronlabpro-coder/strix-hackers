"""Resolución de acuerdos y features con aislamiento por organización."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.commercial.models import EnterpriseAgreement, PlanFeatureEntitlement
from backend.apps.organizations.models import Organization


def resolve_feature_value(*, override: bool | None, plan_enabled: bool | None) -> bool:
    if override is not None:
        return override
    if plan_enabled is not None:
        return plan_enabled
    return False


async def effective_agreement(
    session: AsyncSession, organization_id: uuid.UUID, *, now: datetime | None = None
) -> EnterpriseAgreement | None:
    momento = now or datetime.now(UTC)
    resultado = await session.execute(
        select(EnterpriseAgreement)
        .where(
            EnterpriseAgreement.organization_id == organization_id,
            EnterpriseAgreement.valid_from <= momento,
            or_(
                EnterpriseAgreement.valid_until.is_(None), EnterpriseAgreement.valid_until > momento
            ),
        )
        .order_by(EnterpriseAgreement.valid_from.desc(), EnterpriseAgreement.created_at.desc())
        .limit(1)
    )
    return resultado.scalar_one_or_none()


async def features_for_organization(
    session: AsyncSession, organization_id: uuid.UUID
) -> dict[str, bool] | None:
    tier = await session.scalar(
        select(Organization.plan_tier).where(Organization.id == organization_id)
    )
    if tier is None:
        return None
    filas = await session.execute(
        select(PlanFeatureEntitlement).where(PlanFeatureEntitlement.plan_tier == tier.value)
    )
    del_plan = {fila.feature_key: fila.enabled for fila in filas.scalars()}
    acuerdo = await effective_agreement(session, organization_id)
    overrides = acuerdo.features if acuerdo is not None else {}
    return {
        feature: resolve_feature_value(
            override=overrides.get(feature), plan_enabled=del_plan.get(feature)
        )
        for feature in del_plan.keys() | overrides.keys()
    }


async def feature_enabled(
    session: AsyncSession, organization_id: uuid.UUID, feature_key: str
) -> bool:
    features = await features_for_organization(session, organization_id)
    return features is not None and features.get(feature_key, False)
