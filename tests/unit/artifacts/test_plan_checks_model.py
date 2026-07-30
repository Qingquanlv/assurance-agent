"""plan-checks evidence documents carry complete, applicability-aware results."""

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.assurance import PLAN_CHECK_IDS
from assurance_agent.artifacts.models.plan_checks import (
    CheckEvidence,
    Finding,
    LayerApplicability,
    PlanCheckDocument,
)
from assurance_agent.artifacts.registry import match_artifact


def _api_scope(*, applicable: bool = True) -> LayerApplicability:
    return LayerApplicability(
        layer="api",
        applicable=applicable,
        reason_code=("automated_cases_present" if applicable else "no_automated_cases"),
        case_ids=("TC_API_001",) if applicable else (),
    )


def _finding() -> Finding:
    return Finding(locator="plans/api-plan.md:12", actual="x", expected="y")


def _checks(status: str = "pass") -> list[CheckEvidence]:
    return [CheckEvidence(check_id=check_id, status=status) for check_id in PLAN_CHECK_IDS]


def test_v2_inapplicable_document_contains_every_known_check() -> None:
    checks = [
        CheckEvidence(
            check_id=check_id,
            status="not_applicable",
            applicability_reason="layer_not_applicable",
        )
        for check_id in PLAN_CHECK_IDS
    ]
    doc = PlanCheckDocument.from_checks(
        layer="api",
        applicability=_api_scope(applicable=False),
        checks=checks,
    )
    assert doc.schema_version == "2"
    assert doc.status == "not_applicable"


def test_v2_rejects_duplicate_check_ids() -> None:
    checks = _checks()
    checks[-1] = CheckEvidence(check_id=PLAN_CHECK_IDS[0], status="pass")

    with pytest.raises(ValidationError):
        PlanCheckDocument.from_checks(layer="api", applicability=_api_scope(), checks=checks)


def test_v2_rejects_omitted_check_ids() -> None:
    with pytest.raises(ValidationError):
        PlanCheckDocument.from_checks(
            layer="api", applicability=_api_scope(), checks=_checks()[:-1]
        )


def test_v2_rejects_unknown_check_ids() -> None:
    checks = _checks()
    checks[-1] = CheckEvidence(check_id="unexpected", status="pass")

    with pytest.raises(ValidationError):
        PlanCheckDocument.from_checks(layer="api", applicability=_api_scope(), checks=checks)


def test_v2_rejects_layer_mismatched_with_applicability() -> None:
    with pytest.raises(ValidationError):
        PlanCheckDocument.from_checks(
            layer="e2e", applicability=_api_scope(), checks=_checks()
        )


def test_v2_inapplicable_layer_rejects_fail_check_via_model_validate() -> None:
    checks = [
        CheckEvidence(
            check_id=check_id,
            status="not_applicable",
            applicability_reason="layer_not_applicable",
        )
        for check_id in PLAN_CHECK_IDS
    ]
    checks[0] = CheckEvidence(
        check_id=PLAN_CHECK_IDS[0], status="fail", findings=(_finding(),)
    )

    with pytest.raises(ValidationError):
        PlanCheckDocument.model_validate(
            {
                "schema_version": "2",
                "layer": "api",
                "applicability": _api_scope(applicable=False).model_dump(),
                "status": "not_applicable",
                "checks": [check.model_dump() for check in checks],
            }
        )


def test_v2_inapplicable_layer_rejects_pass_check_via_model_validate() -> None:
    checks = [
        CheckEvidence(
            check_id=check_id,
            status="not_applicable",
            applicability_reason="layer_not_applicable",
        )
        for check_id in PLAN_CHECK_IDS
    ]
    checks[0] = CheckEvidence(check_id=PLAN_CHECK_IDS[0], status="pass")

    with pytest.raises(ValidationError):
        PlanCheckDocument.model_validate(
            {
                "schema_version": "2",
                "layer": "api",
                "applicability": _api_scope(applicable=False).model_dump(),
                "status": "not_applicable",
                "checks": [check.model_dump() for check in checks],
            }
        )


def test_v2_inapplicable_layer_rejects_wrong_not_applicable_reason() -> None:
    checks = [
        CheckEvidence(
            check_id=check_id,
            status="not_applicable",
            applicability_reason="layer_not_applicable",
        )
        for check_id in PLAN_CHECK_IDS
    ]
    checks[0] = CheckEvidence(
        check_id=PLAN_CHECK_IDS[0],
        status="not_applicable",
        applicability_reason="check_not_in_profile",
    )

    with pytest.raises(ValidationError):
        PlanCheckDocument.model_validate(
            {
                "schema_version": "2",
                "layer": "api",
                "applicability": _api_scope(applicable=False).model_dump(),
                "status": "not_applicable",
                "checks": [check.model_dump() for check in checks],
            }
        )


