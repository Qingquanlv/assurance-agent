"""Profile-aware execution of the four plan checks across all assurance layers."""

from __future__ import annotations

import json

import pytest

from assurance_agent.artifacts.models.assurance import PLAN_CHECK_IDS, LayerName
from assurance_agent.artifacts.models.plan_checks import CheckEvidence, LayerApplicability, PlanCheckDocument
from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.verification.checks.registry import (
    CHECKS_BY_ID,
    _run_profile_checks,
    run_plan_checks,
    validate_plan_check_document,
)

_EXPECTED_STATUS_MAP = {
    "api": {"l1_path": "pass", "shared_factory": "pass", "assert_ideal": "pass", "capability_keys": "pass"},
    "e2e": {"l1_path": "pass", "shared_factory": "pass", "assert_ideal": "pass", "capability_keys": "pass"},
    "fuzz": {
        "l1_path": "pass",
        "shared_factory": "pass",
        "assert_ideal": "not_applicable",
        "capability_keys": "pass",
    },
    "performance": {
        "l1_path": "pass",
        "shared_factory": "pass",
        "assert_ideal": "not_applicable",
        "capability_keys": "pass",
    },
}

_EMPTY_DK: dict[str, object] = {"capabilities": {"domain_factories": {}}}


def _automated_case(case_type: str) -> dict:
    return {
        "added": [
            {
                "case_id": f"TC_ROUND_TRIP_{case_type.upper()}_001",
                "title": "round trip",
                "type": case_type,
                "automation": {"required": True},
                "assertions": ["HTTP 200"],
            }
        ],
        "modified": [],
    }


def _plan_texts_for(layer: str) -> dict[str, str]:
    profile = get_layer_assurance_profile(layer)
    case_id = f"TC_ROUND_TRIP_{profile.case_type.upper()}_001"
    main_plan = profile.plan_artifacts[0]
    case_table = f"| Case ID | Scenario | Expected |\n|---|---|---|\n| {case_id} | round trip | HTTP 200 |\n"
    return {path: (case_table if path == main_plan else "# Plan\n") for path in profile.plan_artifacts}


def _context_for(layer: LayerName) -> CheckContext:
    profile = get_layer_assurance_profile(layer)
    return CheckContext(
        plan_texts=_plan_texts_for(layer),
        cases=(_automated_case(profile.case_type),),
        data_knowledge=_EMPTY_DK,
        layer=layer,
        required_capabilities=(),
    )


def _empty_scope_context(layer: LayerName) -> CheckContext:
    return CheckContext(
        plan_texts={},
        cases=(),
        data_knowledge=_EMPTY_DK,
        layer=layer,
        required_capabilities=(),
    )


@pytest.mark.parametrize("layer", ["api", "e2e", "fuzz", "performance"])
def test_canonical_round_trip_serializes_and_parses_exactly(layer: LayerName) -> None:
    document = run_plan_checks(_context_for(layer))

    round_tripped = PlanCheckDocument.model_validate_json(document.model_dump_json())

    assert round_tripped == document
    assert round_tripped.schema_version == "2"
    assert round_tripped.layer == layer
    assert tuple(item.check_id for item in round_tripped.checks) == PLAN_CHECK_IDS
    assert all(not item.findings for item in round_tripped.checks)
    assert {item.check_id: item.status for item in round_tripped.checks} == _EXPECTED_STATUS_MAP[layer]


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_applicable_layer_with_assert_ideal_passes_every_check(layer: LayerName) -> None:
    document = run_plan_checks(_context_for(layer))
    assert document.schema_version == "2"
    assert document.layer == layer
    assert document.status == "pass"
    assert {item.check_id: item.status for item in document.checks} == {
        "l1_path": "pass",
        "shared_factory": "pass",
        "assert_ideal": "pass",
        "capability_keys": "pass",
    }


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_layers_without_assert_ideal_mark_it_not_in_profile(layer: LayerName) -> None:
    document = run_plan_checks(_context_for(layer))
    assert document.status == "pass"
    by_id = {item.check_id: item for item in document.checks}
    assert by_id["assert_ideal"].status == "not_applicable"
    assert by_id["assert_ideal"].applicability_reason == "check_not_in_profile"
    for check_id in ("l1_path", "shared_factory", "capability_keys"):
        assert by_id[check_id].status == "pass"


def test_empty_scope_marks_every_check_layer_not_applicable() -> None:
    document = run_plan_checks(_empty_scope_context("api"))
    assert document.status == "not_applicable"
    assert document.applicability is not None
    assert document.applicability.applicable is False
    for item in document.checks:
        assert item.status == "not_applicable"
        assert item.applicability_reason == "layer_not_applicable"


