"""Gate expression corpus against packaged schema_version \"2\"."""

from pathlib import Path
from typing import Any

import pytest

from assurance_agent.artifacts.models.policy import Policy
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.orchestration.dsl import MISSING as MISS
from assurance_agent.workflow.orchestration.dsl import Scope, evaluate, parse_expression
from assurance_agent.workflow.orchestration.schema import GateDef


def _collect(gates: dict[str, GateDef]) -> dict[str, str]:
    out: dict[str, str] = {}
    for gid, g in gates.items():
        for r in g.rules:
            out[f"gate:{gid}:{r.field}"] = r.expr
    return out


def gv(v):
    return {"gate_verdict": lambda _i, _v=v: _v}


def fx(b):
    return {"file_exists": lambda _p, _b=b: _b}


def gvfx(v, b):
    return {"gate_verdict": lambda _i, _v=v: _v, "file_exists": lambda _p, _b=b: _b}


def node_result_resolver(results):
    return {"node_result": lambda node_id, _results=results: _results.get(node_id, {})}


_DEFAULT_POLICY = {
    "human_review_risk_levels": ["high", "critical"],
    "force_continue_allowed": True,
    "plan_checks": {
        "l1_path": "warn",
        "shared_factory": "warn",
        "assert_ideal": "warn",
        "capability_keys": "warn",
    },
    # Deprecated compat binding only — no gate expression may read it (§8).
    "coverage_floor": {"risk_high": 0.9, "risk_medium": 0.7},
    "fuzz": {"required_when_endpoint_has_auth": True},
    "healing": {"auth_module": "require_human"},
    # `trace-sufficiency-gate` reads `on_insufficient` from here; floors/cadence are
    # carried so the scope stays a complete stand-in for a loaded policy.
    "evidence_sufficiency": {
        "recency_hours": 72,
        "required_kinds": {
            "API": ["covered", "execution_recent"],
            "E2E": ["covered", "execution_recent"],
            "Fuzz": ["covered", "fuzz_run"],
            "Performance": ["covered", "perf_run"],
        },
        "on_insufficient": "require_human",
        "floors": {
            "low": {
                "constraint_coverage": {"target": "value", "min": 0.5},
                "auth_matrix_coverage": {"target": "touched", "min": 1.0},
            },
            "medium": {
                "constraint_coverage": {"target": "value", "min": 0.7},
                "auth_matrix_coverage": {"target": "touched", "min": 1.0},
                "journey_coverage": {"target": "touched", "min": 1.0},
            },
            "high": {
                "constraint_coverage": {"target": "touched", "min": 1.0},
                "auth_matrix_coverage": {"target": "touched", "min": 1.0},
                "journey_coverage": {"target": "touched", "min": 1.0},
            },
            "critical": {
                "constraint_coverage": {"target": "touched", "min": 1.0},
                "auth_matrix_coverage": {"target": "touched", "min": 1.0},
                "journey_coverage": {"target": "touched", "min": 1.0},
                "adversarial_clean": {"target": "holds", "must_hold": True},
            },
        },
        "cadence": {
            "pr": [
                "diff_coverage",
                "constraint_coverage",
                "auth_matrix_coverage",
                "journey_coverage",
                "threshold_slack",
            ],
            "nightly": [
                "mutation_score",
                "assertion_strength",
                "adversarial_yield",
                "baseline_drift",
            ],
        },
        "mutation_budget_seconds": 300,
    },
}


def _scope(values, resolvers: dict[str, Any]):
    return Scope(
        {"policy": _DEFAULT_POLICY, **values},
        file_exists=resolvers.get("file_exists"),
        gate_verdict=resolvers.get("gate_verdict"),
        node_result=resolvers.get("node_result", lambda _node_id: {}),
        capabilities_present=resolvers.get("capabilities_present"),
    )


def test_corpus_default_policy_covers_every_policy_field() -> None:
    """Adding a Policy field must not leave the corpus scope silently short of it.

    ``version`` is loader bookkeeping and is never a gate constant, so it is the one
    field the scope does not bind.
    """
    assert set(_DEFAULT_POLICY) == set(Policy.model_fields) - {"version"}


