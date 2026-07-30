"""The packaged API/E2E plan-review gates consume v2 mechanical-check evidence."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.policy import KNOWN_PLAN_CHECK_IDS, Policy
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.orchestration.dsl import Ident, Member, Scope, evaluate, parse_expression
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from assurance_agent.workflow.orchestration.schema import GateDef, Verdict

_CHANGE_ID = "CH-1"
_EMPTY_DK: dict[str, object] = {
    "version": 1,
    "capabilities": {"domain_factories": {}},
}


def _gate(layer: str = "api") -> GateDef:
    profile = get_layer_assurance_profile(layer)
    return load_workflow_v2(Path.cwd()).gates[profile.gate_id]


def _codegen_gate(layer: str) -> GateDef:
    return load_workflow_v2(Path.cwd()).gates[f"{layer}-codegen-precondition-gate"]


def test_packaged_gate_can_read_its_own_cycle_producer() -> None:
    compiled = compile_workflow(load_workflow_v2(Path.cwd()), load_execution_contracts(Path.cwd()))
    assert compiled.schema.gates["api-plan-review-gate"] == _gate("api")


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


def _inapplicable_checks(layer: str) -> dict[str, object]:
    document = run_plan_checks(
        CheckContext(plan_texts={}, cases=(), data_knowledge=_EMPTY_DK, layer=layer)  # type: ignore[arg-type]
    )
    return document.model_dump(mode="json")


def _data_knowledge() -> dict[str, object]:
    return {
        "version": 1,
        "accounts": {},
        "auth": {"api_admin_token": {"method": "token", "symbol": "API_ADMIN_TOKEN"}},
        "entities": {},
        "capabilities": {
            "domain_factories": {},
            "adapters": {"api": {}, "e2e": {}, "fuzz": {}, "performance": {}},
            "cleanup": {},
        },
    }


def _review(layer: str = "api", **overrides: object) -> dict[str, object]:
    review = {
        "schema_version": "1.0",
        "decision": "pass",
        "review_type": f"{layer}-plan",
        "change_id": _CHANGE_ID,
        "codegen_readiness": "ready",
        "required_capabilities": ["auth.api_admin_token"],
        "auto_fix_allowed": False,
        "human_review_required": False,
        "risk_level": "low",
        "findings": [],
        "auto_fix_plan": [],
        "next_action": "continue",
    }
    review.update(overrides)
    return review


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


def _failed_checks(layer: str, *check_ids: str) -> dict[str, object]:
    payload = _applicable_checks(layer)
    for check in payload["checks"]:
        if check["check_id"] in check_ids:
            check["status"] = "fail"
            check["findings"] = [{"locator": check["check_id"], "actual": "bad", "expected": "good"}]
    payload["status"] = "fail"
    return payload


def _context(
    tmp_path: Path,
    *,
    layer: str = "api",
    check_actions: dict[str, str] | None = None,
    checks: object | None = None,
    review: dict[str, object] | None = None,
    node_results: dict[str, object] | None = None,
) -> GateEvaluationContext:
    profile = get_layer_assurance_profile(layer)
    policy_path = tmp_path / ".aa" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(_policy_text(check_actions=check_actions), encoding="utf-8")
    overrides: dict[str, object] = {
        profile.review_artifact: review or _review(layer),
        "repo:.aa/data-knowledge.yaml": _data_knowledge(),
    }
    if checks is not None:
        overrides[profile.checks_artifact] = checks
    return GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "qa" / "changes" / _CHANGE_ID,
        change_id=_CHANGE_ID,
        params={"force_continue": False},
        state_values={},
        node_results=node_results or {},
        artifact_overrides=overrides,
    )


def _adjudicate(
    tmp_path: Path,
    *,
    layer: str = "api",
    check_actions: dict[str, str] | None = None,
    checks: object | None = None,
    review: dict[str, object] | None = None,
    node_results: dict[str, object] | None = None,
):
    profile = get_layer_assurance_profile(layer)
    return check_gate_in_view(
        {profile.gate_id: _gate(layer)},
        profile.gate_id,
        _context(
            tmp_path,
            layer=layer,
            check_actions=check_actions,
            checks=checks,
            review=review,
            node_results=node_results,
        ),
    )


@pytest.mark.parametrize("layer", ["api", "e2e"])
@pytest.mark.parametrize(
    ("action", "expected"),
    [("warn", Verdict.PASS), ("block", Verdict.REJECT), ("require_human", Verdict.NEEDS_HUMAN_REVIEW)],
)
def test_packaged_gate_routes_failing_check_by_policy(
    tmp_path: Path, layer: str, action: str, expected: Verdict
) -> None:
    report = _adjudicate(
        tmp_path,
        layer=layer,
        check_actions={"assert_ideal": action},
        checks=_failed_checks(layer, "assert_ideal"),
    )
    assert report.verdict == expected


@pytest.mark.parametrize("layer", ["api", "e2e"])
@pytest.mark.parametrize("action", ["warn", "block", "require_human"])
def test_packaged_gate_keeps_passing_checks_inert(tmp_path: Path, layer: str, action: str) -> None:
    assert (
        _adjudicate(
            tmp_path,
            layer=layer,
            check_actions={"assert_ideal": action},
            checks=_applicable_checks(layer),
        ).verdict
        == Verdict.PASS
    )


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_inapplicable_scope_skips_without_review_or_l1(tmp_path: Path, layer: str) -> None:
    report = _adjudicate(tmp_path, layer=layer, checks=_inapplicable_checks(layer), review=None)
    assert report.verdict == Verdict.SKIP


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_invalid_checks_stop_before_policy_branches(tmp_path: Path, layer: str) -> None:
    broken = dict(_applicable_checks(layer))
    broken["schema_version"] = "1"
    report = _adjudicate(tmp_path, layer=layer, checks=broken)
    assert report.verdict == Verdict.STOP
    assert report.matched_rule is not None and report.matched_rule.startswith("stop_when:")


@pytest.mark.parametrize(
    ("review", "expected_verdict", "expected_target"),
    [
        (
            _review(decision="pass", codegen_readiness="ready"),
            Verdict.PASS,
            "END",
        ),
        (
            _review(decision="needs_fix", codegen_readiness="not_ready", auto_fix_allowed=True),
            Verdict.NEEDS_FIX,
            "fix",
        ),
        (
            _review(
                decision="needs_human_review",
                codegen_readiness="not_ready",
                auto_fix_allowed=False,
                human_review_required=True,
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
    report = _adjudicate(tmp_path, checks=_applicable_checks("api"), review=review)
    route = next(
        route
        for route in load_workflow_v2(Path.cwd()).graphs["api-plan-cycle"].routes
        if route.from_ == "review-gate"
    )
    assert report.verdict == expected_verdict
    assert route.cases[report.verdict.value] == expected_target


def test_explicit_reject_precedes_otherwise_matching_human_review_policy(tmp_path: Path) -> None:
    report = _adjudicate(
        tmp_path,
        check_actions={"assert_ideal": "require_human"},
        checks=_failed_checks("api", "assert_ideal"),
        review=_review(
            decision="reject",
            codegen_readiness="not_ready",
            human_review_required=True,
            risk_level="critical",
        ),
    )
    assert report.verdict == Verdict.REJECT
    assert report.matched_rule is not None and report.matched_rule.startswith("reject_when:")


def test_capability_absence_routes_to_knowledge_remediation_even_under_warn(tmp_path: Path) -> None:
    report = _adjudicate(
        tmp_path,
        check_actions={"capability_keys": "warn"},
        checks=_applicable_checks("api"),
        review=_review(required_capabilities=["capabilities.missing.leaf"]),
    )
    assert report.verdict == Verdict.NEEDS_HUMAN_REVIEW
    assert report.details is not None
    assert report.details.get("missing_capabilities")


def test_existing_needs_fix_precedes_a_blocking_check(tmp_path: Path) -> None:
    report = _adjudicate(
        tmp_path,
        check_actions={"assert_ideal": "block"},
        checks=_failed_checks("api", "assert_ideal"),
        review=_review(decision="needs_fix", auto_fix_allowed=True),
    )
    assert report.verdict == Verdict.NEEDS_FIX


def test_missing_checks_are_invalid_not_policy_compatible(tmp_path: Path) -> None:
    assert _adjudicate(tmp_path, check_actions={"assert_ideal": "block"}).verdict == Verdict.STOP


def test_codegen_precondition_skips_inapplicable_layer(tmp_path: Path) -> None:
    profile = get_layer_assurance_profile("api")
    context = _context(
        tmp_path,
        checks=_inapplicable_checks("api"),
        review=None,
        node_results={"review-cycle": {"status": "succeeded"}},
    )
    report = check_gate_in_view(
        load_workflow_v2(Path.cwd()).gates,
        "api-codegen-precondition-gate",
        context,
    )
    assert report.verdict == Verdict.SKIP


def test_codegen_precondition_stops_without_current_child_success(tmp_path: Path) -> None:
    (tmp_path / ".aa").mkdir(parents=True)
    (tmp_path / ".aa" / "data-knowledge.yaml").write_text("version: 1\ncapabilities:\n  domain_factories: {}\n", encoding="utf-8")
    context = _context(
        tmp_path,
        checks=_applicable_checks("api"),
        node_results={"review-cycle": {"status": "failed"}},
    )
    report = check_gate_in_view(
        load_workflow_v2(Path.cwd()).gates,
        "api-codegen-precondition-gate",
        context,
    )
    assert report.verdict == Verdict.STOP


def test_check_failed_builtin_is_false_for_missing_document() -> None:
    expr = parse_expression("check_failed(api_plan_checks, 'assert_ideal')")
    scope = Scope({"api_plan_checks": None})
    assert evaluate(expr, scope) is False


def test_check_failed_builtin_detects_named_failure() -> None:
    expr = parse_expression("check_failed(api_plan_checks, 'assert_ideal')")
    scope = Scope({"api_plan_checks": _failed_checks("api", "assert_ideal")})
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


REGISTERED_PYTHON_POLICY_CONSUMERS = frozenset({"evidence_sufficiency"})


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