def test_missing_exact_plan_artifact_key_raises_value_error_naming_the_path() -> None:
    ctx = _context_for("api")
    missing_path = "plans/api-codegen-plan.md"
    incomplete = CheckContext(
        plan_texts={k: v for k, v in ctx.plan_texts.items() if k != missing_path},
        cases=ctx.cases,
        data_knowledge=ctx.data_knowledge,
        layer=ctx.layer,
    )
    with pytest.raises(ValueError, match=missing_path):
        run_plan_checks(incomplete)


def test_unknown_layer_raises_the_profile_lookup_error() -> None:
    ctx = CheckContext(plan_texts={}, cases=(), data_knowledge={}, layer="api")
    object.__setattr__(ctx, "layer", "bogus")
    with pytest.raises(ValueError, match="unknown assurance layer"):
        run_plan_checks(ctx)


def test_supplying_applicability_for_another_layer_raises_value_error() -> None:
    ctx = _context_for("api")
    other_layer_applicability = LayerApplicability(
        layer="e2e", applicable=True, reason_code="automated_cases_present", case_ids=("TC_X",)
    )
    with pytest.raises(ValueError, match="does not match"):
        run_plan_checks(ctx, applicability=other_layer_applicability)


def test_check_function_returning_wrong_check_id_raises_value_error_not_silent_fold() -> None:
    ctx = _context_for("api")
    profile = get_layer_assurance_profile("api")
    applicability = derive_layer_applicability(ctx.cases, profile)

    def _misbehaving(_: CheckContext) -> CheckEvidence:
        return CheckEvidence(check_id="shared_factory", status="pass")

    bad_checks_by_id = dict(CHECKS_BY_ID)
    bad_checks_by_id["l1_path"] = _misbehaving

    with pytest.raises(ValueError, match="l1_path"):
        _run_profile_checks(ctx, profile, applicability, bad_checks_by_id)


def test_validate_plan_check_document_rejects_na_statuses_contradicting_the_profile() -> None:
    profile = get_layer_assurance_profile("api")
    applicability = LayerApplicability(
        layer="api", applicable=True, reason_code="automated_cases_present", case_ids=("TC_X",)
    )
    checks = tuple(
        CheckEvidence(check_id=check_id, status="not_applicable", applicability_reason="check_not_in_profile")
        for check_id in ("l1_path", "shared_factory", "assert_ideal", "capability_keys")
    )
    document = PlanCheckDocument.model_construct(
        schema_version="2",
        layer="api",
        applicability=applicability,
        status="not_applicable",
        checks=checks,
    )
    with pytest.raises(ValueError):
        validate_plan_check_document(document, profile)


def test_string_automation_flag_raises_a_value_error_in_applicability() -> None:
    profile = get_layer_assurance_profile("api")
    cases = [
        {
            "added": [
                {
                    "case_id": "TC_STRING_FLAG_001",
                    "type": "API",
                    "automation": {"required": "true"},
                }
            ],
            "modified": [],
        }
    ]
    with pytest.raises(ValueError, match="automation.required must be a boolean"):
        derive_layer_applicability(cases, profile)


def test_dropping_a_check_from_serialized_version_2_evidence_raises_on_validation() -> None:
    document = run_plan_checks(_context_for("api"))
    payload = json.loads(document.model_dump_json())
    payload["checks"] = payload["checks"][:-1]

    with pytest.raises(ValueError):
        PlanCheckDocument.model_validate_json(json.dumps(payload))


def test_adding_an_unknown_check_to_version_2_evidence_raises_on_validation() -> None:
    document = run_plan_checks(_context_for("api"))
    payload = json.loads(document.model_dump_json())
    payload["checks"].append(
        {
            "check_id": "unknown_check",
            "status": "pass",
            "findings": [],
            "refs": [],
            "applicability_reason": None,
        }
    )

    with pytest.raises(ValueError):
        PlanCheckDocument.model_validate_json(json.dumps(payload))


def test_changing_document_layer_without_applicability_layer_raises_on_validation() -> None:
    document = run_plan_checks(_context_for("api"))
    payload = json.loads(document.model_dump_json())
    payload["layer"] = "e2e"

    with pytest.raises(ValueError, match="document layer must match applicability layer"):
        PlanCheckDocument.model_validate_json(json.dumps(payload))


def test_validate_plan_check_document_rejects_pass_for_a_statically_excluded_fuzz_check() -> None:
    profile = get_layer_assurance_profile("fuzz")
    applicability = LayerApplicability(
        layer="fuzz", applicable=True, reason_code="automated_cases_present", case_ids=("TC_X",)
    )
    checks = tuple(CheckEvidence(check_id=check_id, status="pass") for check_id in PLAN_CHECK_IDS)
    document = PlanCheckDocument.model_construct(
        schema_version="2",
        layer="fuzz",
        applicability=applicability,
        status="pass",
        checks=checks,
    )

    with pytest.raises(ValueError, match="assert_ideal"):
        validate_plan_check_document(document, profile)
