"""Deep validation for plan assurance evidence consumed by gate DSL builtins."""

from __future__ import annotations

import json

import pytest

from assurance_agent.artifacts.models.assurance import PLAN_CHECK_IDS
from assurance_agent.artifacts.models.plan_checks import CheckEvidence, LayerApplicability, PlanCheckDocument
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.verification.gate_state import plan_assurance_state
from assurance_agent.verification.profiles import get_layer_assurance_profile

_CHANGE_ID = "CH-1"
_EMPTY_DK: dict[str, object] = {
    "version": 1,
    "capabilities": {"domain_factories": {}},
}
_VALID_REVIEW = {
    "schema_version": "1.0",
    "decision": "pass",
    "review_type": "api-plan",
    "change_id": _CHANGE_ID,
    "auto_fix_allowed": False,
    "human_review_required": False,
    "codegen_readiness": "ready",
    "risk_level": "low",
    "findings": [],
    "auto_fix_plan": [],
    "next_action": "continue",
    "required_capabilities": ["capabilities.domain_factories.dept.make_dept"],
}


def _empty_scope_checks(layer: str) -> dict[str, object]:
    document = run_plan_checks(
        CheckContext(plan_texts={}, cases=(), data_knowledge=_EMPTY_DK, layer=layer)  # type: ignore[arg-type]
    )
    return document.model_dump(mode="json")


def _applicable_checks(layer: str) -> dict[str, object]:

    profile = get_layer_assurance_profile(layer)
    case_id = f"TC_GATE_{profile.case_type.upper()}_001"
    cases = (
        {
            "added": [
                {
                    "case_id": case_id,
                    "title": "gate state",
                    "type": profile.case_type,
                    "automation": {"required": True},
                    "assertions": ["HTTP 200"],
                }
            ],
            "modified": [],
        },
    )
    main_plan = profile.plan_artifacts[0]
    case_table = f"| Case ID | Scenario | Expected |\n|---|---|---|\n| {case_id} | gate | HTTP 200 |\n"
    plan_texts = {path: (case_table if path == main_plan else "# Plan\n") for path in profile.plan_artifacts}
    document = run_plan_checks(
        CheckContext(
            plan_texts=plan_texts,
            cases=cases,
            data_knowledge=_EMPTY_DK,
            layer=layer,  # type: ignore[arg-type]
        )
    )
    return document.model_dump(mode="json")


def _review_for(layer: str) -> dict[str, object]:
    return {**_VALID_REVIEW, "review_type": f"{layer}-plan"}


@pytest.fixture()
def valid_inapplicable_checks() -> dict[str, object]:
    return _empty_scope_checks("e2e")


def test_valid_applicable_api_e2e() -> None:
    for layer in ("api", "e2e"):
        assert (
            plan_assurance_state(
                _applicable_checks(layer),
                _review_for(layer),
                _EMPTY_DK,
                layer,
                change_id=_CHANGE_ID,
            )
            == "applicable"
        )


def test_valid_inapplicable_without_review_or_l1(valid_inapplicable_checks: dict[str, object]) -> None:
    assert (
        plan_assurance_state(valid_inapplicable_checks, None, None, "e2e", change_id=_CHANGE_ID)
        == "not_applicable"
    )


def test_missing_checks_is_invalid() -> None:
    assert plan_assurance_state(None, None, None, "e2e", change_id=_CHANGE_ID) == "invalid"


def test_v1_checks_are_invalid() -> None:
    payload = {
        "schema_version": "1",
        "status": "pass",
        "checks": [{"check_id": "l1_path", "status": "pass"}],
    }
    assert (
        plan_assurance_state(payload, _review_for("api"), _EMPTY_DK, "api", change_id=_CHANGE_ID) == "invalid"
    )


def test_malformed_checks_are_invalid(valid_inapplicable_checks: dict[str, object]) -> None:
    broken = dict(valid_inapplicable_checks)
    broken["status"] = "maybe"
    assert plan_assurance_state(broken, None, None, "e2e", change_id=_CHANGE_ID) == "invalid"


