"""Aplicación FastAPI de control de Mind Guard Fenix Team."""

from fastapi import FastAPI

from backend.apps.dashboard.router import router as dashboard_router
from backend.apps.organizations.router import router as organizations_router
from backend.apps.pentests.router import router as pentests_router
from backend.apps.repositories.router import router as repositories_router
from backend.apps.repositories.router_auth import router as repositories_auth_router
from backend.apps.repositories.router_webhooks import router as repositories_webhooks_router
from backend.apps.vulnerabilities.router import router as vulnerabilities_router

app = FastAPI(title="Mind Guard Fenix Team API")
app.include_router(organizations_router)
app.include_router(pentests_router)
app.include_router(vulnerabilities_router)
app.include_router(repositories_webhooks_router)
app.include_router(repositories_auth_router)
app.include_router(repositories_router)
app.include_router(dashboard_router)
