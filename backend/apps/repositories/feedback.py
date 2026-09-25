"""Formato de feedback de seguridad publicado en Pull/Merge Requests."""

from __future__ import annotations

from collections.abc import Iterable

from backend.apps.vulnerabilities.models import SeverityEnum, Vulnerability


def _inline_text(value: str) -> str:
    return " ".join(value.replace("`", "'").split())


def _safe_code_block(value: str) -> str:
    return value.replace("```", "```\\`")


def build_pr_comment_markdown(
    findings: Iterable[Vulnerability],
    dashboard_url: str,
) -> str:
    """Construye un comentario determinista sin interpolar HTML ni código ejecutable."""

    ordered_findings = sorted(
        findings,
        key=lambda finding: (
            0 if finding.severity == SeverityEnum.CRITICAL else 1,
            0 if finding.severity == SeverityEnum.HIGH else 1,
            finding.title.casefold(),
        ),
    )
    critical_count = sum(
        finding.severity == SeverityEnum.CRITICAL for finding in ordered_findings
    )
    high_count = sum(finding.severity == SeverityEnum.HIGH for finding in ordered_findings)
    if critical_count == 0 and high_count == 0:
        return "\n".join(
            [
                "## 🦉 Fenix Security Review",
                "",
                "No se detectaron vulnerabilidades CRITICAL ni HIGH en los archivos modificados.",
                "",
                "Solicita una nueva revisión desde el panel o mediante el comando "
                "de ChatOps configurado.",
            ]
        )
    lines = [
        "## 🦉 Fenix Security Review",
        "",
        (
            f"Se detectaron **{critical_count} CRITICAL** y **{high_count} HIGH**. "
            "El merge está bloqueado hasta corregir los hallazgos."
        ),
        "",
    ]
    for finding in ordered_findings:
        lines.extend(
            [
                f"### {finding.severity.value}: {_inline_text(finding.title)}",
                f"- **Ubicación:** `{_inline_text(finding.affected_target)}`",
                f"- **CVSS:** `{finding.cvss_score:.1f}`",
                "",
                "#### Proof of Concept",
                "```text",
                _safe_code_block(finding.poc_reproduction_raw),
                "```",
            ]
        )
        if finding.autofix_patch_diff:
            lines.extend(
                [
                    "",
                    "#### Suggested Remediation",
                    "```diff",
                    _safe_code_block(finding.autofix_patch_diff),
                    "```",
                ]
            )
        if finding.id is not None:
            lines.extend(["", f"[Abrir hallazgo en el panel]({dashboard_url}/{finding.id})"])
        lines.extend(["", "---", ""])
    lines.extend(
        [
            "Solicita una nueva revisión desde el panel o mediante el comando "
            "de ChatOps configurado.",
        ]
    )
    return "\n".join(lines)
