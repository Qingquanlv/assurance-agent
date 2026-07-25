import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import FailureAnalysis, IssueReport, QualityGateResult, QualityReport


def make_failure_entry(**overrides: object) -> dict:
    entry: dict = {
        "case_id": "TC_MENU_001",
        "target": "api",
        "category": "assertion_failure",
        "fix_proposal_eligible": False,
        "severity": "high",
        "evidence": {
            "result_file": "execution/runs/b1/api-result.json",
            "test_file": "tests/api/test_menu.py",
            "trace": "",
            "screenshot": "",
            "video": "",
            "raw_log": "execution/runs/b1/api-raw.log",
            "log_excerpt": "AssertionError: expected 200 got 500",
        },
        "diagnosis": "endpoint returned 500",
        "recommended_action": "inspect server log",
    }
    entry.update(overrides)
    return entry


def make_failure_analysis(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "source_manifest": "execution/execution-manifest.yaml",
        "inspection_status": "completed",
        "batch_id": "b1",
        "source_batch_id": "b1",
        "final_status": "FAIL",
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "analyzed",
        "failures": [make_failure_entry()],
        "hard_fails": [],
        "needs_review": [],
        "known_product_issues": [],
    }
    doc.update(overrides)
    return doc


def make_functional() -> dict:
    return {
        "status": "PASS",
        "api": {"total": 10, "passed": 10, "failed": 0},
        "e2e": {"total": 4, "passed": 4, "failed": 0},
    }


def make_coverage() -> dict:
    return {
        "status": "PASS",
        "available": True,
        "line_coverage": 85.0,
        "branch_coverage": 70.0,
        "threshold": {"line": 70, "branch": 60},
    }


def make_quality_gate_result(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "b1",
        "dimensions": {"functional": make_functional(), "coverage": make_coverage()},
        "final_status": "PASS",
    }
    doc.update(overrides)
    return doc


def make_quality_report(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "b1",
        "final_status": "PASS",
        "quality_score": 92.5,
        "score_breakdown": {"functional": 95, "coverage": 88, "fuzz": "N/A", "performance": "N/A"},
        "scope": {"cases": 14, "requirements": ["REQ-1"]},
        "functional": make_functional(),
        "coverage": make_coverage(),
        "defects": {"product": [], "test": [], "environment": []},
        "risk_level": "LOW",
        "risk_rationale": "all dimensions pass",
        "recommendation": "release",
    }
    doc.update(overrides)
    return doc


def test_failure_analysis_valid_fixture_parses() -> None:
    model = FailureAnalysis.model_validate(make_failure_analysis())
    assert model.source_batch_id == "b1"
    assert model.failures[0].category == "assertion_failure"
    assert model.failures[0].fix_proposal_eligible is False
    assert model.final_status == "FAIL"


def test_failure_analysis_category_enum_violation_fails() -> None:
    doc = make_failure_analysis(failures=[make_failure_entry(category="cosmic_rays")])
    with pytest.raises(ValidationError):
        FailureAnalysis.model_validate(doc)


def test_failure_analysis_missing_source_batch_id_fails() -> None:
    doc = make_failure_analysis()
    del doc["source_batch_id"]
    with pytest.raises(ValidationError):
        FailureAnalysis.model_validate(doc)


def test_failure_analysis_fix_proposal_eligible_required() -> None:
    entry = make_failure_entry()
    del entry["fix_proposal_eligible"]
    with pytest.raises(ValidationError):
        FailureAnalysis.model_validate(make_failure_analysis(failures=[entry]))


def test_failure_analysis_fuzz_and_perf_categories_accepted() -> None:
    doc = make_failure_analysis(
        failures=[
            make_failure_entry(target="fuzz", category="fuzz_stateful_failure"),
            make_failure_entry(target="performance", category="perf_threshold_exceeded"),
        ]
    )
    assert len(FailureAnalysis.model_validate(doc).failures) == 2


def test_quality_gate_result_valid_fixture_parses() -> None:
    model = QualityGateResult.model_validate(make_quality_gate_result())
    assert model.dimensions.functional.api.total == 10
    assert model.final_status == "PASS"


def test_quality_gate_result_final_status_enum_violation_fails() -> None:
    with pytest.raises(ValidationError):
        QualityGateResult.model_validate(make_quality_gate_result(final_status="OK"))


