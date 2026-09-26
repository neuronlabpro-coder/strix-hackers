"""Base de datos CVE de referencia, compartida y de solo lectura."""

from backend.apps.cve_database.models import CVERecord, CVESeverityEnum

__all__ = ["CVERecord", "CVESeverityEnum"]
