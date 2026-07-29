"""The packaged API plan-review gate consumes mechanical-check evidence."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.policy import Policy
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.orchestration.dsl import Ident, Member, Scope, evaluate, parse_expression
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from assurance_agent.workflow.orchestration.schema import GateDef, Verdict


def _gate() -> GateDef:
    return load_workflow_v2(Path.cwd()).gates["api-plan-review-gate"]


def test_packaged_gate_can_read_its_own_cycle_producer() -> None:
    compiled = compile_workflow(load_workflow_v2(Path.cwd()), load_execution_contracts(Path.cwd()))

    assert compiled.schema.gates["api-plan-review-gate"] == _gate()


def _review(**overrides: object) -> dict[str, object]:
    review = {
        "decision": "pass",
        "codegen_readiness": "ready",
        "required_capabilities": ["auth.api_admin_token"],
        "auto_fix_allowed": False,
        "human_review_required": False,
        "risk_level": "low",
    }
    review.update(overrides)
    return review


def _data_knowledge() -> dict[str, object]:
    return {
        "version": 1,
        "accounts": {},
        "auth": {"api_admin_token": {"method": "token"}},
        "entities": {},
        "capabilities": {
            "domain_factories": {},
            "adapters": {"api": {}, "e2e": {}, "fuzz": {}, "performance": {}},
            "cleanup": {},
        },
    }


def _context(
    tmp_path: Path,
    *,
    action: str,
    checks: object | None = None,
    review: dict[str, object] | None = None,
    node_results: dict[str, object] | None = None,
) -> GateEvaluationContext:
    policy_path = tmp_path / ".aa" / "policy.yaml"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(
        "\n".join(
            [
                "version: 1",
                "human_review_risk_levels: [high, critical]",
                "force_continue_allowed: true",
                f"plan_check_action: {action}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    overrides: dict[str, object] = {
        "review/api-plan-review.json": review or _review(),
        "repo:.aa/data-knowledge.yaml": _data_knowledge(),
    }
    if checks is not None:
        overrides["review/api-plan-checks.json"] = checks
    return GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={"force_continue": False},
        state_values={},
        node_results=node_results or {},
        artifact_overrides=overrides,
    )


def _adjudicate(
    tmp_path: Path,
    *,
    action: str,
    checks: object | None = None,
    review: dict[str, object] | None = None,
    node_results: dict[str, object] | None = None,
):
    return check_gate_in_view(
        {"api-plan-review-gate": _gate()},
        "api-plan-review-gate",
        _context(
            tmp_path,
            action=action,
            checks=checks,
            review=review,
            node_results=node_results,
        ),
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    [("warn", Verdict.PASS), ("block", Verdict.REJECT), ("require_human", Verdict.NEEDS_HUMAN_REVIEW)],
)
def test_packaged_gate_routes_failing_check_by_policy(tmp_path: Path, action: str, expected: Verdict) -> None:
    report = _adjudicate(tmp_path, action=action, checks={"status": "fail"})

    assert report.gate_id == "api-plan-review-gate"
    assert report.verdict == expected


@pytest.mark.parametrize("action", ["warn", "block", "require_human"])
def test_packaged_gate_keeps_passing_checks_inert(tmp_path: Path, action: str) -> None:
    assert _adjudicate(tmp_path, action=action, checks={"status": "pass"}).verdict == Verdict.PASS


@pytest.mark.parametrize("action", ["block", "require_human"])
def test_missing_checks_remain_compatible_without_a_current_producer(tmp_path: Path, action: str) -> None:
    """Historical/imported views have no frozen mechanical producer result."""
    assert _adjudicate(tmp_path, action=action).verdict == Verdict.PASS


@pytest.mark.parametrize(
    ("field", "action"),
    [("reject_when", "block"), ("needs_human_review_when", "require_human")],
)
def test_packaged_policy_branch_guard_returns_false_for_compatibility_absence(
    field: str, action: str
) -> None:
    rule = next(rule for rule in _gate().rules if rule.field == field)
    scope = Scope(
        {
            "api_plan_review": _review(),
            "api_plan_checks": None,
            "data_knowledge": _data_knowledge(),
            "params": {"force_continue": False},
            "policy": {
                "human_review_risk_levels": ["high", "critical"],
                "force_continue_allowed": True,
                "plan_check_action": action,
            },
        },
        capabilities_present=lambda _review_doc, _knowledge_doc: True,
    )

    assert evaluate(parse_expression(rule.expr), scope) is False


@pytest.mark.parametrize("checks", [None, {}])
def test_current_producer_requires_a_check_status(tmp_path: Path, checks: object | None) -> None:
    report = _adjudicate(
        tmp_path,
        action="warn",
        checks=checks,
        node_results={"mechanical-plan-checks": {"status": "succeeded"}},
    )

    assert report.verdict == Verdict.STOP
    assert report.matched_rule is not None and report.matched_rule.startswith("stop_when:")


def test_existing_needs_fix_precedes_a_blocking_check(tmp_path: Path) -> None:
    report = _adjudicate(
        tmp_path,
        action="block",
        checks={"status": "fail"},
        review=_review(decision="needs_fix", auto_fix_allowed=True),
    )

    assert report.verdict == Verdict.NEEDS_FIX
    assert report.matched_rule is not None and report.matched_rule.startswith("needs_fix_when:")


def _policy_fields(expression: object) -> set[str]:
    if isinstance(expression, Member):
        direct = (
            {expression.prop}
            if isinstance(expression.obj, Ident) and expression.obj.name == "policy"
            else set()
        )
    else:
        direct = set()
    if not is_dataclass(expression):
        return direct
    for field in fields(expression):
        value = getattr(expression, field.name)
        if isinstance(value, tuple):
            for item in value:
                direct.update(_policy_fields(item))
        else:
            direct.update(_policy_fields(value))
    return direct


def test_every_policy_field_has_a_parsed_runtime_consumer() -> None:
    consumed = {
        name
        for gate in load_workflow_v2(Path.cwd()).gates.values()
        for rule in gate.rules
        for name in _policy_fields(parse_expression(rule.expr))
    }

    assert consumed == set(Policy.model_fields) - {"version"}
