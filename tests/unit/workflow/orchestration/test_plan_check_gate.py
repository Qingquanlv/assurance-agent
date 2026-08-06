"""The packaged API plan-review gate consumes mechanical-check evidence."""

from __future__ import annotations

import importlib
import inspect
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


def _dsl_consumed_policy_fields() -> set[str]:
    return {
        name
        for gate in load_workflow_v2(Path.cwd()).gates.values()
        for rule in gate.rules
        for name in _policy_fields(parse_expression(rule.expr))
    }


# Policy fields whose only consumer is Python, not a gate expression, mapped to the
# ``module::symbol`` that reads them so every registration is auditable by name instead of
# being a bare opt-out. A field consumed nowhere and registered nowhere stays fail-closed.
# Faking a DSL reference to pass the guard is forbidden — that is the "fake interface" this
# registry exists to avoid.
#
# Empty since M1's ``trace-sufficiency-gate`` landed: ``evidence_sufficiency`` was the one
# entry, registered while sufficiency had no gate to route it, and that gate now DSL-reads
# ``policy.evidence_sufficiency.on_insufficient`` directly. Nested ``floors`` / ``cadence`` /
# ``mutation_budget_seconds`` are consumed by ``evaluate_metrics_sufficiency``, which
# ``metrics-sufficiency-gate`` calls (no tautological DSL). The registry stays because the
# guard needs somewhere to record the next such field — and because an empty registry is
# the strongest statement of the closure: every live policy field has a gate expression.
REGISTERED_PYTHON_POLICY_CONSUMERS: dict[str, str] = {}

# Deprecated compat field: must remain loadable, must gain no new consumers (§8 / §2.2).
DEPRECATED_UNCONSUMED_POLICY_FIELDS = frozenset({"coverage_floor"})

# Nested evidence_sufficiency fields that a top-level DSL mention of
# ``policy.evidence_sufficiency`` does *not* prove are consumed. Each must be named by
# the auditable evaluator wired into metrics-sufficiency-gate.
METRICS_POLICY_EVALUATOR = "assurance_agent.evidence.metrics_sufficiency::evaluate_metrics_sufficiency"
METRICS_POLICY_NESTED_FIELDS = frozenset({"floors", "cadence", "mutation_budget_seconds"})


def _unaudited_policy_fields(field_names: set[str], *, registered: set[str] | None = None) -> set[str]:
    audited = set(REGISTERED_PYTHON_POLICY_CONSUMERS) if registered is None else registered
    return (
        field_names
        - {"version"}
        - DEPRECATED_UNCONSUMED_POLICY_FIELDS
        - _dsl_consumed_policy_fields()
        - audited
    )


def _registration_reads_its_field(target: str, field: str) -> bool:
    """Whether the registered ``module::symbol`` actually names ``field``.

    Source inspection, not an import check: importing proves the consumer exists,
    while its source is the only place that can show it reads this policy field
    (`policy.evidence_sufficiency`, here). The audit stays wholly on the test
    side — production code carries no marker for it.
    """
    module, _, symbol = target.partition("::")
    return field in inspect.getsource(getattr(importlib.import_module(module), symbol))


def _unknown_dsl_policy_roots(consumed: set[str], field_names: set[str]) -> set[str]:
    """``policy.<root>`` references that no live Policy field backs.

    Catches typos and references left behind by a renamed or deleted field. ``version`` is
    loader bookkeeping, never a gate constant, so referencing it is also an error.
    """
    return consumed - (field_names - {"version"})


def test_every_policy_field_has_a_parsed_runtime_consumer() -> None:
    assert _unaudited_policy_fields(set(Policy.model_fields)) == set()


def test_a_field_consumed_nowhere_and_registered_nowhere_is_reported() -> None:
    assert _unaudited_policy_fields(set(Policy.model_fields) | {"deploy_on_friday"}) == {"deploy_on_friday"}


def test_every_policy_field_is_now_consumed_by_a_gate_expression() -> None:
    """The registry is empty because the DSL closed the last live gap.

    ``evidence_sufficiency`` was the one Python-only field; M1's
    ``trace-sufficiency-gate`` reads ``on_insufficient`` in its own rules, which is
    what allowed the registration to be deleted rather than the guard relaxed.
    ``coverage_floor`` is deprecated and explicitly unconsumed (see below).
    """
    assert "evidence_sufficiency" in _dsl_consumed_policy_fields()
    assert "coverage_floor" not in _dsl_consumed_policy_fields()
    assert _unaudited_policy_fields(set(Policy.model_fields), registered=set()) == set()


def test_deprecated_coverage_floor_gains_no_new_consumers() -> None:
    """§8: keep the field for compat; forbid new DSL or Python consumers.

    Bidirectional with the live-field guard: ``coverage_floor`` is exempt from
    ``_unaudited_policy_fields`` *and* must stay out of both consumer channels.
    """
    assert "coverage_floor" in DEPRECATED_UNCONSUMED_POLICY_FIELDS
    assert "coverage_floor" not in _dsl_consumed_policy_fields()
    assert "coverage_floor" not in REGISTERED_PYTHON_POLICY_CONSUMERS
    assert "coverage_floor" not in _unaudited_policy_fields(set(Policy.model_fields))


