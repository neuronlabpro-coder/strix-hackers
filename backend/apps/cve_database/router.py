"""Endpoints de consulta del catálogo CVE."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.cve_database.models import CVERecord, CVESeverityEnum
from backend.apps.cve_database.schemas import CVEDetail, CVEPage, CVESummary, CVEYearsResponse
from backend.apps.cve_database.service import (
    available_years,
    build_search_filters,
    count_kev,
    fetch_record,
    fetch_trending_kev,
    normalize_cve_id,
)
from backend.core.database import get_db
from backend.core.middleware import TenantContext, get_current_tenant
from backend.core.rate_limit import enforce_cve_query_rate_limit

router = APIRouter(prefix="/api/v1/cve", tags=["cve"])
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]
QueryRateLimit = Depends(enforce_cve_query_rate_limit)

_MAX_YEAR = 2100
_MIN_YEAR = 1999


@router.get("/search", response_model=CVEPage, dependencies=[QueryRateLimit])
async def search_cve_records(
    _tenant: TenantDependency,
    session: SessionDependency,
    query: Annotated[str | None, Query(max_length=256)] = None,
    severity: CVESeverityEnum | None = None,
    is_kev_only: bool = False,
    year: Annotated[int | None, Query(ge=_MIN_YEAR, le=_MAX_YEAR)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> CVEPage:
    """Busca vulnerabilidades por identificador, palabra clave, severidad y año.

    R3 no aplica: el catálogo es público y no pertenece a ningún tenant. La ruta
    sí exige autenticación porque describe técnicas de explotación en detalle, y
    el rate limit evita que alguien lo use como buscador masivo.
    """

    filters = build_search_filters(
        query=query, severity=severity, is_kev_only=is_kev_only, year=year
    )
    total_result = await session.execute(
        select(func.count()).select_from(CVERecord).where(*filters)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(CVERecord)
        .where(*filters)
        .order_by(CVERecord.published_at.desc(), CVERecord.cve_id)
        .limit(limit)
        .offset(offset)
    )
    return CVEPage(
        items=[CVESummary.model_validate(record) for record in result.scalars().all()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/years", response_model=CVEYearsResponse, dependencies=[QueryRateLimit])
async def read_cve_years(
    _tenant: TenantDependency,
    session: SessionDependency,
) -> CVEYearsResponse:
    """Años disponibles para navegar, del más reciente al más antiguo."""

    return CVEYearsResponse(years=await available_years(session))


@router.get("/trending-kev", response_model=CVEPage, dependencies=[QueryRateLimit])
async def read_trending_kev(
    _tenant: TenantDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> CVEPage:
    """Vulnerabilidades con explotación activa confirmada por CISA KEV.

    Es la lista que de verdad importa: el resto del catálogo es histórico, esto
    es lo que un atacante está usando ahora mismo.
    """

    records = await fetch_trending_kev(session, limit=limit)
    # `total` es el número de KEV del catálogo, no el de la página devuelta: el
    # panel muestra "Njugate explotadas" y ese N no debe cambiar con el límite.
    return CVEPage(
        items=[CVESummary.model_validate(record) for record in records],
            total=await count_kev(session),
        limit=limit,
        offset=0,
    )


@router.get("/{cve_id}", response_model=CVEDetail, dependencies=[QueryRateLimit])
async def read_cve_record(
    _tenant: TenantDependency,
    session: SessionDependency,
    cve_id: str,
) -> CVEDetail:
    """Detalle de una vulnerabilidad concreta.

    El identificador se normaliza, de modo que `cve-2026-100599` y
    `CVE-2026-100599` devuelven la misma ficha. Un identificador con formato
    inválido devuelve `404` y no `422`: desde la interfaz es un enlace roto, no
    una petición mal formada.
    """

    try:
        normalized = normalize_cve_id(cve_id)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="CVE no encontrado"
        ) from error
    record = await fetch_record(session, normalized)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CVE no encontrado")
    return CVEDetail.model_validate(record)