def test_duplicate_checks_are_invalid() -> None:
    document = run_plan_checks(
        CheckContext(plan_texts={}, cases=(), data_knowledge=_EMPTY_DK, layer="api")  # type: ignore[arg-type]
    )
    payload = json.loads(document.model_dump_json())
    payload["checks"] = payload["checks"] + [payload["checks"][0]]
    assert plan_assurance_state(payload, None, None, "api", change_id=_CHANGE_ID) == "invalid"


def test_incomplete_checks_are_invalid() -> None:
    document = run_plan_checks(
        CheckContext(plan_texts={}, cases=(), data_knowledge=_EMPTY_DK, layer="api")  # type: ignore[arg-type]
    )
    payload = json.loads(document.model_dump_json())
    payload["checks"] = payload["checks"][:-1]
    assert plan_assurance_state(payload, None, None, "api", change_id=_CHANGE_ID) == "invalid"


def test_unknown_checks_are_invalid() -> None:
    document = run_plan_checks(
        CheckContext(plan_texts={}, cases=(), data_knowledge=_EMPTY_DK, layer="api")  # type: ignore[arg-type]
    )
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
    assert plan_assurance_state(payload, None, None, "api", change_id=_CHANGE_ID) == "invalid"


def test_wrong_layer_param_is_invalid(valid_inapplicable_checks: dict[str, object]) -> None:
    assert (
        plan_assurance_state(valid_inapplicable_checks, None, None, "api", change_id=_CHANGE_ID) == "invalid"
    )


def test_unknown_layer_is_invalid(valid_inapplicable_checks: dict[str, object]) -> None:
    assert (
        plan_assurance_state(valid_inapplicable_checks, None, None, "bogus", change_id=_CHANGE_ID)
        == "invalid"
    )


def test_applicable_missing_review_is_invalid() -> None:
    assert (
        plan_assurance_state(_applicable_checks("api"), None, _EMPTY_DK, "api", change_id=_CHANGE_ID)
        == "invalid"
    )


def test_applicable_malformed_review_is_invalid() -> None:
    review = _review_for("api")
    review["decision"] = "not-a-decision"
    assert (
        plan_assurance_state(_applicable_checks("api"), review, _EMPTY_DK, "api", change_id=_CHANGE_ID)
        == "invalid"
    )


def test_applicable_wrong_review_type_is_invalid() -> None:
    review = _review_for("api")
    review["review_type"] = "e2e-plan"
    assert (
        plan_assurance_state(_applicable_checks("api"), review, _EMPTY_DK, "api", change_id=_CHANGE_ID)
        == "invalid"
    )


def test_applicable_wrong_change_id_is_invalid() -> None:
    review = _review_for("api")
    review["change_id"] = "CH-OTHER"
    assert (
        plan_assurance_state(_applicable_checks("api"), review, _EMPTY_DK, "api", change_id=_CHANGE_ID)
        == "invalid"
    )


def test_applicable_missing_l1_is_invalid() -> None:
    assert (
        plan_assurance_state(_applicable_checks("api"), _review_for("api"), None, "api", change_id=_CHANGE_ID)
        == "invalid"
    )


def test_applicable_malformed_l1_is_invalid() -> None:
    assert (
        plan_assurance_state(
            _applicable_checks("api"),
            _review_for("api"),
            {"version": "not-an-int"},
            "api",
            change_id=_CHANGE_ID,
        )
        == "invalid"
    )


def test_profile_validation_rejects_contradictory_applicable_checks() -> None:
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
    payload = document.model_dump(mode="json")
    review = {
        **_VALID_REVIEW,
        "review_type": "fuzz-plan",
        "layer_applicable": True,
    }
    assert plan_assurance_state(payload, review, _EMPTY_DK, "fuzz", change_id=_CHANGE_ID) == "invalid"