def test_corpus_scope_binds_every_policy_field() -> None:
    scope = _scope({}, {})

    assert evaluate(parse_expression("policy.force_continue_allowed == true"), scope) is True
    assert evaluate(parse_expression("'high' in policy.human_review_risk_levels"), scope) is True
    assert evaluate(parse_expression("policy.plan_checks.assert_ideal == 'warn'"), scope) is True
    assert (
        evaluate(
            parse_expression("policy.evidence_sufficiency.on_insufficient == 'require_human'"),
            scope,
        )
        is True
    )
    assert (
        evaluate(
            parse_expression("policy.evidence_sufficiency.mutation_budget_seconds == 300"),
            scope,
        )
        is True
    )


@pytest.mark.parametrize(
    ("gate_id", "alias", "passing_decision"),
    [
        ("case-review-gate", "case_review", "pass"),
        ("api-plan-review-gate", "api_plan_review", "pass"),
        ("e2e-plan-review-gate", "plan_review", "pass"),
        ("fuzz-plan-review-gate", "fuzz_plan_review", "approved"),
        ("performance-plan-review-gate", "performance_plan_review", "approved"),
    ],
)
def test_force_continue_policy_branch_is_exercised(
    tmp_path: Path, gate_id: str, alias: str, passing_decision: str
) -> None:
    gate = load_workflow_v2(tmp_path).gates[gate_id]
    rule = next(rule for rule in gate.rules if rule.field == "needs_human_review_when")
    values = {
        alias: {
            "decision": passing_decision,
            "human_review_required": True,
            "risk_level": "low",
            "codegen_readiness": "ready",
            "required_capabilities": ["auth.api_admin_token"],
        },
        "data_knowledge": {"auth": {"api_admin_token": {"method": "token"}}},
        "params": {"force_continue": True},
    }
    resolvers = {"capabilities_present": lambda _r, _d: True}

    allowed = _scope(values, resolvers)
    denied = _scope({**values, "policy": {**_DEFAULT_POLICY, "force_continue_allowed": False}}, resolvers)

    assert evaluate(parse_expression(rule.expr), allowed) is False
    assert evaluate(parse_expression(rule.expr), denied) is True


P_FULL = {
    "params": {
        "run_mode": "full",
        "test_types": ["api", "e2e", "fuzz", "performance"],
        "run_tests": True,
        "auto_archive": True,
        "max_healing_attempts": 3,
        "force_continue": False,
    }
}

CORPUS: dict[str, tuple[tuple[dict, dict], tuple[dict, dict, object]]] = {}

CORPUS["gate:registry-gate:pass_when"] = (
    ({"registry": {"status": "pass", "healing_available": True}}, {}),
    ({"registry": {"status": "fail", "healing_available": False}}, {}, False),
)
CORPUS["gate:registry-gate:stop_when"] = (
    ({"registry": {"status": "fail", "healing_available": False}}, {}),
    ({"registry": {"status": "pass", "healing_available": True}}, {}, False),
)

_NF = ({"case_review": {"decision": "needs_fix", "auto_fix_allowed": True}}, {})
CORPUS["gate:case-review-gate:needs_fix_when"] = (_NF, ({}, {}, MISS))
CORPUS["gate:case-review-gate:needs_human_review_when"] = (
    ({"case_review": {"decision": "needs_human_review"}, "params": {"force_continue": False}}, {}),
    ({}, {}, MISS),
)
CORPUS["gate:case-review-gate:reject_when"] = (
    ({"case_review": {"decision": "reject"}}, {}),
    ({}, {}, MISS),
)
CORPUS["gate:case-review-gate:pass_when"] = (
    ({"case_review": {"decision": "pass"}}, {}),
    ({}, {}, MISS),
)

