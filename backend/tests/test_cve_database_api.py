"""Pruebas del catálogo CVE: búsqueda, filtros, KEV y sincronización."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.cve_database.models import CVERecord, CVESeverityEnum
from backend.apps.cve_database.service import (
    CveSyncReport,
    normalize_cve_id,
    sync_cve_catalog,
)
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _tenant(session: AsyncSession) -> dict[str, str]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"CVE {suffix}", slug=f"cve-{suffix}")
    user = User(
        email=f"cve-{suffix}@example.com",
        hashed_password="not-used",
        full_name="CVE User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.MEMBER)
    )
    await session.commit()
    return {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


def _record(
    cve_id: str,
    *,
    severity: CVESeverityEnum = CVESeverityEnum.HIGH,
    cvss: str = "7.5",
    epss: str | None = "0.42",
    is_kev: bool = False,
    description: str = "Desbordamiento de búfer en el parser de imágenes",
    days_ago: int = 10,
) -> CVERecord:
    return CVERecord(
        cve_id=cve_id,
        severity=severity,
        cvss_score=Decimal(cvss),
        epss_score=Decimal(epss) if epss is not None else None,
        is_kev=is_kev,
        published_at=datetime.now(UTC) - timedelta(days=days_ago),
        description=description,
    )


# --------------------------------------------------------------------------- #
# Modelo y normalización
# --------------------------------------------------------------------------- #


def test_normalize_cve_id_uppercases_and_validates() -> None:
    assert normalize_cve_id("  cve-2026-900001 ") == "CVE-2026-900001"
    assert normalize_cve_id("CVE-1999-0001") == "CVE-1999-0001"

    for invalid in ("", "not-a-cve", "CVE-26-1", "CVE-2026-900099X", "CVE-ABCD-1234"):
        with pytest.raises(ValueError):
            normalize_cve_id(invalid)


def test_normalize_cve_id_rejects_pre_1999_years() -> None:
    """El CVE existe desde 1999; un año anterior indica un feed corrupto."""

    with pytest.raises(ValueError):
        normalize_cve_id("CVE-1998-0001")


@pytest.mark.asyncio
async def test_cve_id_is_the_primary_key(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    from sqlalchemy.exc import IntegrityError

    integration_session.add_all([_record("CVE-2026-000001"), _record("CVE-2026-000001")])
    with pytest.raises(IntegrityError):
        await integration_session.flush()
    await integration_session.rollback()


# --------------------------------------------------------------------------- #
# Búsqueda
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_filters_by_query_severity_year_and_kev(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    headers = await _tenant(integration_session)
    integration_session.add_all(
        [
            _record(
                "CVE-2026-900001",
                severity=CVESeverityEnum.CRITICAL,
                cvss="9.8",
                is_kev=True,
                description="Mitigación ausente de DNS rebinding en el balanceador",
                days_ago=1,
            ),
            _record(
                "CVE-2025-900001",
                severity=CVESeverityEnum.MEDIUM,
                cvss="5.3",
                description="Desbordamiento de búfer en el parser",
                days_ago=200,
            ),
            _record(
                "CVE-2024-900001",
                severity=CVESeverityEnum.LOW,
                cvss="3.1",
                description="Filtración de información en respuestas de error",
                days_ago=400,
            ),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        everything = await client.get("/api/v1/cve/search", headers=headers)
        by_id = await client.get("/api/v1/cve/search?query=CVE-2026-900001", headers=headers)
        by_keyword = await client.get("/api/v1/cve/search?query=rebinding", headers=headers)
        by_severity = await client.get(
            "/api/v1/cve/search?severity=CRITICAL", headers=headers
        )
        by_year = await client.get("/api/v1/cve/search?year=2024", headers=headers)
        kev_only = await client.get("/api/v1/cve/search?is_kev_only=true", headers=headers)
        combined = await client.get(
            "/api/v1/cve/search?year=2026&severity=CRITICAL&is_kev_only=true",
            headers=headers,
        )
        miss = await client.get("/api/v1/cve/search?query=inexistente-xyz", headers=headers)

    assert everything.status_code == 200
    assert everything.json()["total"] >= 3
    # El catálogo viene sembrado por la migración, así que las aserciones son
    # sobre la presencia o ausencia del registro de la prueba y no sobre totales
    # absolutos: un total exacto describiría el seed, no el comportamiento.
    assert by_id.json()["total"] == 1
    assert by_id.json()["items"][0]["cve_id"] == "CVE-2026-900001"
    # La búsqueda por palabra clave ignora acentos y mayúsculas.
    assert "CVE-2026-900001" in {item["cve_id"] for item in by_keyword.json()["items"]}
    critical_ids = {item["cve_id"] for item in by_severity.json()["items"]}
    assert "CVE-2026-900001" in critical_ids
    assert "CVE-2025-900001" not in critical_ids
    assert by_year.json()["items"][0]["cve_id"] == "CVE-2024-900001"
    kev_ids = {item["cve_id"] for item in kev_only.json()["items"]}
    assert "CVE-2026-900001" in kev_ids
    assert "CVE-2025-900001" not in kev_ids
    combined_ids = {item["cve_id"] for item in combined.json()["items"]}
    assert "CVE-2026-900001" in combined_ids
    assert "CVE-2024-900001" not in combined_ids
    assert miss.json()["total"] == 0


@pytest.mark.asyncio
async def test_search_escapes_like_wildcards(integration_session: AsyncSession) -> None:
    """Un `_` en la búsqueda se busca literal, no como comodín de LIKE."""

    assert integration_session is not None
    headers = await _tenant(integration_session)
    integration_session.add_all(
        [
            _record("CVE-2026-900010", description="Registro con guion bajo literal"),
            _record("CVE-2026-900011", description="Registro de control"),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Sin escapar, `%` matchearía todo el catálogo y `9000_1` también.
        percent = await client.get("/api/v1/cve/search?query=%25", headers=headers)
        underscore = await client.get("/api/v1/cve/search?query=9000_1", headers=headers)
        exact = await client.get("/api/v1/cve/search?query=900010", headers=headers)

    assert percent.json()["total"] == 0
    assert underscore.json()["total"] == 0
    assert exact.json()["total"] == 1
    assert exact.json()["items"][0]["cve_id"] == "CVE-2026-900010"


@pytest.mark.asyncio
async def test_search_is_paginated_and_sorted_by_published_desc(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    headers = await _tenant(integration_session)
    for index in range(5):
        integration_session.add(
            _record(
                f"CVE-2026-9{index:05d}",
                description=f"Registro de prueba {index}",
                days_ago=index,
            )
        )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get(
            "/api/v1/cve/search?limit=2&offset=0&year=2026", headers=headers
        )
        second = await client.get(
            "/api/v1/cve/search?limit=2&offset=2&year=2026", headers=headers
        )

    assert page.json()["limit"] == 2
    assert page.json()["offset"] == 0
    assert len(page.json()["items"]) == 2
    assert second.json()["offset"] == 2
    dates = [item["published_at"] for item in page.json()["items"]]
    assert dates == sorted(dates, reverse=True)
    first_ids = {item["cve_id"] for item in page.json()["items"]}
    second_ids = {item["cve_id"] for item in second.json()["items"]}
    assert first_ids.isdisjoint(second_ids)


@pytest.mark.asyncio
async def test_cve_detail_and_trending_kev(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    headers = await _tenant(integration_session)
    integration_session.add_all(
        [
            _record(
                "CVE-2026-900001",
                severity=CVESeverityEnum.CRITICAL,
                cvss="9.8",
                is_kev=True,
                days_ago=1,
            ),
            _record("CVE-2026-900002", is_kev=True, days_ago=5),
            _record("CVE-2026-900003", is_kev=False, days_ago=1),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get("/api/v1/cve/CVE-2026-900001", headers=headers)
        lowercase = await client.get("/api/v1/cve/cve-2026-900001", headers=headers)
        missing = await client.get("/api/v1/cve/CVE-1999-0000", headers=headers)
        trending = await client.get("/api/v1/cve/trending-kev", headers=headers)
        trending_limited = await client.get("/api/v1/cve/trending-kev?limit=1", headers=headers)

    assert detail.status_code == 200
    payload = detail.json()
    assert payload["cve_id"] == "CVE-2026-900001"
    assert payload["severity"] == "CRITICAL"
    assert payload["is_kev"] is True
    assert payload["cvss_score"] == "9.80"
    assert payload["epss_score"] == "0.42000"
    # El identificador se normaliza también en la ruta.
    assert lowercase.json()["cve_id"] == "CVE-2026-900001"
    assert missing.status_code == 404
    assert trending.status_code == 200
    # El seed también aporta KEVs, así que se comprueba contención y no total.
    trending_ids = {item["cve_id"] for item in trending.json()["items"]}
    assert {"CVE-2026-900001", "CVE-2026-900002"} <= trending_ids
    assert "CVE-2026-900003" not in trending_ids
    assert all(item["is_kev"] is True for item in trending.json()["items"])
    # El límite se aplica a los elementos devueltos, no al universo del catálogo.
    assert trending_limited.json()["total"] == trending.json()["total"]
    assert len(trending_limited.json()["items"]) == 1
    # Ordenado por publicación descendente: el más reciente va primero.
    dates = [item["published_at"] for item in trending.json()["items"]]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.asyncio
async def test_cve_years_available_covers_published_records(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    headers = await _tenant(integration_session)
    integration_session.add_all(
        [
            _record("CVE-2026-900001", days_ago=1),
            _record("CVE-2024-900001", days_ago=500),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/cve/years", headers=headers)

    assert response.status_code == 200
    years = response.json()["years"]
    assert 2026 in years
    assert 2024 in years
    assert years == sorted(years, reverse=True)


@pytest.mark.asyncio
async def test_cve_endpoints_require_authentication() -> None:
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        search = await client.get("/api/v1/cve/search")
        detail = await client.get("/api/v1/cve/CVE-2026-900001")
        trending = await client.get("/api/v1/cve/trending-kev")
        years = await client.get("/api/v1/cve/years")

    for response in (search, detail, trending, years):
        assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Sincronización
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sync_is_incremental_and_idempotent(
    integration_session: AsyncSession,
) -> None:
    """La sincronización solo inserta lo que falta y pisa los datos que cambian."""

    assert integration_session is not None
    before = datetime.now(UTC) - timedelta(days=2)
    # Las fechas se calculan desde `now()` en lugar de fijarse: una fecha literal
    # envejecería con el tiempo y empezaría a caer en la ventana de incremental.
    recent_one_day = datetime.now(UTC) - timedelta(days=1)
    recent_two_days = datetime.now(UTC) - timedelta(days=1, hours=12)
    feed = [
        {
            "cve_id": "cve-2026-900001",
            "severity": "CRITICAL",
            "cvss_score": 9.8,
            "epss_score": 0.75,
            "is_kev": True,
            "published_at": recent_two_days.isoformat(),
            "description": "Mitigación ausente de DNS rebinding en el balanceador",
        },
        {
            "cve_id": "CVE-2026-900002",
            "severity": "HIGH",
            "cvss_score": 7.5,
            "epss_score": None,
            "is_kev": False,
            "published_at": recent_one_day.isoformat(),
            "description": "Desbordamiento de búfer",
        },
        {
            "cve_id": "NO-VALIDO",
            "severity": "HIGH",
            "cvss_score": 7.0,
            "description": "Registro corrupto que debe descartarse",
        },
    ]

    first: CveSyncReport = await sync_cve_catalog(
        integration_session, feed, since=before
    )
    assert first.inserted == 2
    assert first.skipped_invalid == 1
    assert first.updated == 0

    second = await sync_cve_catalog(integration_session, feed, since=before)
    assert second.inserted == 0
    assert second.updated == 0

    # La resincronización no duplica: el feed se reinyere por completo y cada
    # identificador sigue teniendo exactamente una fila.
    total = (
        await integration_session.execute(
            select(func.count(CVERecord.cve_id)).where(
                CVERecord.cve_id.in_(["CVE-2026-900001", "CVE-2026-900002"])
            )
        )
    ).scalar_one()
    assert int(total) == 2


@pytest.mark.asyncio
async def test_sync_updates_existing_records(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    integration_session.add(
        _record("CVE-2026-900001", cvss="5.0", is_kev=False, days_ago=1)
    )
    await integration_session.commit()

    report = await sync_cve_catalog(
        integration_session,
        [
            {
                "cve_id": "CVE-2026-900001",
                "severity": "CRITICAL",
                "cvss_score": 9.8,
                "epss_score": 0.9,
                "is_kev": True,
                "published_at": "2026-09-20T10:00:00+00:00",
                "description": "Descripción corregida",
            }
        ],
        since=None,
    )

    assert report.inserted == 0
    assert report.updated == 1
    stored = (
        await integration_session.execute(
            select(CVERecord).where(CVERecord.cve_id == "CVE-2026-900001")
        )
    ).scalar_one()
    assert stored.severity == CVESeverityEnum.CRITICAL
    assert stored.cvss_score == Decimal("9.8")
    assert stored.is_kev is True


@pytest.mark.asyncio
async def test_sync_rejects_out_of_range_cvss_and_unknown_severity(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    report = await sync_cve_catalog(
        integration_session,
        [
            {
                "cve_id": "CVE-2026-900003",
                "severity": "CRITICAL",
                "cvss_score": 12.5,
                "description": "CVSS fuera de rango",
            },
            {
                "cve_id": "CVE-2026-900004",
                "severity": "APOCALIPTICA",
                "cvss_score": 5.0,
                "description": "Severidad desconocida",
            },
        ],
        since=None,
    )

    assert report.inserted == 0
    assert report.skipped_invalid == 2


@pytest.mark.asyncio
async def test_sync_ignores_records_older_than_since(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    report = await sync_cve_catalog(
        integration_session,
        [
            {
                "cve_id": "CVE-2020-900001",
                "severity": "MEDIUM",
                "cvss_score": 5.0,
                "description": "Antigua",
                "published_at": "2020-01-01T00:00:00+00:00",
            },
            {
                "cve_id": "CVE-2026-900005",
                "severity": "MEDIUM",
                "cvss_score": 5.0,
                "description": "Reciente",
                "published_at": "2026-09-25T00:00:00+00:00",
            },
        ],
        since=datetime.now(UTC) - timedelta(days=30),
    )

    assert report.inserted == 1
    assert report.skipped_older == 1
