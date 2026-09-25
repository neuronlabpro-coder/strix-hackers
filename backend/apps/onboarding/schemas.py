"""Esquemas del checklist de onboarding."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class OnboardingStepKeyEnum(StrEnum):
    """Claves estables de los pasos, independientes del idioma de la interfaz."""

    CONNECT_GIT = "connect_git"
    IMPORT_REPOSITORY = "import_repository"
    RUN_FIRST_SCAN = "run_first_scan"


class OnboardingStep(BaseModel):
    """Un paso del checklist con su Completion derivado de la base de datos."""

    key: OnboardingStepKeyEnum
    completed: bool


class OnboardingResponse(BaseModel):
    """Progreso del checklist de configuración inicial."""

    steps: list[OnboardingStep]
    completed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=1)
    is_complete: bool