for _alias, _gid in [
    ("api_plan_review", "api-plan-review-gate"),
    ("plan_review", "e2e-plan-review-gate"),
]:
    CORPUS[f"gate:{_gid}:stop_when"] = (
        ({_alias: {"required_capabilities": None}}, {}),
        ({_alias: {"required_capabilities": ["auth.api_admin_token"]}}, {}, False),
    )
    CORPUS[f"gate:{_gid}:needs_fix_when"] = (
        ({_alias: {"decision": "needs_fix", "auto_fix_allowed": True}}, {}),
        ({}, {}, MISS),
    )
    CORPUS[f"gate:{_gid}:needs_human_review_when"] = (
        (
            {
                _alias: {
                    "decision": "pass",
                    "required_capabilities": ["auth.api_admin_token"],
                    "codegen_readiness": "ready",
                },
                "data_knowledge": {},
            },
            {"capabilities_present": lambda _r, _d: False},
        ),
        ({}, {}, MISS),
    )
    CORPUS[f"gate:{_gid}:reject_when"] = (
        ({_alias: {"decision": "reject"}}, {}),
        ({}, {}, MISS),
    )
    CORPUS[f"gate:{_gid}:pass_when"] = (
        (
            {
                _alias: {
                    "decision": "pass",
                    "codegen_readiness": "ready",
                    "required_capabilities": ["auth.api_admin_token"],
                },
                "data_knowledge": {"auth": {"api_admin_token": {"method": "token"}}},
            },
            {"capabilities_present": lambda _r, _d: True},
        ),
        ({}, {"capabilities_present": lambda _r, _d: False}, False),
    )

CORPUS["gate:api-plan-review-gate:stop_when"] = (
    (
        {
            "api_plan_review": {"required_capabilities": ["auth.api_admin_token"]},
            "api_plan_checks": None,
        },
        node_result_resolver({"mechanical-plan-checks": {"status": "succeeded"}}),
    ),
    (
        {
            "api_plan_review": {"required_capabilities": ["auth.api_admin_token"]},
            "api_plan_checks": None,
        },
        {},
        MISS,
    ),
)

for _alias, _gid in [
    ("fuzz_plan_review", "fuzz-plan-review-gate"),
    ("performance_plan_review", "performance-plan-review-gate"),
]:
    CORPUS[f"gate:{_gid}:needs_fix_when"] = (
        ({_alias: {"decision": "needs_fix", "auto_fix_allowed": True}}, {}),
        ({}, {}, MISS),
    )
    CORPUS[f"gate:{_gid}:needs_human_review_when"] = (
        ({_alias: {"decision": "changes_requested"}}, {}),
        ({}, {}, MISS),
    )
    CORPUS[f"gate:{_gid}:skip_when"] = (
        ({_alias: {"layer_applicable": False}}, {}),
        ({}, {}, MISS),
    )
    CORPUS[f"gate:{_gid}:reject_when"] = (
        ({_alias: {"decision": "reject"}}, {}),
        ({}, {}, MISS),
    )
    CORPUS[f"gate:{_gid}:pass_when"] = (
        ({_alias: {"decision": "approved"}}, {}),
        ({}, {}, MISS),
    )

CORPUS["gate:case-design-gate:pass_when"] = (
    ({"qa": {"approval": {"mode": "autonomous"}}}, {}),
    ({}, {}, MISS),
)

CORPUS["gate:api-codegen-precondition-gate:pass_when"] = (
    ({"api_plan_checks": {"status": "pass"}}, gvfx("pass", True)),
    ({}, gvfx("pass", True), False),
)
CORPUS["gate:api-codegen-precondition-gate:stop_when"] = (
    ({}, fx(True)),
    ({"api_plan_checks": {"status": "pass"}}, fx(True), False),
)
CORPUS["gate:e2e-codegen-precondition-gate:pass_when"] = (
    ({}, gvfx("pass", True)),
    ({}, gvfx(MISS, True), MISS),
)
CORPUS["gate:e2e-codegen-precondition-gate:stop_when"] = (({}, fx(False)), ({}, fx(True), False))

for _gid in ["fuzz-codegen-precondition-gate", "performance-codegen-precondition-gate"]:
    CORPUS[f"gate:{_gid}:pass_when"] = (({}, gv("pass")), ({}, gv(MISS), MISS))
    CORPUS[f"gate:{_gid}:stop_when"] = (({}, gv("stop")), ({}, gv(MISS), MISS))