def test_a_python_only_field_is_audited_only_while_it_stays_registered() -> None:
    """The registry still works, now that nothing is in it.

    Checked with an injected field rather than a live one, so the guard's two
    channels stay demonstrably distinct: registering silences the report, and
    dropping the registration brings it back.
    """
    fields_with_extra = set(Policy.model_fields) | {"deploy_on_friday"}

    assert _unaudited_policy_fields(fields_with_extra, registered={"deploy_on_friday"}) == set()
    assert _unaudited_policy_fields(fields_with_extra, registered=set()) == {"deploy_on_friday"}


def test_no_gate_expression_references_a_missing_policy_field() -> None:
    """The other closure direction: every DSL `policy.<root>` must be a real field."""
    assert _unknown_dsl_policy_roots(_dsl_consumed_policy_fields(), set(Policy.model_fields)) == set()


@pytest.mark.parametrize("root", ["coverage_flooor", "version"], ids=("misspelled", "not-a-constant"))
def test_a_dsl_root_outside_the_gate_constant_surface_is_reported(root: str) -> None:
    assert _unknown_dsl_policy_roots({root}, set(Policy.model_fields)) == {root}


def test_metrics_nested_policy_fields_are_named_by_the_evaluator() -> None:
    """Top-level DSL on ``evidence_sufficiency`` only proves ``on_insufficient``.

    Floors/cadence/budget must be named by the Task-8-bound evaluator, or they
    are dead constants the way ``coverage_floor`` was.
    """
    for field in METRICS_POLICY_NESTED_FIELDS:
        assert _registration_reads_its_field(METRICS_POLICY_EVALUATOR, field)


def test_python_registrations_name_real_policy_fields() -> None:
    assert set(REGISTERED_PYTHON_POLICY_CONSUMERS) <= set(Policy.model_fields) - {"version"}


def test_python_registrations_name_an_importable_symbol() -> None:
    """A registration must name a consumer that actually exists.

    The shape check alone would let a renamed or deleted consumer keep vouching for its
    field, so the named symbol is imported here: the registry states a checked fact.
    """
    for field, target in REGISTERED_PYTHON_POLICY_CONSUMERS.items():
        module, sep, symbol = target.partition("::")
        assert sep == "::", f"{field} registration must be 'module::symbol', got {target!r}"
        assert module.startswith("assurance_agent.") and symbol, target
        imported = importlib.import_module(module)
        assert hasattr(imported, symbol), f"{field} registers {target}, which no longer exists"


def test_every_python_registration_reads_the_field_it_claims() -> None:
    """The consumer's own source must mention the field it is registered for.

    Importing the symbol proves it exists; it does not prove it reads this policy
    field. Reading the symbol's source closes that gap from the test side only —
    no marker, decorator or registry is added to production code, so the
    consuming module stays unaware that a guard audits it.
    """
    for field, target in REGISTERED_PYTHON_POLICY_CONSUMERS.items():
        assert _registration_reads_its_field(target, field), (
            f"{target} no longer mentions policy.{field}; point the registration at the "
            "symbol that now reads it, or drop the registration"
        )


def test_a_registration_naming_a_symbol_that_ignores_the_field_is_reported() -> None:
    """The audit is not vacuously true: a real symbol that never names the field
    (here a DTO in the very module that does consume it) is rejected."""
    assert not _registration_reads_its_field(
        "assurance_agent.evidence.sufficiency::RowVerdict", "evidence_sufficiency"
    )


def test_python_registrations_do_not_duplicate_a_dsl_consumer() -> None:
    """Hygiene for today's split, not an architectural invariant.

    The registry exists only for fields the gate DSL cannot reach. So when M1's dedicated
    trace/metrics gate genuinely DSL-consumes `evidence_sufficiency`, the required
    transition is to delete its Python registration here — not to relax this test.
    """
    double_booked = sorted(set(REGISTERED_PYTHON_POLICY_CONSUMERS) & _dsl_consumed_policy_fields())
    assert not double_booked, (
        f"{double_booked} now have gate expressions reading them; drop their Python "
        "registration instead of keeping both channels on the books"
    )


