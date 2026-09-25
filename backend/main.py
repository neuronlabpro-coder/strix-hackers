"""Aplicación FastAPI de control de Mind Guard Fenix Team."""

from fastapi import FastAPI

from backend.apps.organizations.router import router as organizations_router
from backend.apps.pentests.router import router as pentests_router
from backend.apps.vulnerabilities.router import router as vulnerabilities_router

app = FastAPI(title="Mind Guard Fenix Team API")
app.include_router(organizations_router)
app.include_router(pentests_router)
app.include_router(vulnerabilities_router)
