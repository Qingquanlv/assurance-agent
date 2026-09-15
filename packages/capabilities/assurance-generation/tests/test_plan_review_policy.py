from __future__ import annotations

import pytest

from assurance_generation.contracts.reviews import PlanReviewAuthoring
from assurance_generation.operations.plan_review_policy import (
    apply_plan_review_policy,
    is_runner_contract_finding,
    load_finding_scope,
    write_finding_scope,
)


def _finding(
    finding_id: str,
    category: str,
    message: str,
    *,
    case_id: str | None = None,
    key: str = "5. Assertion Strategy",
) -> dict[str, object]:
    return {
        "id": finding_id,
        "severity": "high",
        "category": category,
        "message": message,
        "locator": {
            "artifact": "qa/results/plans/api-codegen-plan.md",
            "case_id": case_id,
            "key": key,
        },
    }


def _needs_fix(*findings: dict[str, object]) -> dict[str, object]:
    ids = [str(item["id"]) for item in findings]
    return {
        "schema_version": "1.0",
        "review_type": "api-plan",
        "change_id": "CH-DEMO-001",
        "decision": "needs_fix",
        "findings": list(findings),
        "auto_fix_plan": ids,
        "next_action": "run api planner",
        "auto_fix_allowed": True,
        "human_review_required": False,
        "codegen_readiness": "not_ready",
        "risk_level": "high",
        "required_capabilities": ["entities.item.create"],
    }


def test_runner_contract_detects_asyncio_markers_and_run_guidance() -> None:
    assert is_runner_contract_finding(
        _finding(
            "API-PLAN-001",
            "test-runner",
            "asyncio_mode = 'strict' so mark async tests",
            key="7. Run Guidance",
        )
    )
    assert is_runner_contract_finding(
        _finding("API-PLAN-001", "sync_async_invocation", "Need @pytest.mark.asyncio")
    )
    assert not is_runner_contract_finding(
        _finding(
            "API-PLAN-002",
            "request_boundary",
            "TC_DEPT_006 must send the empty-string name",
            case_id="TC_DEPT_006",
        )
    )
    assert not is_runner_contract_finding(
        _finding(
            "API-PLAN-003",
            "runtime_contract",
            "stored password is excluded from GET /user/get",
            case_id="TC_USER_003",
        )
    )


def test_policy_strips_only_runner_findings_and_can_pass() -> None:
    runner = _finding(
        "API-PLAN-001",
        "test-runner",
        "Markers: none required contradicts asyncio_mode",
        key="7. Run Guidance",
    )
    semantic = _finding(
        "API-PLAN-002",
        "coverage",
        "empty-string name is missing",
        case_id="TC_DEPT_006",
    )
    mixed = apply_plan_review_policy(_needs_fix(runner, semantic), previous=None)
    assert [item["id"] for item in mixed["findings"]] == ["API-PLAN-002"]
    assert mixed["decision"] == "needs_fix"
    assert mixed["auto_fix_plan"] == ["API-PLAN-002"]

    only_runner = apply_plan_review_policy(_needs_fix(runner), previous=None)
    assert only_runner["decision"] == "pass"
    assert only_runner["findings"] == []
    assert only_runner["auto_fix_plan"] == []
    assert only_runner["codegen_readiness"] != "not_ready"


def test_retry_cannot_add_new_finding_ids() -> None:
    previous = {"decision": "needs_fix", "finding_ids": ["API-PLAN-002"]}
    first = _finding(
        "API-PLAN-002",
        "coverage",
        "empty-string name is missing",
        case_id="TC_DEPT_006",
    )
    novel = _finding(
        "API-PLAN-004",
        "data-setup",
        "make_dept must materialize DeptClosure",
        case_id="TC_DEPT_011",
    )
    cleaned = apply_plan_review_policy(_needs_fix(first, novel), previous=previous)
    assert [item["id"] for item in cleaned["findings"]] == ["API-PLAN-002"]


def test_prior_pass_is_sticky_against_later_needs_fix() -> None:
    novel = _finding(
        "API-PLAN-001",
        "coverage",
        "empty-string name is missing",
        case_id="TC_DEPT_006",
    )
    cleaned = apply_plan_review_policy(
        _needs_fix(novel),
        previous={"decision": "pass", "finding_ids": []},
    )
    assert cleaned["decision"] == "pass"
    assert cleaned["findings"] == []


def test_finding_scope_roundtrip(tmp_path) -> None:
    write_finding_scope(
        tmp_path,
        family="api",
        coverage_epoch=2,
        change_id="CH-DEMO-001",
        decision="needs_fix",
        finding_ids=("API-PLAN-002",),
    )
    assert load_finding_scope(tmp_path, family="api", coverage_epoch=2) == {
        "decision": "needs_fix",
        "finding_ids": ["API-PLAN-002"],
    }
    assert load_finding_scope(tmp_path, family="api", coverage_epoch=3) is None


@pytest.mark.parametrize("previous", [None, {"decision": "pass", "finding_ids": []}])
@pytest.mark.parametrize("category", ["oracle", "runner_contract"])
def test_policy_preserves_human_required_needs_fix(previous, category: str) -> None:
    raw = _needs_fix(_finding("F1", category, "A human must resolve the oracle ambiguity."))
    raw.update(auto_fix_allowed=False, human_review_required=True, auto_fix_plan=[])
    context = {"capability_leafs": frozenset({"entities.item.create"})}
    original = PlanReviewAuthoring.model_validate(raw, context=context)
    assert original.public_outcome == "needs_human"

    result = PlanReviewAuthoring.model_validate(
        apply_plan_review_policy(original.model_dump(mode="json"), previous=previous), context=context
    )

    assert result.public_outcome == "needs_human"
    assert result.human_review_required is True
    assert result.auto_fix_allowed is False
    assert result.auto_fix_plan == []
    assert result.findings == original.findings
