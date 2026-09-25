"""Rastro de auditoría append-only de la plataforma."""

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry

__all__ = ["AuditActionEnum", "AuditLogEntry"]