def test_quality_report_valid_fixture_parses() -> None:
    model = QualityReport.model_validate(make_quality_report())
    assert model.quality_score == 92.5
    assert model.score_breakdown.fuzz == "N/A"
    assert model.risk_level == "LOW"


def test_quality_report_bad_risk_level_fails() -> None:
    with pytest.raises(ValidationError):
        QualityReport.model_validate(make_quality_report(risk_level="low"))


# ---- Schema 1.1 / IssueReport tests ----------------------------------------


def _make_issue_report(**overrides: object) -> dict:
    doc: dict = {
        "analysis_status": "completed",
        "project_sync_status": "completed",
        "total_occurrences": 2,
        "counts_by_status": {"detected": 1, "resolved": 1},
        "counts_by_classification": {"product_bug": 2},
        "counts_by_severity": {"high": 1},
        "new_count": 2,
        "repeated_count": 0,
        "regressed_count": 0,
        "resolved_count": 1,
        "accepted_risk_count": 0,
        "not_an_issue_count": 0,
        "issue_risk": "high",
        "issue_risk_rationale": "1 active product_bug (high severity).",
    }
    doc.update(overrides)
    return doc


def test_quality_report_schema_version_11_with_issues_parses() -> None:
    doc = make_quality_report(schema_version="1.1", issues=_make_issue_report())
    model = QualityReport.model_validate(doc)
    assert model.schema_version == "1.1"
    assert model.issues is not None
    assert model.issues.issue_risk == "high"
    assert model.issues.analysis_status == "completed"
    assert model.issues.total_occurrences == 2


def test_quality_report_10_without_issues_still_parses() -> None:
    model = QualityReport.model_validate(make_quality_report(schema_version="1.0"))
    assert model.issues is None
    # final_status is unchanged regardless of schema version
    assert model.final_status == "PASS"


def test_quality_report_11_without_issues_parses() -> None:
    model = QualityReport.model_validate(make_quality_report(schema_version="1.1"))
    assert model.issues is None


def test_issue_report_unknown_risk_parses() -> None:
    doc = make_quality_report(
        schema_version="1.1",
        issues=_make_issue_report(
            analysis_status="failed",
            total_occurrences=0,
            counts_by_status={},
            counts_by_classification={},
            counts_by_severity={},
            new_count=0,
            resolved_count=0,
            issue_risk="unknown",
            issue_risk_rationale="Issue analysis failed or incomplete",
        ),
    )
    model = QualityReport.model_validate(doc)
    assert model.issues is not None
    assert model.issues.issue_risk == "unknown"
    # final_status is independent of Issue risk
    assert model.final_status == "PASS"


def test_issue_report_clear_risk_parses() -> None:
    doc = make_quality_report(
        schema_version="1.1",
        issues=_make_issue_report(
            counts_by_status={"resolved": 2},
            counts_by_severity={},
            new_count=0,
            resolved_count=2,
            issue_risk="clear",
            issue_risk_rationale="No active Issues — all resolved or not-an-issue.",
        ),
    )
    model = QualityReport.model_validate(doc)
    assert model.issues is not None
    assert model.issues.issue_risk == "clear"


def test_issue_report_accepted_risk_is_not_clear() -> None:
    doc = make_quality_report(
        schema_version="1.1",
        issues=_make_issue_report(
            counts_by_status={"accepted_risk": 1},
            counts_by_severity={"medium": 1},
            accepted_risk_count=1,
            resolved_count=0,
            issue_risk="medium",
            issue_risk_rationale="1 active issue(s); accepted_risk is still active.",
        ),
    )
    model = QualityReport.model_validate(doc)
    assert model.issues is not None
    assert model.issues.issue_risk == "medium"
    assert model.issues.accepted_risk_count == 1


def test_issue_report_bad_risk_value_fails() -> None:
    with pytest.raises(ValidationError):
        IssueReport.model_validate(_make_issue_report(issue_risk="UNKNOWN"))


def test_quality_report_bad_schema_version_fails() -> None:
    with pytest.raises(ValidationError):
        QualityReport.model_validate(make_quality_report(schema_version="2.0"))


def test_issue_report_final_status_unchanged() -> None:
    """Issue risk must never rewrite final_status."""
    doc = make_quality_report(
        schema_version="1.1",
        final_status="PASS",
        issues=_make_issue_report(issue_risk="critical"),
    )
    model = QualityReport.model_validate(doc)
    assert model.final_status == "PASS"
    assert model.issues is not None
    assert model.issues.issue_risk == "critical"
