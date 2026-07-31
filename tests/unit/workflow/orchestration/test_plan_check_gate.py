"""The packaged API/E2E plan-review gates consume v2 mechanical-check evidence."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.policy import KNOWN_PLAN_CHECK_IDS, Policy
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.orchestration.dsl import Ident, Member, Scope, evaluate, parse_expression
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from assurance_agent.workflow.orchestration.schema import GateDef, Verdict, derive_alias, normalize_gates

_CHANGE_ID = "CH-1"
_FUZZ_PERF_FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "assurance" / "fuzz-performance-gates.yaml"
)
_CANONICAL_FUZZ_PERF_GATE_IDS = frozenset(
    {
        "fuzz-plan-review-gate",
        "performance-plan-review-gate",
        "fuzz-codegen-precondition-gate",
        "performance-codegen-precondition-gate",
    }
)
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
    checks = payload["checks"]
    assert isinstance(checks, list)
    for check in checks:
        assert isinstance(check, dict)
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
        review=_review(required_capabilities=["auth.missing_token"]),
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
    (tmp_path / ".aa" / "data-knowledge.yaml").write_text(
        "version: 1\ncapabilities:\n  domain_factories: {}\n", encoding="utf-8"
    )
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


def _load_fixture_gates() -> dict[str, GateDef]:
    raw = yaml.safe_load(_FUZZ_PERF_FIXTURE.read_text(encoding="utf-8"))
    return normalize_gates(raw)


def _fixture_gate(layer: str) -> GateDef:
    profile = get_layer_assurance_profile(layer)
    return _load_fixture_gates()[profile.gate_id]


def _fixture_codegen_gate(layer: str) -> GateDef:
    return _load_fixture_gates()[f"{layer}-codegen-precondition-gate"]


def _fixture_adjudicate(
    tmp_path: Path,
    *,
    layer: str,
    check_actions: dict[str, str] | None = None,
    checks: object | None = None,
    review: dict[str, object] | None = None,
    node_results: dict[str, object] | None = None,
):
    profile = get_layer_assurance_profile(layer)
    fixture_gates = _load_fixture_gates()
    return check_gate_in_view(
        fixture_gates,
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


def test_fixture_defines_exactly_four_canonical_fuzz_performance_gates() -> None:
    gates = _load_fixture_gates()
    assert set(gates) == _CANONICAL_FUZZ_PERF_GATE_IDS


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_plan_gate_reads_profile_canonical_artifacts(layer: str) -> None:
    profile = get_layer_assurance_profile(layer)
    gate = _fixture_gate(layer)
    reads_by_path = {entry.path: entry.alias for entry in gate.reads}
    assert reads_by_path == {
        profile.review_artifact: profile.review_alias,
        profile.checks_artifact: derive_alias(profile.checks_artifact),
        "repo:.aa/data-knowledge.yaml": "data_knowledge",
    }
    assert gate.invalid_json == Verdict.STOP
    assert gate.missing_field_is == Verdict.STOP


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_codegen_gate_reads_all_three_artifacts(layer: str) -> None:
    profile = get_layer_assurance_profile(layer)
    gate = _fixture_codegen_gate(layer)
    reads_by_path = {entry.path: entry.alias for entry in gate.reads}
    assert reads_by_path == {
        profile.review_artifact: profile.review_alias,
        profile.checks_artifact: derive_alias(profile.checks_artifact),
        "repo:.aa/data-knowledge.yaml": "data_knowledge",
    }


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_invalid_assurance_state_stops(tmp_path: Path, layer: str) -> None:
    broken = dict(_applicable_checks(layer))
    broken["schema_version"] = "1"
    report = _fixture_adjudicate(tmp_path, layer=layer, checks=broken)
    assert report.verdict == Verdict.STOP
    assert report.matched_rule is not None and report.matched_rule.startswith("stop_when:")


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_inapplicable_scope_skips(tmp_path: Path, layer: str) -> None:
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        checks=_inapplicable_checks(layer),
        review=None,
    )
    assert report.verdict == Verdict.SKIP


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_needs_fix_without_auto_fix_allowed(tmp_path: Path, layer: str) -> None:
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        checks=_applicable_checks(layer),
        review=_review(layer, decision="needs_fix", auto_fix_allowed=False),
    )
    assert report.verdict == Verdict.NEEDS_FIX


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
@pytest.mark.parametrize("action", ["warn", "block", "require_human"])
def test_fixture_missing_capability_routes_to_knowledge_remediation(
    tmp_path: Path, layer: str, action: str
) -> None:
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        check_actions={"capability_keys": action},
        checks=_applicable_checks(layer),
        review=_review(layer, required_capabilities=["auth.missing_token"]),
    )
    assert report.verdict == Verdict.NEEDS_HUMAN_REVIEW
    assert report.details is not None
    assert report.details.get("missing_capabilities")


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_explicit_reject_precedes_high_risk_human_review(tmp_path: Path, layer: str) -> None:
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        check_actions={"l1_path": "require_human"},
        checks=_failed_checks(layer, "l1_path"),
        review=_review(
            layer,
            decision="reject",
            codegen_readiness="not_ready",
            human_review_required=True,
            risk_level="critical",
        ),
    )
    assert report.verdict == Verdict.REJECT
    assert report.matched_rule is not None and report.matched_rule.startswith("reject_when:")


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
@pytest.mark.parametrize(
    ("review_overrides", "check_actions", "failed_checks"),
    [
        ({"decision": "changes_requested"}, None, ()),
        ({"decision": "needs_human_review"}, None, ()),
        ({"decision": "pass", "human_review_required": True, "risk_level": "high"}, None, ()),
        ({"decision": "pass"}, {"l1_path": "require_human"}, ("l1_path",)),
    ],
    ids=("changes-requested", "needs-human-review", "configured-risk", "require-human-check"),
)
def test_fixture_routes_to_human_review(
    tmp_path: Path,
    layer: str,
    review_overrides: dict[str, object],
    check_actions: dict[str, str] | None,
    failed_checks: tuple[str, ...],
) -> None:
    checks = _applicable_checks(layer)
    if failed_checks:
        checks = _failed_checks(layer, *failed_checks)
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        check_actions=check_actions,
        checks=checks,
        review=_review(layer, **review_overrides),
    )
    assert report.verdict == Verdict.NEEDS_HUMAN_REVIEW


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
@pytest.mark.parametrize(
    ("review_overrides", "check_actions", "failed_checks"),
    [
        ({"decision": "reject"}, None, ()),
        ({"decision": "pass", "codegen_readiness": "not_ready"}, None, ()),
        ({"decision": "pass"}, {"l1_path": "block"}, ("l1_path",)),
    ],
    ids=("explicit-reject", "not-ready", "block-check"),
)
def test_fixture_routes_to_reject(
    tmp_path: Path,
    layer: str,
    review_overrides: dict[str, object],
    check_actions: dict[str, str] | None,
    failed_checks: tuple[str, ...],
) -> None:
    checks = _applicable_checks(layer)
    if failed_checks:
        checks = _failed_checks(layer, *failed_checks)
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        check_actions=check_actions,
        checks=checks,
        review=_review(layer, **review_overrides),
    )
    assert report.verdict == Verdict.REJECT


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_passes_ready_review_with_present_capabilities(tmp_path: Path, layer: str) -> None:
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        checks=_applicable_checks(layer),
        review=_review(layer, decision="pass", codegen_readiness="ready"),
    )
    assert report.verdict == Verdict.PASS


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_force_continue_never_overrides_reject_or_missing_capability(
    tmp_path: Path, layer: str
) -> None:
    reject_report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        checks=_applicable_checks(layer),
        review=_review(
            layer,
            decision="reject",
            human_review_required=True,
            risk_level="low",
            codegen_readiness="ready",
        ),
    )
    assert reject_report.verdict == Verdict.REJECT

    missing_cap_report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        checks=_applicable_checks(layer),
        review=_review(
            layer,
            decision="pass",
            human_review_required=True,
            risk_level="low",
            codegen_readiness="ready",
            required_capabilities=["capabilities.missing.leaf"],
        ),
    )
    assert missing_cap_report.verdict == Verdict.NEEDS_HUMAN_REVIEW


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_force_continue_suppresses_configured_risk_when_allowed(
    tmp_path: Path, layer: str
) -> None:
    context = _context(
        tmp_path,
        layer=layer,
        checks=_applicable_checks(layer),
        review=_review(
            layer,
            decision="pass",
            human_review_required=True,
            risk_level="low",
            codegen_readiness="ready",
        ),
    )
    context = replace(context, params={**context.params, "force_continue": True})
    profile = get_layer_assurance_profile(layer)
    report = check_gate_in_view(_load_fixture_gates(), profile.gate_id, context)
    assert report.verdict == Verdict.PASS


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_approved_is_not_pass(tmp_path: Path, layer: str) -> None:
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        checks=_applicable_checks(layer),
        review=_review(layer, decision="approved", codegen_readiness="ready"),
    )
    assert report.verdict != Verdict.PASS


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_malformed_review_stops(tmp_path: Path, layer: str) -> None:
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        checks=_applicable_checks(layer),
        review={"decision": "pass"},
    )
    assert report.verdict == Verdict.STOP


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_malformed_l1_stops(tmp_path: Path, layer: str) -> None:
    context = _context(tmp_path, layer=layer, checks=_applicable_checks(layer))
    profile = get_layer_assurance_profile(layer)
    context = replace(
        context,
        artifact_overrides={
            **context.artifact_overrides,
            "repo:.aa/data-knowledge.yaml": {"version": "bad"},
        },
    )
    report = check_gate_in_view(_load_fixture_gates(), profile.gate_id, context)
    assert report.verdict == Verdict.STOP


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_unmatched_combination_defaults_to_stop(tmp_path: Path, layer: str) -> None:
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        checks=_applicable_checks(layer),
        review=_review(layer, decision="pass", codegen_readiness="pending"),
    )
    assert report.verdict == Verdict.STOP


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
@pytest.mark.parametrize(
    ("check_id", "action", "expected"),
    [
        ("assert_ideal", "block", Verdict.PASS),
        ("assert_ideal", "require_human", Verdict.PASS),
        ("l1_path", "warn", Verdict.PASS),
        ("l1_path", "block", Verdict.REJECT),
        ("l1_path", "require_human", Verdict.NEEDS_HUMAN_REVIEW),
        ("shared_factory", "block", Verdict.REJECT),
        ("capability_keys", "require_human", Verdict.NEEDS_HUMAN_REVIEW),
    ],
)
def test_fixture_check_actions_respect_profile_applicability(
    tmp_path: Path,
    layer: str,
    check_id: str,
    action: str,
    expected: Verdict,
) -> None:
    if check_id == "assert_ideal":
        checks = _applicable_checks(layer)
    else:
        checks = _failed_checks(layer, check_id)
    report = _fixture_adjudicate(
        tmp_path,
        layer=layer,
        check_actions={check_id: action},
        checks=checks,
        review=_review(layer),
    )
    assert report.verdict == expected


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_codegen_precondition_skips_inapplicable_layer(tmp_path: Path, layer: str) -> None:
    context = _context(
        tmp_path,
        layer=layer,
        checks=_inapplicable_checks(layer),
        review=None,
        node_results={"review-cycle": {"status": "succeeded"}},
    )
    report = check_gate_in_view(
        _load_fixture_gates(),
        f"{layer}-codegen-precondition-gate",
        context,
    )
    assert report.verdict == Verdict.SKIP


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_codegen_precondition_stops_without_current_child_success(
    tmp_path: Path, layer: str
) -> None:
    (tmp_path / ".aa").mkdir(parents=True)
    (tmp_path / ".aa" / "data-knowledge.yaml").write_text(
        "version: 1\ncapabilities:\n  domain_factories: {}\n", encoding="utf-8"
    )
    context = _context(
        tmp_path,
        layer=layer,
        checks=_applicable_checks(layer),
        node_results={"review-cycle": {"status": "failed"}},
    )
    report = check_gate_in_view(
        _load_fixture_gates(),
        f"{layer}-codegen-precondition-gate",
        context,
    )
    assert report.verdict == Verdict.STOP
