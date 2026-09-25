import json
import uuid

import pytest

from backend.apps.vulnerabilities.models import SeverityEnum, Vulnerability
from backend.workers.parser.strix_parser import (
    IncompleteFindingError,
    StrixOutputError,
    parse_strix_output,
)


def test_parser_normalizes_synthetic_strix_findings() -> None:
    organization_id = uuid.uuid4()
    run_id = uuid.uuid4()
    report = {
        "scan_id": "scan-test-001",
        "status": "completed",
        "findings": [
            {
                "id": "finding-001",
                "title": "SQL Injection",
                "description": "User input is concatenated into a query.",
                "severity": "critical",
                "cvss_score": 9.8,
                "cve": "CVE-2024-0001",
                "target": "/api/login",
                "line": "42",
                "poc": "curl -X POST https://target/api/login",
                "autofix": "--- auth.py\n+++ auth.py",
            }
        ],
    }

    findings = parse_strix_output(
        json.dumps(report),
        organization_id=organization_id,
        run_id=run_id,
        db=None,
    )

    assert len(findings) == 1
    finding = findings[0]
    assert isinstance(finding, Vulnerability)
    assert finding.organization_id == organization_id
    assert finding.run_id == run_id
    assert finding.source_finding_id == "finding-001"
    assert finding.severity is SeverityEnum.CRITICAL
    assert finding.cvss_score == 9.8
    assert finding.cve_id == "CVE-2024-0001"
    assert finding.poc_reproduction_raw.startswith("curl")


def test_parser_rejects_corrupt_json() -> None:
    with pytest.raises(StrixOutputError):
        parse_strix_output(
            "{not-json",
            organization_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            db=None,
        )


def test_parser_rejects_report_without_completed_status() -> None:
    report = {
        "scan_id": "scan-failed",
        "status": "failed",
        "findings": [],
    }

    with pytest.raises(StrixOutputError, match="completed"):
        parse_strix_output(
            report,
            organization_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            db=None,
        )


def test_parser_rejects_scan_id_mismatch_when_run_provenance_is_known() -> None:
    report = {
        "scan_id": "scan-from-run",
        "status": "completed",
        "findings": [],
    }

    with pytest.raises(StrixOutputError, match="scan_id"):
        parse_strix_output(
            report,
            organization_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            db=None,
            expected_scan_id="different-scan",
        )


def test_parser_rejects_duplicate_external_finding_ids() -> None:
    report = {
        "scan_id": "scan-duplicate",
        "status": "completed",
        "findings": [
            {
                "id": "finding-001",
                "title": "First",
                "description": "First finding.",
                "severity": "HIGH",
                "cvss_score": 7.5,
                "target": "/api/first",
                "poc": "curl /api/first",
            },
            {
                "id": "finding-001",
                "title": "Second",
                "description": "Second finding.",
                "severity": "LOW",
                "cvss_score": 2.0,
                "target": "/api/second",
                "poc": "curl /api/second",
            },
        ],
    }

    with pytest.raises(IncompleteFindingError, match="duplicado"):
        parse_strix_output(
            report,
            organization_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            db=None,
        )


def test_parser_rejects_finding_without_poc() -> None:
    report = {
        "scan_id": "scan-missing-poc",
        "status": "completed",
        "findings": [
            {
                "title": "Missing PoC",
                "description": "No reproduction evidence",
                "severity": "HIGH",
                "cvss_score": 7.5,
                "target": "/api/login",
            }
        ]
    }

    with pytest.raises(IncompleteFindingError):
        parse_strix_output(
            report,
            organization_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            db=None,
        )
