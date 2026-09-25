"""Normalización de severidades y puntuaciones CVSS."""

from backend.apps.vulnerabilities.models import SeverityEnum

_SEVERITY_ALIASES = {
    "critical": SeverityEnum.CRITICAL,
    "crit": SeverityEnum.CRITICAL,
    "high": SeverityEnum.HIGH,
    "medium": SeverityEnum.MEDIUM,
    "med": SeverityEnum.MEDIUM,
    "low": SeverityEnum.LOW,
    "info": SeverityEnum.INFO,
    "informational": SeverityEnum.INFO,
}


def normalize_severity(value: object) -> SeverityEnum:
    """Convierte severidades de Strix a valores persistidos canónicos."""

    if isinstance(value, str):
        normalized = _SEVERITY_ALIASES.get(value.strip().lower())
        if normalized is not None:
            return normalized
    raise ValueError("Severidad no soportada")


def normalize_cvss_score(value: object) -> float:
    """Valida y normaliza una puntuación CVSS v3 en el rango 0-10."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("cvss_score debe ser numérico")
    score = float(value)
    if not 0 <= score <= 10:
        raise ValueError("cvss_score debe estar entre 0 y 10")
    return score
