from __future__ import annotations

from assurance_generation.operations.plan_review_policy import (
    apply_plan_review_policy,
    load_finding_scope,
    write_finding_scope,
)


def _finding(finding_id: str) -> dict[str, object]:
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


def _auto_fix(*findings: dict[str, object]) -> dict[str, object]:
    ids = [str(item["id"]) for item in findings]
    return {
        "schema_version": "1.0",
        "review_type": "api-codegen",
        "change_id": "CH-DEMO-001",
        "route": "auto_fix",
        "findings": list(findings),
        "finding_ids": ids,
        "next_action": "run api planner",
        "risk_level": "high",
        "required_capabilities": ["entities.item.create"],
    }


def test_policy_does_not_strip_or_stick_prior_scope() -> None:
    runner = {
        **_finding("API-PLAN-001"),
        "category": "test-runner",
        "message": "Markers: none required contradicts asyncio_mode",
        "locator": {"artifact": "qa/results/plans/api-codegen-plan.md", "key": "7. Run Guidance"},
    }
    semantic = _finding("API-PLAN-002")
    kept = apply_plan_review_policy(
        _auto_fix(runner, semantic),
        previous={"route": "codegen", "finding_ids": []},
    )
    assert kept["route"] == "auto_fix"
    assert [item["id"] for item in kept["findings"]] == ["API-PLAN-001", "API-PLAN-002"]
    assert kept["finding_ids"] == ["API-PLAN-001", "API-PLAN-002"]


def test_finding_scope_roundtrip(tmp_path) -> None:
    write_finding_scope(
        tmp_path,
        family="api",
        coverage_epoch=2,
        change_id="CH-DEMO-001",
        route="auto_fix",
        finding_ids=("API-PLAN-002",),
    )
    assert load_finding_scope(tmp_path, family="api", coverage_epoch=2) == {
        "route": "auto_fix",
        "finding_ids": ["API-PLAN-002"],
    }
    assert load_finding_scope(tmp_path, family="api", coverage_epoch=3) is None
