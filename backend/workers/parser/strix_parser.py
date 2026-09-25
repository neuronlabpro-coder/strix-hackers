"""Parser estricto de los reportes JSON generados por Strix."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session

from backend.apps.vulnerabilities.models import IssueStatusEnum, Vulnerability
from backend.core.config import settings
from backend.workers.parser.normalizer import normalize_cvss_score, normalize_severity


class StrixOutputError(ValueError):
    """Error controlado para salida Strix inválida o corrupta."""


class IncompleteFindingError(StrixOutputError):
    """Error controlado para un hallazgo que no contiene evidencia obligatoria."""


def _redact_runtime_secrets(value: str) -> str:
    """Evita persistir la credencial LLM si un agente la incluye en evidencia."""

    secret = settings.llm_api_key.get_secret_value()
    return value.replace(secret, "[REDACTED]") if secret else value


def _required_text(
    finding: Mapping[str, object],
    key: str,
    *aliases: str,
    max_length: int | None = None,
) -> str:
    value: object = finding.get(key)
    if value is None:
        for alias in aliases:
            value = finding.get(alias)
            if value is not None:
                break
    if not isinstance(value, str) or not value.strip():
        raise IncompleteFindingError(f"El hallazgo no contiene el campo obligatorio: {key}")
    normalized = value.strip()
    if max_length is not None and len(normalized) > max_length:
        raise IncompleteFindingError(f"El campo {key} excede {max_length} caracteres")
    return _redact_runtime_secrets(normalized)


def _optional_text(
    finding: Mapping[str, object],
    key: str,
    *aliases: str,
    max_length: int | None = None,
) -> str | None:
    value: object = finding.get(key)
    if value is None:
        for alias in aliases:
            value = finding.get(alias)
            if value is not None:
                break
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise IncompleteFindingError(f"El campo opcional no es texto: {key}")
    normalized = value.strip()
    if max_length is not None and len(normalized) > max_length:
        raise IncompleteFindingError(f"El campo {key} excede {max_length} caracteres")
    return _redact_runtime_secrets(normalized)


def extract_strix_scan_id(json_data: Mapping[str, object] | str) -> str:
    """Extrae el identificador de provenance de un reporte Strix válido."""

    if (
        isinstance(json_data, str)
        and len(json_data.encode("utf-8")) > settings.strix_max_output_bytes
    ):
        raise StrixOutputError("El reporte de Strix supera el tamaño máximo permitido")
    try:
        decoded: object = json.loads(json_data) if isinstance(json_data, str) else json_data
    except json.JSONDecodeError as error:
        raise StrixOutputError("El JSON de Strix está corrupto") from error
    if not isinstance(decoded, dict):
        raise StrixOutputError("El reporte de Strix debe ser un objeto JSON")
    report = cast(dict[str, object], decoded)
    report_status = report.get("status")
    if not isinstance(report_status, str) or report_status.strip().lower() != "completed":
        raise StrixOutputError("El reporte de Strix debe tener status completed")
    return _required_text(report, "scan_id", max_length=128)


def parse_strix_output(
    json_data: Mapping[str, object] | str,
    organization_id: UUID,
    run_id: UUID,
    db: Session | None = None,
    expected_scan_id: str | None = None,
) -> list[Vulnerability]:
    """Convierte un reporte Strix en entidades sin abrir una transacción de persistencia.

    La sesión se acepta para mantener el contrato de ingesta; la persistencia y el
    commit pertenecen al worker que invoca el parser dentro de su transacción.
    """

    del db
    if (
        isinstance(json_data, str)
        and len(json_data.encode("utf-8")) > settings.strix_max_output_bytes
    ):
        raise StrixOutputError("El reporte de Strix supera el tamaño máximo permitido")
    try:
        decoded: object = json.loads(json_data) if isinstance(json_data, str) else json_data
    except json.JSONDecodeError as error:
        raise StrixOutputError("El JSON de Strix está corrupto") from error

    if not isinstance(decoded, dict):
        raise StrixOutputError("El reporte de Strix debe ser un objeto JSON")
    report = cast(dict[str, object], decoded)
    try:
        report_size = len(json.dumps(report, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as error:
        raise StrixOutputError("El reporte de Strix no es JSON serializable") from error
    if report_size > settings.strix_max_output_bytes:
        raise StrixOutputError("El reporte de Strix supera el tamaño máximo permitido")
    report_status = report.get("status")
    if not isinstance(report_status, str) or report_status.strip().lower() != "completed":
        raise StrixOutputError("El reporte de Strix debe tener status completed")
    scan_id = _required_text(report, "scan_id", max_length=128)
    if expected_scan_id is not None and scan_id != expected_scan_id:
        raise StrixOutputError("El scan_id del reporte no coincide con el run esperado")
    findings = report.get("findings")
    if not isinstance(findings, list):
        raise StrixOutputError("El reporte de Strix no contiene una lista de findings")
    if len(findings) > settings.strix_max_findings:
        raise StrixOutputError("El reporte de Strix supera el número máximo de findings")

    parsed_findings: list[Vulnerability] = []
    seen_finding_ids: set[str] = set()
    for index, raw_finding in enumerate(findings):
        if not isinstance(raw_finding, dict):
            raise IncompleteFindingError(f"El finding {index} no es un objeto")
        finding = cast(dict[str, object], raw_finding)
        try:
            finding_id = _required_text(finding, "id", max_length=128)
            if finding_id in seen_finding_ids:
                raise IncompleteFindingError(f"El finding {finding_id} está duplicado")
            seen_finding_ids.add(finding_id)
            parsed_findings.append(
                Vulnerability(
                    organization_id=organization_id,
                    run_id=run_id,
                    source_finding_id=finding_id,
                    title=_required_text(finding, "title", max_length=255),
                    description=_required_text(
                        finding,
                        "description",
                        max_length=settings.strix_max_description_chars,
                    ),
                    severity=normalize_severity(finding.get("severity")),
                    cvss_score=normalize_cvss_score(finding.get("cvss_score")),
                    cve_id=_optional_text(finding, "cve_id", "cve", max_length=64),
                    affected_target=_required_text(
                        finding, "affected_target", "target", max_length=512
                    ),
                    affected_line=_optional_text(
                        finding, "affected_line", "line", max_length=64
                    ),
                    poc_reproduction_raw=_required_text(
                        finding,
                        "poc_reproduction_raw",
                        "poc",
                        max_length=settings.strix_max_poc_chars,
                    ),
                    autofix_patch_diff=_optional_text(
                        finding,
                        "autofix_patch_diff",
                        "autofix",
                        max_length=settings.strix_max_autofix_chars,
                    ),
                    status=IssueStatusEnum.OPEN,
                )
            )
        except IncompleteFindingError:
            raise
        except ValueError as error:
            raise IncompleteFindingError(f"Finding {index} inválido: {error}") from error

    return parsed_findings