CORPUS["gate:fixer-safety-gate:pass_when"] = (
    (
        {
            "fixer_safety_check": {
                "passed": True,
                "product_code_modified": False,
                "assertion_expected_value_changes_detected": False,
                "skip_or_xfail_added": False,
                "unrelated_tests_modified": False,
                "high_risk_proposal_applied": False,
            }
        },
        {},
    ),
    ({}, {}, MISS),
)
CORPUS["gate:fixer-safety-gate:needs_human_review_when"] = (
    ({"fixer_safety_check": {"passed": False}}, {}),
    ({}, {}, MISS),
)

CORPUS["gate:coverage-repair-entry-gate:enter_when"] = (
    ({"brief": {"eligible": True}}, {}),
    ({}, {}, MISS),
)
CORPUS["gate:coverage-repair-entry-gate:skip_when"] = (
    ({"brief": {"eligible": False}}, {}),
    ({}, {}, MISS),
)
CORPUS["gate:coverage-repair-loop-gate:reject_when"] = (
    ({"execution": {"final_status": "FAIL"}}, {}),
    ({"execution": {"final_status": "PASS"}}, {}, False),
)
CORPUS["gate:coverage-repair-loop-gate:exit_when"] = (
    ({"brief": {"eligible": False}}, {}),
    ({"brief": {"eligible": True}}, {}, False),
)
CORPUS["gate:coverage-repair-loop-gate:continue_when"] = (
    ({"brief": {"eligible": True}}, {}),
    ({"brief": {"eligible": False}}, {}, False),
)
CORPUS["gate:coverage-repair-safety-gate:needs_human_review_when"] = (
    (
        {
            "safety": {
                "needs_review": False,
                "skip_or_xfail_added": False,
                "stale_summary": True,
                "unbriefed_files_modified": [],
            }
        },
        {},
    ),
    (
        {
            "safety": {
                "needs_review": False,
                "skip_or_xfail_added": False,
                "stale_summary": False,
                "unbriefed_files_modified": [],
            }
        },
        {},
        False,
    ),
)
CORPUS["gate:coverage-repair-safety-gate:pass_when"] = (
    (
        {
            "safety": {
                "passed": True,
                "product_code_modified": False,
                "declaration_files_modified": False,
                "skip_or_xfail_added": False,
            }
        },
        {},
    ),
    ({}, {}, MISS),
)

CORPUS["gate:healing-entry-gate:enter_when"] = (
    (
        {
            "execution": {"final_status": "FAIL"},
            "failure_analysis": {
                "failures": [{"fix_proposal_eligible": True}],
                "inspect_mode": "primary",
            },
            "registry": {"healing_available": True},
        },
        {},
    ),
    ({}, {}, MISS),
)
CORPUS["gate:healing-entry-gate:stop_when"] = (
    (
        {
            "execution": {"final_status": "FAIL"},
            "failure_analysis": {"failures": [{"fix_proposal_eligible": True}]},
            "registry": {"healing_available": False},
        },
        {},
    ),
    ({}, {}, MISS),
)
CORPUS["gate:healing-entry-gate:skip_when"] = (
    ({"execution": {"final_status": "PASS"}}, {}),
    ({}, {}, MISS),
)

CORPUS["gate:healing-loop-gate:exit_when"] = (
    ({"execution": {"final_status": "PASS"}}, {}),
    ({"execution": {}}, {}, MISS),
)
CORPUS["gate:healing-loop-gate:continue_when"] = (
    (
        {
            "execution": {"final_status": "FAIL"},
            "failure_analysis": {"failures": [{"fix_proposal_eligible": True}]},
            "healing": {"attempts_used": 0},
            "params": {"max_healing_attempts": 3},
        },
        {},
    ),
    ({}, {}, MISS),
)
CORPUS["gate:healing-loop-gate:stop_when"] = (
    (
        {"healing": {"attempts_used": 3}, "params": {"max_healing_attempts": 3}},
        {},
    ),
    ({"healing": {}}, {}, MISS),
)
CORPUS["gate:healing-loop-gate:reject_when"] = (
    (
        {
            "execution": {"final_status": "FAIL"},
            "failure_analysis": {"failures": [{"fix_proposal_eligible": False}]},
        },
        {},
    ),
    (
        {
            "execution": {"final_status": "FAIL"},
            "failure_analysis": {"failures": [{"fix_proposal_eligible": True}]},
        },
        {},
        False,
    ),
)

