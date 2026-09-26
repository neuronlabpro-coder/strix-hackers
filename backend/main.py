"""Aplicación FastAPI de control de Mind Guard Fenix Team."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from backend.apps.admin.router import router as admin_router
from backend.apps.api_access.router import router as api_access_router
from backend.apps.audit.router import router as audit_router
from backend.apps.billing.router import router as billing_router
from backend.apps.cve_database.router import router as cve_router
from backend.apps.dashboard.router import router as dashboard_router
from backend.apps.knowledge.router import router as knowledge_router
from backend.apps.onboarding.router import router as onboarding_router
from backend.apps.organizations.router import router as organizations_router
from backend.apps.pentests.router import router as pentests_router
from backend.apps.repositories.router import router as repositories_router
from backend.apps.repositories.router_auth import router as repositories_auth_router
from backend.apps.repositories.router_webhooks import router as repositories_webhooks_router
from backend.apps.vulnerabilities.router import router as vulnerabilities_router
from backend.apps.webhooks.router import router as webhooks_router

app = FastAPI(title="Mind Guard Fenix Team API")


class ServiceInfoResponse(BaseModel):
    """Identidad del servicio para quien consulta la raíz de la API."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Mind Guard Fenix Team API",
                    "status": "online",
                    "docs": "/docs",
                }
            ]
        }
    )

    name: str
    status: Literal["online"] = "online"
    docs: str = "/docs"


class HealthResponse(BaseModel):
    """Resultado del sondeo de salud del proceso."""

    status: Literal["healthy"] = "healthy"


@app.get("/", response_model=ServiceInfoResponse, tags=["system"])
async def read_service_info() -> ServiceInfoResponse:
    """Evita el `404` en la raíz y descubre la documentación de la API."""

    return ServiceInfoResponse(name="Mind Guard Fenix Team API", status="online", docs="/docs")


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def read_health() -> HealthResponse:
    """Sondeo de vida del proceso, pensado para orquestadores y balanceadores."""

    return HealthResponse(status="healthy")


app.include_router(organizations_router)
app.include_router(pentests_router)
app.include_router(vulnerabilities_router)
app.include_router(repositories_webhooks_router)
app.include_router(repositories_auth_router)
app.include_router(repositories_router)
app.include_router(dashboard_router)
app.include_router(api_access_router)
app.include_router(admin_router)
app.include_router(audit_router)
app.include_router(knowledge_router)
app.include_router(onboarding_router)
app.include_router(billing_router)
app.include_router(cve_router)
app.include_router(webhooks_router)
