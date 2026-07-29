"""The packaged API plan-review gate consumes mechanical-check evidence."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.policy import KNOWN_PLAN_CHECK_IDS, Policy
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


def _policy_text(*, check_actions: dict[str, str] | None = None) -> str:
    actions = {check_id: "warn" for check_id in sorted(KNOWN_PLAN_CHECK_IDS)}
    if check_actions:
        actions.update(check_actions)
    lines = [
        "version: 1",
        "human_review_risk_levels: [high, critical]",
        "force_continue_allowed: true",
        "plan_checks:",
    ]
    for check_id, action in actions.items():
        lines.append(f"  {check_id}: {action}")
    lines.extend(
        [
            "coverage_floor:",
            "  risk_high: 0.9",
            "  risk_medium: 0.7",
            "fuzz:",
            "  required_when_endpoint_has_auth: true",
            "healing:",
            "  auth_module: require_human",
            "",
        ]
    )
    return "\n".join(lines)


def _failed_checks(*check_ids: str) -> dict[str, object]:
    checks = [
        {
            "check_id": check_id,
            "status": "fail",
            "findings": [{"locator": check_id, "actual": "bad", "expected": "good"}],
            "refs": [],
        }
        for check_id in check_ids
    ]
    return {"schema_version": "1", "status": "fail", "checks": checks}


def _passing_checks() -> dict[str, object]:
    return {
        "schema_version": "1",
        "status": "pass",
        "checks": [
            {"check_id": check_id, "status": "pass", "findings": [], "refs": []}
            for check_id in sorted(KNOWN_PLAN_CHECK_IDS)
        ],
    }


def _context(
    tmp_path: Path,
    *,
    check_actions: dict[str, str] | None = None,
    checks: object | None = None,
    review: dict[str, object] | None = None,
    node_results: dict[str, object] | None = None,
) -> GateEvaluationContext:
    policy_path = tmp_path / ".aa" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(_policy_text(check_actions=check_actions), encoding="utf-8")
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
    check_actions: dict[str, str] | None = None,
    checks: object | None = None,
    review: dict[str, object] | None = None,
    node_results: dict[str, object] | None = None,
):
    return check_gate_in_view(
        {"api-plan-review-gate": _gate()},
        "api-plan-review-gate",
        _context(
            tmp_path,
            check_actions=check_actions,
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
    report = _adjudicate(
        tmp_path,
        check_actions={"assert_ideal": action},
        checks=_failed_checks("assert_ideal"),
    )

    assert report.gate_id == "api-plan-review-gate"
    assert report.verdict == expected


@pytest.mark.parametrize("action", ["warn", "block", "require_human"])
def test_packaged_gate_keeps_passing_checks_inert(tmp_path: Path, action: str) -> None:
    assert (
        _adjudicate(
            tmp_path,
            check_actions={"assert_ideal": action},
            checks=_passing_checks(),
        ).verdict
        == Verdict.PASS
    )


@pytest.mark.parametrize(
    ("review", "expected_verdict", "expected_target"),
    [
        (
            _review(
                decision="pass",
                codegen_readiness="ready",
                auto_fix_allowed=False,
                human_review_required=False,
                next_action="continue",
            ),
            Verdict.PASS,
            "END",
        ),
        (
            _review(
                decision="needs_fix",
                codegen_readiness="not_ready",
                auto_fix_allowed=True,
                human_review_required=False,
                next_action="run_api_plan_fixer",
            ),
            Verdict.NEEDS_FIX,
            "fix",
        ),
        (
            _review(
                decision="needs_human_review",
                codegen_readiness="not_ready",
                auto_fix_allowed=False,
                human_review_required=True,
                next_action="human_review",
            ),
            Verdict.NEEDS_HUMAN_REVIEW,
            "human-review",
        ),
        (
            _review(
                decision="reject",
                codegen_readiness="not_ready",
                auto_fix_allowed=False,
                human_review_required=True,
                next_action="stop",
            ),
            Verdict.REJECT,
            "STOP",
        ),
    ],
    ids=("pass", "needs-fix", "needs-human-review", "reject"),
)
def test_packaged_gate_routes_every_documented_reviewer_decision(
    tmp_path: Path,
    review: dict[str, object],
    expected_verdict: Verdict,
    expected_target: str,
) -> None:
    report = _adjudicate(tmp_path, checks=_passing_checks(), review=review)
    route = next(
        route
        for route in load_workflow_v2(Path.cwd()).graphs["api-plan-cycle"].routes
        if route.from_ == "review"
    )

    assert report.verdict == expected_verdict
    assert route.cases[report.verdict.value] == expected_target


def test_explicit_reject_precedes_otherwise_matching_human_review_policy(tmp_path: Path) -> None:
    report = _adjudicate(
        tmp_path,
        check_actions={"assert_ideal": "require_human"},
        checks=_failed_checks("assert_ideal"),
        review=_review(
            decision="reject",
            codegen_readiness="not_ready",
            auto_fix_allowed=False,
            human_review_required=True,
            risk_level="critical",
            next_action="stop",
        ),
    )

    assert report.verdict == Verdict.REJECT
    assert report.matched_rule is not None and report.matched_rule.startswith("reject_when:")


def test_codegen_precondition_stops_when_plan_check_evidence_is_missing(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir(parents=True)
    (tmp_path / ".aa" / "data-knowledge.yaml").write_text("version: 1\n", encoding="utf-8")
    context = _context(tmp_path, check_actions={"assert_ideal": "block"})
    gates = load_workflow_v2(Path.cwd()).gates

    report = check_gate_in_view(gates, "api-codegen-precondition-gate", context)

    assert report.verdict == Verdict.STOP


@pytest.mark.parametrize("action", ["block", "require_human"])
def test_missing_checks_remain_compatible_without_a_current_producer(tmp_path: Path, action: str) -> None:
    """Historical/imported views have no frozen mechanical producer result."""
    assert _adjudicate(tmp_path, check_actions={"assert_ideal": action}).verdict == Verdict.PASS


@pytest.mark.parametrize(
    ("field", "action"),
    [("reject_when", "block"), ("needs_human_review_when", "require_human")],
)
def test_packaged_policy_branch_guard_returns_false_for_compatibility_absence(
    field: str, action: str
) -> None:
    rule = next(rule for rule in _gate().rules if rule.field == field)
    plan_checks = {check_id: "warn" for check_id in sorted(KNOWN_PLAN_CHECK_IDS)}
    plan_checks["assert_ideal"] = action
    scope = Scope(
        {
            "api_plan_review": _review(),
            "api_plan_checks": None,
            "data_knowledge": _data_knowledge(),
            "params": {"force_continue": False},
            "policy": {
                "human_review_risk_levels": ["high", "critical"],
                "force_continue_allowed": True,
                "plan_checks": plan_checks,
                "coverage_floor": {"risk_high": 0.9, "risk_medium": 0.7},
                "fuzz": {"required_when_endpoint_has_auth": True},
                "healing": {"auth_module": "require_human"},
            },
        },
        capabilities_present=lambda _review_doc, _knowledge_doc: True,
    )

    assert evaluate(parse_expression(rule.expr), scope) is False


@pytest.mark.parametrize("checks", [None, {}])
def test_current_producer_requires_a_check_status(tmp_path: Path, checks: object | None) -> None:
    report = _adjudicate(
        tmp_path,
        checks=checks,
        node_results={"mechanical-plan-checks": {"status": "succeeded"}},
    )

    assert report.verdict == Verdict.STOP
    assert report.matched_rule is not None and report.matched_rule.startswith("stop_when:")


def test_existing_needs_fix_precedes_a_blocking_check(tmp_path: Path) -> None:
    report = _adjudicate(
        tmp_path,
        check_actions={"assert_ideal": "block"},
        checks=_failed_checks("assert_ideal"),
        review=_review(decision="needs_fix", auto_fix_allowed=True),
    )

    assert report.verdict == Verdict.NEEDS_FIX
    assert report.matched_rule is not None and report.matched_rule.startswith("needs_fix_when:")


def test_check_failed_builtin_is_false_for_missing_document() -> None:
    expr = parse_expression("check_failed(api_plan_checks, 'assert_ideal')")
    scope = Scope({"api_plan_checks": None})
    assert evaluate(expr, scope) is False


def test_check_failed_builtin_detects_named_failure() -> None:
    expr = parse_expression("check_failed(api_plan_checks, 'assert_ideal')")
    scope = Scope({"api_plan_checks": _failed_checks("assert_ideal")})
    assert evaluate(expr, scope) is True


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


# Python-side policy consumers that are not referenced in gate DSL expressions.
# Each entry must name the runtime symbol and why DSL cannot express the rule.
REGISTERED_PYTHON_POLICY_CONSUMERS = frozenset(
    {
        # evidence.sufficiency.evaluate_sufficiency + runner coverage gate (Task 8/9)
        "evidence_sufficiency",
    }
)


def _consumed_policy_fields() -> set[str]:
    return {
        name
        for gate in load_workflow_v2(Path.cwd()).gates.values()
        for rule in gate.rules
        for name in _policy_fields(parse_expression(rule.expr))
    }


def test_every_policy_field_has_a_runtime_consumer() -> None:
    consumed = _consumed_policy_fields()
    expected = set(Policy.model_fields) - {"version"}

    assert consumed | REGISTERED_PYTHON_POLICY_CONSUMERS == expected


def test_registered_python_consumers_must_name_real_policy_fields() -> None:
    expected = set(Policy.model_fields) - {"version"}
    assert REGISTERED_PYTHON_POLICY_CONSUMERS <= expected


def test_guard_fails_when_a_policy_field_lacks_any_consumer() -> None:
    consumed = _consumed_policy_fields()
    expected = set(Policy.model_fields) - {"version"}
    uncovered = expected - consumed - REGISTERED_PYTHON_POLICY_CONSUMERS

    assert not uncovered, f"policy fields without runtime consumers: {sorted(uncovered)}"
