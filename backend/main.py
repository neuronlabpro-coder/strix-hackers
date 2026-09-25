"""Aplicación FastAPI de control de Mind Guard Fenix Team."""

from fastapi import FastAPI

from backend.apps.organizations.router import router as organizations_router

app = FastAPI(title="Mind Guard Fenix Team API")
app.include_router(organizations_router)