CORPUS["gate:archive-gate:pass_when"] = (
    (
        {
            **P_FULL,
            "execution": {"final_status": "PASS", "batch_id": "b1"},
            "trace": {"has_open_problems": False},
            "healing": {"status": "resolved"},
            "case_review": {"decision": "pass"},
            "api_plan_review": {"decision": "pass"},
            "plan_review": {"decision": "pass"},
            "failure_analysis": {"source_batch_id": "b1", "failures": []},
            "metrics": {"collection_gaps": []},
        },
        {},
    ),
    ({}, {}, MISS),
)
CORPUS["gate:archive-gate:stop_when"] = (
    ({"execution": {"final_status": "FAIL"}}, {}),
    ({}, {}, MISS),
)


def _trace(**overrides: object) -> dict[str, object]:
    """A judged, clean `inspect/trace-sufficiency.json` — the shape the gate reads."""
    document: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-1",
        "authoritative_batch_id": "20260702-111111",
        "policy_digest": "0" * 64,
        "as_of": "2026-07-02T11:11:11+00:00",
        "integrity": "complete",
        "integrity_blocks_routing": False,
        "sufficient": True,
        "has_open_problems": False,
        "error_code": None,
        "insufficient_cases": [],
        "gap_codes": [],
    }
    document.update(overrides)
    return document


_THIN_EVIDENCE = _trace(
    sufficient=False,
    insufficient_cases=[{"case_id": "TC_API_001", "reason_codes": ["never_run"]}],
)

# The negative path for each rule is a *fully specified* document rather than a
# missing alias: the gate's `missing_field_is` already covers absence, and what is
# worth pinning here is that a healthy document does not satisfy the two failure
# rules — the direction in which a mistake fails open.
CORPUS["gate:trace-sufficiency-gate:stop_when"] = (
    ({"trace": _trace(error_code="policy_error")}, {}),
    ({"trace": _trace()}, {}, False),
)
# True via the policy branch on purpose: it is the only corpus expression that
# resolves `policy.evidence_sufficiency.on_insufficient`, whose default here is
# `require_human`.
CORPUS["gate:trace-sufficiency-gate:needs_human_review_when"] = (
    ({"trace": _THIN_EVIDENCE}, {}),
    ({"trace": _trace()}, {}, False),
)
CORPUS["gate:trace-sufficiency-gate:reject_when"] = (
    ({"trace": _trace(has_open_problems=True)}, {}),
    ({"trace": _trace()}, {}, False),
)
CORPUS["gate:trace-sufficiency-gate:pass_when"] = (
    ({"trace": _trace()}, {}),
    ({"trace": _THIN_EVIDENCE}, {}, False),
)


def test_corpus_covers_every_packaged_gate_expression(tmp_path: Path) -> None:
    locs = _collect(load_workflow_v2(tmp_path).gates)
    assert set(locs) == set(CORPUS), (
        "CORPUS 必须与打包 schema 的 gate 表达式一一对应；"
        f"缺失={set(locs) - set(CORPUS)} 多余={set(CORPUS) - set(locs)}"
    )


@pytest.mark.parametrize("loc", sorted(CORPUS))
def test_truth_and_missing_pair(tmp_path: Path, loc: str) -> None:
    expr = _collect(load_workflow_v2(tmp_path).gates)[loc]
    node = parse_expression(expr)
    (tv, tkw), (mv, mkw, mexp) = CORPUS[loc]
    assert evaluate(node, _scope(tv, tkw)) is True, f"{loc} 真值路径应为 True：{expr}"
    assert evaluate(node, _scope(mv, mkw)) is mexp, f"{loc} missing/否定路径不符：{expr}"