def test_policy_values_move_the_metrics_and_trace_truth_tables() -> None:
    """§12.8: a policy reference must change the verdict, not be a tautology.

    Floors/cadence/``on_insufficient`` via the metrics evaluator (Task 8 handoff);
    trace ``on_insufficient`` via the packaged trace-sufficiency-gate DSL.
    ``mutation_budget_seconds`` is only echoed on the decision (§5-B1), not a
    verdict flip — asserted separately from the truth-table rows.
    """
    from datetime import UTC, datetime

    from assurance_agent.artifacts.models.metrics import MetricEntry, MetricScope, MetricsDocument
    from assurance_agent.artifacts.models.policy import MetricCadenceSchedule, MetricFloor
    from assurance_agent.artifacts.policy import load_policy_bytes
    from assurance_agent.evidence.metrics_sufficiency import evaluate_metrics_sufficiency

    sufficiency = load_policy_bytes(None, origin="packaged").evidence_sufficiency
    document = MetricsDocument(
        schema_version="2",
        change_id="CH-1",
        cadence="pr",
        computed_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        risk_tier="low",
        risk_tier_lower_bound="low",
        risk_tier_declared=None,
        risk_declaration_lowered=False,
        risk_lowered_declarations=(),
        metrics={
            "constraint_coverage": MetricEntry(
                layer="api",
                status="evaluated",
                value=0.6,
                declared=MetricScope.of(total=5, covered=3),
                evidence="constraint-coverage.json",
            )
        },
        policy_digest="0" * 64,
    )

    assert evaluate_metrics_sufficiency(document, sufficiency).verdict == "pass"

    raised = {tier: dict(band) for tier, band in sufficiency.floors.items()}
    raised["low"] = {
        **raised["low"],
        "constraint_coverage": MetricFloor(target="value", min=1.0),
    }
    assert (
        evaluate_metrics_sufficiency(document, sufficiency.model_copy(update={"floors": raised})).verdict
        == "needs_human"
    )

    # Numeric miss disposition follows on_insufficient (§6).
    assert (
        evaluate_metrics_sufficiency(
            document,
            sufficiency.model_copy(update={"floors": raised, "on_insufficient": "warn"}),
        ).verdict
        == "pass"
    )
    assert (
        evaluate_metrics_sufficiency(
            document,
            sufficiency.model_copy(update={"floors": raised, "on_insufficient": "block"}),
        ).verdict
        == "stop"
    )

    no_constraint = MetricCadenceSchedule(
        pr=["diff_coverage", "auth_matrix_coverage", "journey_coverage", "threshold_slack"],
        nightly=list(sufficiency.cadence.nightly),
    )
    assert (
        evaluate_metrics_sufficiency(
            document,
            sufficiency.model_copy(update={"floors": raised, "cadence": no_constraint}),
        ).verdict
        == "pass"
    )

    empty_pr = MetricCadenceSchedule(pr=[], nightly=list(sufficiency.cadence.nightly))
    assert (
        evaluate_metrics_sufficiency(document, sufficiency.model_copy(update={"cadence": empty_pr})).verdict
        == "skipped"
    )

    # Empty cadence must not swallow an evaluated boolean stop.
    dirty = MetricsDocument(
        schema_version="2",
        change_id="CH-1",
        cadence="pr",
        computed_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        risk_tier="critical",
        risk_tier_lower_bound="critical",
        risk_tier_declared=None,
        risk_declaration_lowered=False,
        risk_lowered_declarations=(),
        metrics={
            "adversarial_clean": MetricEntry(
                layer="cross",
                status="evaluated",
                value=None,
                declared=None,
                holds=False,
                evidence="adversarial.json",
            )
        },
        policy_digest="0" * 64,
    )
    assert (
        evaluate_metrics_sufficiency(dirty, sufficiency.model_copy(update={"cadence": empty_pr})).verdict
        == "stop"
    )

    budgeted = evaluate_metrics_sufficiency(
        document, sufficiency.model_copy(update={"mutation_budget_seconds": 90})
    )
    assert budgeted.mutation_budget_seconds == 90
    assert budgeted.verdict == "pass"

    # Trace on_insufficient: packaged DSL, not a constant true.
    gate = load_workflow_v2(Path.cwd()).gates["trace-sufficiency-gate"]
    rule = next(rule for rule in gate.rules if rule.field == "needs_human_review_when")
    thin = {
        "schema_version": "1",
        "change_id": "CH-1",
        "authoritative_batch_id": "b",
        "policy_digest": "0" * 64,
        "as_of": "2026-07-02T11:11:11+00:00",
        "integrity": "complete",
        "integrity_blocks_routing": False,
        "sufficient": False,
        "has_open_problems": False,
        "error_code": None,
        "insufficient_cases": [{"case_id": "TC_API_001", "reason_codes": ["never_run"]}],
        "gap_codes": [],
    }
    require_human = {
        "human_review_risk_levels": ["high"],
        "force_continue_allowed": True,
        "plan_checks": {check_id: "warn" for check_id in sorted(KNOWN_PLAN_CHECK_IDS)},
        "coverage_floor": {"risk_high": 0.9, "risk_medium": 0.7},
        "fuzz": {"required_when_endpoint_has_auth": True},
        "healing": {"auth_module": "require_human"},
        "evidence_sufficiency": {
            **sufficiency.model_dump(mode="json"),
            "on_insufficient": "require_human",
        },
    }
    warn = {
        **require_human,
        "evidence_sufficiency": {**require_human["evidence_sufficiency"], "on_insufficient": "warn"},
    }
    expr = parse_expression(rule.expr)
    assert evaluate(expr, Scope({"trace": thin, "policy": require_human})) is True
    assert evaluate(expr, Scope({"trace": thin, "policy": warn})) is False
