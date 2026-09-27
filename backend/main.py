"""Aplicación FastAPI de control de Mind Guard Fenix Team."""

from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

from backend.apps.admin.router import router as admin_router
from backend.apps.api_access.router import router as api_access_router
from backend.apps.assets.router import router as assets_router
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
from backend.apps.support.router import admin_router as support_admin_router
from backend.apps.support.router import router as support_router
from backend.apps.vulnerabilities.router import router as vulnerabilities_router
from backend.apps.webhooks.router import router as webhooks_router
from backend.core.config import settings

app = FastAPI(title="Mind Guard Fenix Team API")

# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #
#
# ## Por qué hace falta y por qué no se notaba que faltaba
#
# En desarrollo no hace falta: el proxy de Vite sirve `/api` desde el mismo origen que la
# SPA, así que el navegador nunca ve un cruce y se puede tener la aplicación entera
# funcionando sin declarar ni un origen.
#
# En producción sí, y sin esto el panel se queda vacío: la SPA se sirve en `panel.` y la API
# responde en `api.`, que son orígenes distintos por definición, y el navegador bloquea
# cada petición **antes** de que salga. El síntoma es un panel que carga sin datos y un error
# de red que no menciona CORS, que es la forma más cara de perder un día de diagnóstico.
#
# ## Por qué `allow_credentials=False`
#
# La API **no** usa cookies: el JWT va en `Authorization` y en `sessionStorage`. Activar
# credenciales obligaría a declarar un origen exacto en vez de un comodín, y se aplicaría a
# un flujo que no existe. Mantenerlo desactivado significa que la cabecera `*` sigue siendo
# válida y que añadir un origen después no obliga a revisar las otras dos opciones.
#
# `allow_methods` y `allow_headers` se declaran en vez de usar los comodines porque
# `Authorization` y `X-Organization-Id` son cabeceras **no simples**: sin declararlas, el
# navegador hace una petición previa y las preflights empiezan a fallar en desarrollo, con
# un error que sí menciona CORS pero no la cabecera concreta.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "X-Organization-Id",
        "Stripe-Signature",
    ],
    # El máximo del navegador para un valor de preflight. Declararlo deja claro que el
    # valor es deliberado y no un 600 que alguien escribió una vez.
    max_age=600,
)


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
app.include_router(support_admin_router)
app.include_router(support_router)
app.include_router(audit_router)
app.include_router(knowledge_router)
app.include_router(onboarding_router)
app.include_router(billing_router)
app.include_router(cve_router)
app.include_router(webhooks_router)
app.include_router(assets_router)
