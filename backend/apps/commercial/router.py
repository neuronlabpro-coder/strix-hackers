"""Catálogo comercial, acuerdos por tenant y entitlements efectivos."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.admin.dependencies import SuperuserDependency
from backend.apps.commercial.models import EnterpriseAgreement, PentestProduct
from backend.apps.commercial.schemas import (
    EnterpriseAgreementCreate,
    EnterpriseAgreementResponse,
    PentestProductCreate,
    PentestProductResponse,
    PentestProductUpdate,
)
from backend.apps.commercial.service import features_for_organization
from backend.apps.organizations.models import Organization
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant

SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]
admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
public_router = APIRouter(prefix="/api/v1", tags=["commercial"])


@public_router.get("/pentest-products", response_model=list[PentestProductResponse])
async def public_products(session: SessionDependency) -> list[PentestProductResponse]:
    result = await session.execute(
        select(PentestProduct)
        .where(PentestProduct.is_active.is_(True))
        .order_by(PentestProduct.slug)
    )
    return [PentestProductResponse.model_validate(row) for row in result.scalars()]


@admin_router.get("/pentest-products", response_model=list[PentestProductResponse])
async def admin_products(
    _superuser: SuperuserDependency, session: SessionDependency
) -> list[PentestProductResponse]:
    result = await session.execute(select(PentestProduct).order_by(PentestProduct.slug))
    return [PentestProductResponse.model_validate(row) for row in result.scalars()]


@admin_router.post("/pentest-products", response_model=PentestProductResponse, status_code=201)
async def create_product(
    payload: PentestProductCreate, _superuser: SuperuserDependency, session: SessionDependency
) -> PentestProductResponse:
    if await session.scalar(select(PentestProduct.id).where(PentestProduct.slug == payload.slug)):
        raise HTTPException(status_code=409, detail="Producto ya existente")
    if payload.scan_mode is not None and await session.scalar(
        select(PentestProduct.id).where(PentestProduct.scan_mode == payload.scan_mode)
    ):
        raise HTTPException(status_code=409, detail="Modo de escaneo ya asignado")
    row = PentestProduct(**payload.model_dump(exclude_unset=True))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return PentestProductResponse.model_validate(row)


@admin_router.patch("/pentest-products/{slug}", response_model=PentestProductResponse)
async def update_product(
    slug: str,
    payload: PentestProductUpdate,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> PentestProductResponse:
    row = await session.scalar(select(PentestProduct).where(PentestProduct.slug == slug))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Envía un campo para modificar")
    if changes.get("scan_mode") is not None and await session.scalar(
        select(PentestProduct.id).where(
            PentestProduct.scan_mode == changes["scan_mode"], PentestProduct.id != row.id
        )
    ):
        raise HTTPException(status_code=409, detail="Modo de escaneo ya asignado")
    for field in (
        "name_es",
        "name_en",
        "description_es",
        "description_en",
        "price_label_es",
        "price_label_en",
        "features",
        "limits",
        "is_active",
    ):
        if field in changes and changes[field] is None:
            raise HTTPException(status_code=422, detail="El campo no admite null")
    for field, value in changes.items():
        setattr(row, field, value)
    if row.price_min_usd is not None and row.price_max_usd is not None:
        if row.price_max_usd < row.price_min_usd:
            raise HTTPException(status_code=422, detail="Rango de precio incoherente")
    await session.commit()
    await session.refresh(row)
    return PentestProductResponse.model_validate(row)


@admin_router.get(
    "/organizations/{organization_id}/enterprise-agreements",
    response_model=list[EnterpriseAgreementResponse],
)
async def list_agreements(
    organization_id: UUID, _superuser: SuperuserDependency, session: SessionDependency
) -> list[EnterpriseAgreementResponse]:
    result = await session.execute(
        select(EnterpriseAgreement)
        .where(EnterpriseAgreement.organization_id == organization_id)
        .order_by(EnterpriseAgreement.valid_from.desc())
    )
    return [EnterpriseAgreementResponse.model_validate(row) for row in result.scalars()]


@admin_router.post(
    "/organizations/{organization_id}/enterprise-agreements",
    response_model=EnterpriseAgreementResponse,
    status_code=201,
)
async def create_agreement(
    organization_id: UUID,
    payload: EnterpriseAgreementCreate,
    superuser: SuperuserDependency,
    session: SessionDependency,
) -> EnterpriseAgreementResponse:
    if (
        await session.scalar(select(Organization.id).where(Organization.id == organization_id))
        is None
    ):
        raise HTTPException(status_code=404, detail="Organización no encontrada")
    desde = payload.valid_from or datetime.now(UTC)
    if payload.valid_until is not None and payload.valid_until <= desde:
        raise HTTPException(status_code=422, detail="Vigencia incoherente")
    row = EnterpriseAgreement(
        **payload.model_dump(exclude={"valid_from"}),
        organization_id=organization_id,
        valid_from=desde,
        created_by=superuser.id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return EnterpriseAgreementResponse.model_validate(row)


@public_router.get("/features/me", response_model=dict[str, bool])
async def my_features(tenant: TenantDependency, session: SessionDependency) -> dict[str, bool]:
    return await features_for_organization(session, tenant.organization.id) or {}
