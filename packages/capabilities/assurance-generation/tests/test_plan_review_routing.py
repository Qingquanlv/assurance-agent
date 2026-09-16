from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_generation.contracts.reviews import PlanReviewAuthoring, public_review_outcome
from assurance_generation.operations.plan_review_policy import apply_plan_review_policy


VALID_LEAFS = frozenset({"entities.item.create"})


def _finding(finding_id: str = "API-PLAN-001") -> dict[str, object]:
    return {
        "id": finding_id,
        "severity": "high",
        "category": "coverage",
        "message": "empty-string name is missing",
        "locator": {
            "artifact": "qa/results/plans/api-plan.md",
            "case_id": "TC_DEPT_006",
            "key": "4. Request Strategy",
        },
    }


def _authoring(
    *,
    route: str = "codegen",
    findings: list[dict[str, object]] | None = None,
    finding_ids: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "review_type": "api-codegen",
        "change_id": "CH-DEMO-001",
        "route": route,
        "findings": [] if findings is None else findings,
        "finding_ids": [] if finding_ids is None else finding_ids,
        "next_action": "proceed to codegen",
        "risk_level": "medium",
        "required_capabilities": ["entities.item.create"],
    }
    if extra:
        payload.update(extra)
    return payload


def _validate(raw: dict[str, object]) -> PlanReviewAuthoring:
    return PlanReviewAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_codegen_and_auto_fix_routes_are_the_only_routing_fields() -> None:
    passed = _validate(_authoring())
    assert passed.route == "codegen"
    assert passed.finding_ids == []
    assert passed.public_outcome == "pass"
    assert not hasattr(passed, "decision") or "decision" not in passed.model_fields_set

    repaired = _validate(_authoring(route="auto_fix", findings=[_finding()], finding_ids=["API-PLAN-001"]))
    assert repaired.route == "auto_fix"
    assert repaired.finding_ids == ["API-PLAN-001"]
    assert repaired.public_outcome == "needs_fix"


@pytest.mark.parametrize(
    ("route", "finding_ids", "findings"),
    [
        ("codegen", ["API-PLAN-001"], [_finding()]),
        ("auto_fix", [], [_finding()]),
        ("auto_fix", ["API-PLAN-001"], []),
        ("human", ["API-PLAN-001"], [_finding()]),
        ("reject", ["API-PLAN-001"], [_finding()]),
    ],
)
def test_illegal_route_and_finding_ids_combinations_are_rejected(
    route: str,
    finding_ids: list[str],
    findings: list[dict[str, object]],
) -> None:
    with pytest.raises(ValidationError):
        _validate(_authoring(route=route, findings=findings, finding_ids=finding_ids))


@pytest.mark.parametrize(
    "legacy_field",
    ("decision", "auto_fix_allowed", "human_review_required", "auto_fix_plan", "codegen_readiness"),
)
def test_legacy_routing_fields_are_rejected(legacy_field: str) -> None:
    raw = _authoring(extra={legacy_field: "pass" if legacy_field == "decision" else True})
    if legacy_field == "auto_fix_plan":
        raw[legacy_field] = []
    if legacy_field == "codegen_readiness":
        raw[legacy_field] = "ready"
    with pytest.raises(ValidationError, match="route and finding_ids"):
        _validate(raw)


def test_policy_validates_without_rewriting_findings_or_route() -> None:
    raw = _authoring(
        route="auto_fix",
        findings=[
            _finding("API-PLAN-001"),
            {
                **_finding("API-PLAN-002"),
                "category": "test-runner",
                "message": "asyncio_mode = 'strict' so mark async tests",
                "locator": {
                    "artifact": "qa/results/plans/api-codegen-plan.md",
                    "key": "7. Run Guidance",
                },
            },
        ],
        finding_ids=["API-PLAN-001", "API-PLAN-002"],
    )
    kept = apply_plan_review_policy(
        raw,
        previous={"route": "codegen", "finding_ids": []},
    )
    assert kept["route"] == "auto_fix"
    assert [item["id"] for item in kept["findings"]] == ["API-PLAN-001", "API-PLAN-002"]
    assert kept["finding_ids"] == ["API-PLAN-001", "API-PLAN-002"]


def test_public_outcome_follows_route_enum() -> None:
    assert public_review_outcome("codegen") == "pass"
    assert public_review_outcome("auto_fix") == "needs_fix"
    assert public_review_outcome("human") == "needs_human"
    assert public_review_outcome("reject") == "reject"
    with pytest.raises(ValueError):
        public_review_outcome("pass")
