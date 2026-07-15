import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import ApplySummary, FixProposal, SafetyCheck


def make_fix_proposal(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "source_analysis": "inspect/failure-analysis.json",
        "source_batch_id": "b1",
        "summary": {"eligible_count": 1, "not_eligible_count": 0, "targets": {"api": 1, "e2e": 0}},
        "proposals": [
            {
                "proposal_id": "FIX-001",
                "target": "api",
                "eligible": True,
                "risk_level": "low",
                "files_to_modify": ["tests/api/test_menu.py"],
            }
        ],
        "not_eligible": [],
    }
    doc.update(overrides)
    return doc


def make_safety_check(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "source": "cli",
        "change_id": "CH-1",
        "passed": True,
        "needs_review": False,
        "product_code_modified": False,
        "skip_or_xfail_added": False,
        "bare_return_added": False,
        "unrelated_tests_modified": False,
        "unrelated_test_files": [],
        "assertion_expected_value_changes_detected": False,
        "high_risk_proposal_applied": False,
    }
    doc.update(overrides)
    return doc


def test_fix_proposal_valid_fixture_parses_and_keeps_extras() -> None:
    model = FixProposal.model_validate(make_fix_proposal())
    assert model.summary.eligible_count == 1
    assert model.proposals[0].target == "api"
    assert model.proposals[0].eligible is True
    assert model.model_extra is not None
    assert model.model_extra["source_batch_id"] == "b1"


def test_fix_proposal_target_enum_violation_fails() -> None:
    doc = make_fix_proposal()
    doc["proposals"][0]["target"] = "database"
    with pytest.raises(ValidationError):
        FixProposal.model_validate(doc)


def test_fix_proposal_missing_summary_eligible_count_fails() -> None:
    with pytest.raises(ValidationError):
        FixProposal.model_validate(make_fix_proposal(summary={"targets": {}}))
    doc = make_fix_proposal()
    del doc["summary"]
    with pytest.raises(ValidationError):
        FixProposal.model_validate(doc)


def test_apply_summary_valid_fixture_parses() -> None:
    model = ApplySummary.model_validate(
        {"schema_version": "1.0", "target": "api", "applied": True, "modified_files": []}
    )
    assert model.target == "api"
    assert model.applied is True


def test_apply_summary_target_enum_violation_fails() -> None:
    with pytest.raises(ValidationError):
        ApplySummary.model_validate({"schema_version": "1.0", "target": "fuzz", "applied": True})


def test_safety_check_valid_pass_fixture_parses() -> None:
    model = SafetyCheck.model_validate(make_safety_check())
    assert model.passed is True
    assert model.high_risk_proposal_applied is False


def test_safety_check_undetermined_skip_flag_parses() -> None:
    model = SafetyCheck.model_validate(
        make_safety_check(skip_or_xfail_added="undetermined", needs_review=True, passed=False)
    )
    assert model.skip_or_xfail_added == "undetermined"
    assert model.needs_review is True


def test_safety_check_missing_passed_fails() -> None:
    doc = make_safety_check()
    del doc["passed"]
    with pytest.raises(ValidationError):
        SafetyCheck.model_validate(doc)