def test_v2_from_checks_rejects_fail_check_on_inapplicable_layer() -> None:
    checks = [
        CheckEvidence(
            check_id=check_id,
            status="not_applicable",
            applicability_reason="layer_not_applicable",
        )
        for check_id in PLAN_CHECK_IDS
    ]
    checks[0] = CheckEvidence(
        check_id=PLAN_CHECK_IDS[0], status="fail", findings=(_finding(),)
    )

    with pytest.raises(ValidationError):
        PlanCheckDocument.from_checks(
            layer="api", applicability=_api_scope(applicable=False), checks=checks
        )


def test_v2_from_checks_rejects_pass_check_on_inapplicable_layer() -> None:
    checks = [
        CheckEvidence(
            check_id=check_id,
            status="not_applicable",
            applicability_reason="layer_not_applicable",
        )
        for check_id in PLAN_CHECK_IDS
    ]
    checks[0] = CheckEvidence(check_id=PLAN_CHECK_IDS[0], status="pass")

    with pytest.raises(ValidationError):
        PlanCheckDocument.from_checks(
            layer="api", applicability=_api_scope(applicable=False), checks=checks
        )


def test_v2_rejects_aggregate_status_mismatched_with_checks() -> None:
    checks = _checks()
    checks[0] = CheckEvidence(check_id=PLAN_CHECK_IDS[0], status="fail", findings=(_finding(),))

    with pytest.raises(ValidationError):
        PlanCheckDocument.model_validate(
            {
                "schema_version": "2",
                "layer": "api",
                "applicability": _api_scope().model_dump(),
                "status": "pass",
                "checks": [check.model_dump() for check in checks],
            }
        )


def test_applicable_layer_requires_present_cases_in_sorted_unique_order() -> None:
    with pytest.raises(ValidationError):
        LayerApplicability(
            layer="api",
            applicable=True,
            reason_code="automated_cases_present",
            case_ids=("TC_API_002", "TC_API_001"),
        )

    with pytest.raises(ValidationError):
        LayerApplicability(
            layer="api",
            applicable=True,
            reason_code="automated_cases_present",
        )


def test_inapplicable_layer_requires_no_automated_cases_and_no_case_ids() -> None:
    with pytest.raises(ValidationError):
        LayerApplicability(
            layer="api",
            applicable=False,
            reason_code="automated_cases_present",
        )

    with pytest.raises(ValidationError):
        LayerApplicability(
            layer="api",
            applicable=False,
            reason_code="no_automated_cases",
            case_ids=("TC_API_001",),
        )


def test_fail_requires_findings() -> None:
    with pytest.raises(ValidationError):
        CheckEvidence(check_id="l1_path", status="fail")


def test_pass_rejects_findings_and_applicability_reason() -> None:
    with pytest.raises(ValidationError):
        CheckEvidence(check_id="l1_path", status="pass", findings=(_finding(),))

    with pytest.raises(ValidationError):
        CheckEvidence(
            check_id="l1_path",
            status="pass",
            applicability_reason="check_not_in_profile",
        )


def test_not_applicable_rejects_findings_and_requires_reason() -> None:
    with pytest.raises(ValidationError):
        CheckEvidence(check_id="l1_path", status="not_applicable")

    with pytest.raises(ValidationError):
        CheckEvidence(
            check_id="l1_path",
            status="not_applicable",
            findings=(_finding(),),
            applicability_reason="check_not_in_profile",
        )


def test_fail_rejects_applicability_reason() -> None:
    with pytest.raises(ValidationError):
        CheckEvidence(
            check_id="l1_path",
            status="fail",
            findings=(_finding(),),
            applicability_reason="check_not_in_profile",
        )


def test_v1_document_remains_readable_without_invented_applicability() -> None:
    doc = PlanCheckDocument.model_validate(
        {
            "schema_version": "1",
            "status": "pass",
            "checks": [{"check_id": "l1_path", "status": "pass"}],
        }
    )
    assert doc.layer is None
    assert doc.applicability is None


def test_v1_rejects_not_applicable_check_status() -> None:
    with pytest.raises(ValidationError):
        PlanCheckDocument.model_validate(
            {
                "schema_version": "1",
                "status": "not_applicable",
                "checks": [{"check_id": "l1_path", "status": "not_applicable"}],
            }
        )


def test_findings_carry_locators_not_bare_booleans() -> None:
    finding = _finding()
    assert finding.locator == "plans/api-plan.md:12"
    assert finding.actual and finding.expected


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CheckEvidence.model_validate({"check_id": "l1_path", "status": "warn"})


def test_registry_binds_plan_checks_before_the_generic_review_glob() -> None:
    spec = match_artifact("review/api-plan-checks.json")
    assert spec is not None
    assert spec.artifact_type == "plan_check"
    assert spec.model is PlanCheckDocument
