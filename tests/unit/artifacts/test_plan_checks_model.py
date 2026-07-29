"""plan-checks evidence 文档：任一 check fail 即文档 fail（spec C2）。"""

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.plan_checks import CheckEvidence, Finding, PlanCheckDocument
from assurance_agent.artifacts.registry import match_artifact


def _fail(check_id: str) -> CheckEvidence:
    return CheckEvidence(
        check_id=check_id,
        status="fail",
        findings=(Finding(locator="plans/api-plan.md:12", actual="x", expected="y"),),
        refs=("plans/api-plan.md",),
    )


def test_document_status_is_pass_when_every_check_passes() -> None:
    doc = PlanCheckDocument.from_checks(
        [
            CheckEvidence(check_id="l1_path", status="pass"),
            CheckEvidence(check_id="assert_ideal", status="pass"),
        ]
    )
    assert doc.status == "pass"
    assert doc.schema_version == "1"


def test_document_status_is_fail_when_any_check_fails() -> None:
    doc = PlanCheckDocument.from_checks(
        [CheckEvidence(check_id="l1_path", status="pass"), _fail("assert_ideal")]
    )
    assert doc.status == "fail"


def test_empty_check_set_is_pass() -> None:
    assert PlanCheckDocument.from_checks([]).status == "pass"


def test_findings_carry_locators_not_bare_booleans() -> None:
    finding = _fail("l1_path").findings[0]
    assert finding.locator == "plans/api-plan.md:12"
    assert finding.actual and finding.expected


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CheckEvidence(check_id="l1_path", status="warn")


def test_registry_binds_plan_checks_before_the_generic_review_glob() -> None:
    spec = match_artifact("review/api-plan-checks.json")
    assert spec is not None
    assert spec.artifact_type == "plan_check"
    assert spec.model is PlanCheckDocument
