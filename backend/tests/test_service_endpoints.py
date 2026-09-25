"""Pruebas de los endpoints raíz y de salud de la API."""

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_service_info_at_root() -> None:
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": "Mind Guard Fenix Team API",
        "status": "online",
        "docs": "/docs",
    }


@pytest.mark.asyncio
async def test_health_endpoint_reports_healthy() -> None:
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


@pytest.mark.asyncio
async def test_service_endpoints_do_not_require_authentication() -> None:
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        health = await client.get("/health")

    assert root.status_code == 200
    assert health.status_code == 200
