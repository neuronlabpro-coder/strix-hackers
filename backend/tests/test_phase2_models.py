from sqlalchemy import Table

from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.vulnerabilities.models import (
    IssueStatusEnum,
    SeverityEnum,
    Vulnerability,
)


def test_phase2_models_define_required_enums_and_indexes() -> None:
    pentest_table = PentestRun.__table__
    vulnerability_table = Vulnerability.__table__
    assert isinstance(pentest_table, Table)
    assert isinstance(vulnerability_table, Table)

    assert [item.value for item in TargetTypeEnum] == ["REPOSITORY", "DOMAIN", "API_SPEC"]
    assert [item.value for item in ScanModeEnum] == ["QUICK", "STANDARD", "DEEP"]
    assert [item.value for item in ScanStatusEnum] == [
        "QUEUED",
        "RUNNING",
        "COMPLETED",
        "FAILED",
        "TIMED_OUT",
        "ABORTED",
    ]
    assert [item.value for item in SeverityEnum] == [
        "CRITICAL",
        "HIGH",
        "MEDIUM",
        "LOW",
        "INFO",
    ]
    assert [item.value for item in IssueStatusEnum] == [
        "OPEN",
        "IN_PROGRESS",
        "FIXED",
        "SNOOZED",
        "IGNORED",
    ]
    assert "ix_pentest_runs_org_status" in {index.name for index in pentest_table.indexes}
    assert "ix_vulnerabilities_org_severity" in {
        index.name for index in vulnerability_table.indexes
    }
    assert "ix_vulnerabilities_org_status" in {
        index.name for index in vulnerability_table.indexes
    }
