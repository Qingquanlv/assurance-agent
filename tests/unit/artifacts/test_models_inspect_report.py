import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import FailureAnalysis, QualityGateResult, QualityReport


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
