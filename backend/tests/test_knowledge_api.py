"""Pruebas del catálogo técnico de remediación (OWASP/CWE) sembrado por migración."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.knowledge.models import KnowledgeCategoryEnum, KnowledgeEntry
from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

_EXPECTED_SEED_CODES = {
    "CWE-89",
    "CWE-79",
    "CWE-798",
    "CWE-22",
    "CWE-502",
    "CWE-918",
    "CWE-287",
    "CWE-327",
    "CWE-1104",
    "CWE-841",
}


async def _tenant(session: AsyncSession) -> tuple[Organization, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Knowledge {suffix}", slug=f"know-{suffix}")
    user = User(
        email=f"know-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Knowledge User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.MEMBER)
    )
    await session.commit()
    return organization, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


@pytest.mark.asyncio
async def test_migration_seeds_the_owasp_and_cwe_catalog(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    codes = set(
        (
            await integration_session.execute(select(KnowledgeEntry.reference_code))
        )
        .scalars()
        .all()
    )
    assert _EXPECTED_SEED_CODES <= codes

    incomplete = (
        await integration_session.execute(
            select(func.count(KnowledgeEntry.id)).where(
                (KnowledgeEntry.vulnerable_example == "")
                | (KnowledgeEntry.secure_example == "")
                | (KnowledgeEntry.mitigation == "")
                | (KnowledgeEntry.risk_summary == "")
            )
        )
    ).scalar_one()
    assert int(incomplete) == 0

    duplicated = (
        await integration_session.execute(
            select(func.count(KnowledgeEntry.id)).where(
                KnowledgeEntry.vulnerable_example == KnowledgeEntry.secure_example
            )
        )
    ).scalar_one()
    assert int(duplicated) == 0


@pytest.mark.asyncio
async def test_knowledge_catalog_is_searchable_and_filterable(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _organization, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        all_entries = await client.get(
            "/api/v1/knowledge/?limit=100", headers=headers
        )
        by_search = await client.get("/api/v1/knowledge/?search=sql", headers=headers)
        by_code = await client.get("/api/v1/knowledge/?search=cwe-89", headers=headers)
        by_category = await client.get(
            "/api/v1/knowledge/?category=SSRF", headers=headers
        )
        by_severity = await client.get(
            "/api/v1/knowledge/?severity=CRITICAL", headers=headers
        )
        miss = await client.get("/api/v1/knowledge/?search=zzz-no-existe", headers=headers)

    assert all_entries.status_code == 200
    payload = all_entries.json()
    assert payload["total"] >= len(_EXPECTED_SEED_CODES)
    assert all(
        {"reference_code", "title", "category", "severity", "risk_summary", "owasp_category"}
        <= set(item)
        for item in payload["items"]
    )

    assert by_search.json()["total"] >= 1
    assert "CWE-89" in {item["reference_code"] for item in by_search.json()["items"]}
    assert by_code.json()["total"] == 1
    assert by_code.json()["items"][0]["reference_code"] == "CWE-89"
    assert by_category.json()["total"] == 1
    assert by_category.json()["items"][0]["category"] == KnowledgeCategoryEnum.SSRF.value
    assert by_severity.json()["total"] >= 1
    assert all(
        item["severity"] == "CRITICAL" for item in by_severity.json()["items"]
    )
    assert miss.json()["total"] == 0


@pytest.mark.asyncio
async def test_knowledge_detail_exposes_remediation_examples(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _organization, headers = await _tenant(integration_session)
    entry = (
        await integration_session.execute(
            select(KnowledgeEntry).where(KnowledgeEntry.reference_code == "CWE-89")
        )
    ).scalar_one()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/knowledge/{entry.id}", headers=headers)
        missing = await client.get(f"/api/v1/knowledge/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 200
    detail = response.json()
    assert detail["reference_code"] == "CWE-89"
    assert detail["vulnerable_example"]
    assert detail["secure_example"]
    assert detail["mitigation"]
    assert detail["owasp_category"].startswith("A03")
    assert detail["vulnerable_example"] != detail["secure_example"]
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_knowledge_catalog_is_read_only_over_http(
    integration_session: AsyncSession,
) -> None:
    """El catálogo es de solo lectura: la API no ofrece ninguna vía de escritura."""

    assert integration_session is not None
    _organization, headers = await _tenant(integration_session)
    entry = (
        await integration_session.execute(
            select(KnowledgeEntry).where(KnowledgeEntry.reference_code == "CWE-502")
        )
    ).scalar_one()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/v1/knowledge/",
            json={"reference_code": "CWE-1", "title": "Inventado"},
            headers=headers,
        )
        update = await client.patch(
            f"/api/v1/knowledge/{entry.id}",
            json={"title": "Alterado"},
            headers=headers,
        )
        delete = await client.delete(f"/api/v1/knowledge/{entry.id}", headers=headers)

    assert create.status_code == 405
    assert update.status_code == 405
    assert delete.status_code == 405

    stored = (
        await integration_session.execute(
            select(KnowledgeEntry).where(KnowledgeEntry.id == entry.id)
        )
    ).scalar_one()
    assert stored.title == "Deserializacion insegura"


@pytest.mark.asyncio
async def test_knowledge_requires_authentication() -> None:
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        catalog = await client.get("/api/v1/knowledge/")

    assert catalog.status_code == 401
