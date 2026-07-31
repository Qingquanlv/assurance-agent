"""Gate expression corpus against packaged schema_version \"2\"."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.replay_schema import LayerTopologySpec, _codegen_ast_errors
from assurance_agent.workflow.graph.schema_v2 import EntrypointDef, GraphDef, ParamDef, WorkflowSchemaV2
from assurance_agent.workflow.orchestration.schema import normalize_gates
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
    "coverage_floor": {"risk_high": 0.9, "risk_medium": 0.7},
    "fuzz": {"required_when_endpoint_has_auth": True},
    "healing": {"auth_module": "require_human"},
}

_PLAN_ASSURANCE_RESOLVER = {
    "plan_assurance_state": lambda checks, review, dk, layer: (
        "applicable"
        if isinstance(checks, dict)
        and checks.get("schema_version") == "2"
        and checks.get("layer") == layer
        and isinstance(checks.get("applicability"), dict)
        and checks["applicability"].get("applicable") is True
        else (
            "not_applicable"
            if isinstance(checks, dict)
            and checks.get("schema_version") == "2"
            and isinstance(checks.get("applicability"), dict)
            and checks["applicability"].get("applicable") is False
            else "invalid"
        )
    )
}

_APPLICABLE_REVIEW = {
    "decision": "pass",
    "review_type": "api-plan",
    "change_id": "CH-1",
    "codegen_readiness": "ready",
    "required_capabilities": ["auth.api_admin_token"],
}
_APPLICABLE_E2E_REVIEW = {
    "decision": "pass",
    "review_type": "e2e-plan",
    "change_id": "CH-1",
    "codegen_readiness": "ready",
    "required_capabilities": ["auth.api_admin_token"],
}
_APPLICABLE_CHECKS = {
    "schema_version": "2",
    "layer": "api",
    "status": "pass",
    "applicability": {
        "layer": "api",
        "applicable": True,
        "reason_code": "automated_cases_present",
        "case_ids": ["TC"],
    },
    "checks": [],
}
_APPLICABLE_E2E_CHECKS = {
    "schema_version": "2",
    "layer": "e2e",
    "status": "pass",
    "applicability": {
        "layer": "e2e",
        "applicable": True,
        "reason_code": "automated_cases_present",
        "case_ids": ["TC"],
    },
    "checks": [],
}
_APPLICABLE_FUZZ_REVIEW = {
    "decision": "pass",
    "review_type": "fuzz-plan",
    "change_id": "CH-1",
    "codegen_readiness": "ready",
    "required_capabilities": ["auth.api_admin_token"],
}
_APPLICABLE_PERF_REVIEW = {
    "decision": "pass",
    "review_type": "performance-plan",
    "change_id": "CH-1",
    "codegen_readiness": "ready",
    "required_capabilities": ["auth.api_admin_token"],
}
_APPLICABLE_FUZZ_CHECKS = {
    "schema_version": "2",
    "layer": "fuzz",
    "status": "pass",
    "applicability": {
        "layer": "fuzz",
        "applicable": True,
        "reason_code": "automated_cases_present",
        "case_ids": ["TC"],
    },
    "checks": [],
}
_APPLICABLE_PERF_CHECKS = {
    "schema_version": "2",
    "layer": "performance",
    "status": "pass",
    "applicability": {
        "layer": "performance",
        "applicable": True,
        "reason_code": "automated_cases_present",
        "case_ids": ["TC"],
    },
    "checks": [],
}
_INAPPLICABLE_CHECKS = {
    "schema_version": "2",
    "layer": "api",
    "status": "pass",
    "applicability": {
        "layer": "api",
        "applicable": False,
        "reason_code": "no_automated_cases",
        "case_ids": [],
    },
    "checks": [
        {
            "check_id": check_id,
            "status": "not_applicable",
            "findings": [],
            "refs": [],
            "applicability_reason": "layer_not_applicable",
        }
        for check_id in ("l1_path", "shared_factory", "assert_ideal", "capability_keys")
    ],
}


def _scope(values, resolvers: dict[str, Any]):
    return Scope(
        {"policy": _DEFAULT_POLICY, **values},
        file_exists=resolvers.get("file_exists"),
        gate_verdict=resolvers.get("gate_verdict"),
        node_result=resolvers.get("node_result", lambda _node_id: {}),
        capabilities_present=resolvers.get("capabilities_present"),
        plan_assurance_state=resolvers.get("plan_assurance_state"),
    )


def test_corpus_scope_binds_every_policy_field() -> None:
    scope = _scope({}, {})

    assert evaluate(parse_expression("policy.force_continue_allowed == true"), scope) is True
    assert evaluate(parse_expression("'high' in policy.human_review_risk_levels"), scope) is True
    assert evaluate(parse_expression("policy.plan_checks.assert_ideal == 'warn'"), scope) is True
    assert evaluate(parse_expression("policy.coverage_floor.risk_high > 0"), scope) is True


@pytest.mark.parametrize(
    ("gate_id", "alias", "passing_decision"),
    [
        ("case-review-gate", "case_review", "pass"),
        ("api-plan-review-gate", "api_plan_review", "pass"),
        ("e2e-plan-review-gate", "plan_review", "pass"),
        ("fuzz-plan-review-gate", "fuzz_plan_review", "pass"),
        ("performance-plan-review-gate", "performance_plan_review", "pass"),
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
    if gate_id == "api-plan-review-gate":
        values["api_plan_checks"] = _APPLICABLE_CHECKS
    elif gate_id == "e2e-plan-review-gate":
        values["e2e_plan_checks"] = _APPLICABLE_E2E_CHECKS
    elif gate_id == "fuzz-plan-review-gate":
        values["fuzz_plan_checks"] = {
            **_APPLICABLE_CHECKS,
            "layer": "fuzz",
            "applicability": {**_APPLICABLE_CHECKS["applicability"], "layer": "fuzz"},
        }
    elif gate_id == "performance-plan-review-gate":
        values["performance_plan_checks"] = {
            **_APPLICABLE_CHECKS,
            "layer": "performance",
            "applicability": {**_APPLICABLE_CHECKS["applicability"], "layer": "performance"},
        }
    resolvers = {
        "capabilities_present": lambda _r, _d: True,
        **_PLAN_ASSURANCE_RESOLVER,
    }

    allowed = _scope(values, resolvers)
    denied = _scope(
        {**values, "policy": {**_DEFAULT_POLICY, "force_continue_allowed": False}},
        resolvers,
    )

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

for _alias, _gid, _layer, _checks_key in [
    ("api_plan_review", "api-plan-review-gate", "api", "api_plan_checks"),
    ("plan_review", "e2e-plan-review-gate", "e2e", "e2e_plan_checks"),
]:
    CORPUS[f"gate:{_gid}:stop_when"] = (
        (
            {_checks_key: {"schema_version": "1"}, _alias: _APPLICABLE_REVIEW},
            _PLAN_ASSURANCE_RESOLVER,
        ),
        (
            {
                _checks_key: _APPLICABLE_CHECKS if _layer == "api" else _APPLICABLE_E2E_CHECKS,
                _alias: _APPLICABLE_REVIEW,
            },
            _PLAN_ASSURANCE_RESOLVER,
            False,
        ),
    )
    CORPUS[f"gate:{_gid}:skip_when"] = (
        (
            {
                _checks_key: _INAPPLICABLE_CHECKS
                if _layer == "api"
                else {
                    **_INAPPLICABLE_CHECKS,
                    "layer": "e2e",
                    "applicability": {**_INAPPLICABLE_CHECKS["applicability"], "layer": "e2e"},
                },
                _alias: None,
            },
            _PLAN_ASSURANCE_RESOLVER,
        ),
        (
            {
                _checks_key: _APPLICABLE_CHECKS if _layer == "api" else _APPLICABLE_E2E_CHECKS,
                _alias: _APPLICABLE_REVIEW,
            },
            _PLAN_ASSURANCE_RESOLVER,
            False,
        ),
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
                _checks_key: _APPLICABLE_CHECKS if _layer == "api" else _APPLICABLE_E2E_CHECKS,
                "data_knowledge": {},
            },
            {
                **_PLAN_ASSURANCE_RESOLVER,
                "capabilities_present": lambda _r, _d: False,
            },
        ),
        ({}, _PLAN_ASSURANCE_RESOLVER, False),
    )
    CORPUS[f"gate:{_gid}:reject_when"] = (
        ({_alias: {"decision": "reject"}}, _PLAN_ASSURANCE_RESOLVER),
        ({}, _PLAN_ASSURANCE_RESOLVER, MISS),
    )
    CORPUS[f"gate:{_gid}:pass_when"] = (
        (
            {
                _alias: _APPLICABLE_REVIEW if _layer == "api" else _APPLICABLE_E2E_REVIEW,
                _checks_key: _APPLICABLE_CHECKS if _layer == "api" else _APPLICABLE_E2E_CHECKS,
                "data_knowledge": {"auth": {"api_admin_token": {"method": "token"}}},
            },
            {
                **_PLAN_ASSURANCE_RESOLVER,
                "capabilities_present": lambda _r, _d: True,
            },
        ),
        (
            {
                _checks_key: _APPLICABLE_CHECKS if _layer == "api" else _APPLICABLE_E2E_CHECKS,
                _alias: _APPLICABLE_REVIEW,
            },
            {**_PLAN_ASSURANCE_RESOLVER, "capabilities_present": lambda _r, _d: False},
            False,
        ),
    )

for _alias, _gid, _layer, _checks_key, _review, _checks in [
    (
        "fuzz_plan_review",
        "fuzz-plan-review-gate",
        "fuzz",
        "fuzz_plan_checks",
        _APPLICABLE_FUZZ_REVIEW,
        _APPLICABLE_FUZZ_CHECKS,
    ),
    (
        "performance_plan_review",
        "performance-plan-review-gate",
        "performance",
        "performance_plan_checks",
        _APPLICABLE_PERF_REVIEW,
        _APPLICABLE_PERF_CHECKS,
    ),
]:
    CORPUS[f"gate:{_gid}:stop_when"] = (
        (
            {_checks_key: {"schema_version": "1"}, _alias: _review},
            _PLAN_ASSURANCE_RESOLVER,
        ),
        ({_checks_key: _checks, _alias: _review}, _PLAN_ASSURANCE_RESOLVER, False),
    )
    CORPUS[f"gate:{_gid}:skip_when"] = (
        (
            {
                _checks_key: {
                    **_INAPPLICABLE_CHECKS,
                    "layer": _layer,
                    "applicability": {**_INAPPLICABLE_CHECKS["applicability"], "layer": _layer},
                },
                _alias: None,
            },
            _PLAN_ASSURANCE_RESOLVER,
        ),
        ({_checks_key: _checks, _alias: _review}, _PLAN_ASSURANCE_RESOLVER, False),
    )
    CORPUS[f"gate:{_gid}:needs_fix_when"] = (
        ({_alias: {"decision": "needs_fix"}}, {}),
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
                _checks_key: _checks,
                "data_knowledge": {},
            },
            {
                **_PLAN_ASSURANCE_RESOLVER,
                "capabilities_present": lambda _r, _d: False,
            },
        ),
        ({}, _PLAN_ASSURANCE_RESOLVER, False),
    )
    CORPUS[f"gate:{_gid}:reject_when"] = (
        ({_alias: {"decision": "reject"}}, _PLAN_ASSURANCE_RESOLVER),
        ({}, _PLAN_ASSURANCE_RESOLVER, MISS),
    )
    CORPUS[f"gate:{_gid}:pass_when"] = (
        (
            {
                _alias: _review,
                _checks_key: _checks,
                "data_knowledge": {"auth": {"api_admin_token": {"method": "token"}}},
            },
            {
                **_PLAN_ASSURANCE_RESOLVER,
                "capabilities_present": lambda _r, _d: True,
            },
        ),
        (
            {_checks_key: _checks, _alias: _review},
            {**_PLAN_ASSURANCE_RESOLVER, "capabilities_present": lambda _r, _d: False},
            False,
        ),
    )

CORPUS["gate:case-design-gate:pass_when"] = (
    ({"qa": {"approval": {"mode": "autonomous"}}}, {}),
    ({}, {}, MISS),
)

CORPUS["gate:api-codegen-precondition-gate:skip_when"] = (
    ({"api_plan_checks": _INAPPLICABLE_CHECKS, "api_plan_review": None}, _PLAN_ASSURANCE_RESOLVER),
    (
        {"api_plan_checks": _APPLICABLE_CHECKS, "api_plan_review": _APPLICABLE_REVIEW},
        _PLAN_ASSURANCE_RESOLVER,
        False,
    ),
)
CORPUS["gate:api-codegen-precondition-gate:pass_when"] = (
    (
        {"api_plan_checks": _APPLICABLE_CHECKS, "api_plan_review": _APPLICABLE_REVIEW},
        {
            **_PLAN_ASSURANCE_RESOLVER,
            **node_result_resolver({"review-cycle": {"status": "succeeded"}}),
            **gvfx("pass", True),
        },
    ),
    ({}, {**_PLAN_ASSURANCE_RESOLVER, **gvfx("pass", True)}, False),
)
CORPUS["gate:api-codegen-precondition-gate:stop_when"] = (
    (
        {"api_plan_checks": {"schema_version": "1"}, "api_plan_review": _APPLICABLE_REVIEW},
        {
            **_PLAN_ASSURANCE_RESOLVER,
            **node_result_resolver({"review-cycle": {"status": "succeeded"}}),
            **fx(True),
        },
    ),
    (
        {"api_plan_checks": _APPLICABLE_CHECKS, "api_plan_review": _APPLICABLE_REVIEW},
        {
            **_PLAN_ASSURANCE_RESOLVER,
            **node_result_resolver({"review-cycle": {"status": "succeeded"}}),
            **fx(True),
        },
        False,
    ),
)
CORPUS["gate:e2e-codegen-precondition-gate:skip_when"] = (
    (
        {
            "e2e_plan_checks": {
                **_INAPPLICABLE_CHECKS,
                "layer": "e2e",
                "applicability": {**_INAPPLICABLE_CHECKS["applicability"], "layer": "e2e"},
            },
            "plan_review": None,
        },
        _PLAN_ASSURANCE_RESOLVER,
    ),
    (
        {"e2e_plan_checks": _APPLICABLE_E2E_CHECKS, "plan_review": _APPLICABLE_E2E_REVIEW},
        _PLAN_ASSURANCE_RESOLVER,
        False,
    ),
)
CORPUS["gate:e2e-codegen-precondition-gate:pass_when"] = (
    (
        {"e2e_plan_checks": _APPLICABLE_E2E_CHECKS, "plan_review": _APPLICABLE_E2E_REVIEW},
        {
            **_PLAN_ASSURANCE_RESOLVER,
            **node_result_resolver({"review-cycle": {"status": "succeeded"}}),
            **gvfx("pass", True),
        },
    ),
    ({}, {**_PLAN_ASSURANCE_RESOLVER, **gvfx("pass", True)}, False),
)
CORPUS["gate:e2e-codegen-precondition-gate:stop_when"] = (
    (
        {"e2e_plan_checks": {"schema_version": "1"}, "plan_review": _APPLICABLE_E2E_REVIEW},
        {
            **_PLAN_ASSURANCE_RESOLVER,
            **node_result_resolver({"review-cycle": {"status": "failed"}}),
            **fx(True),
        },
    ),
    (
        {"e2e_plan_checks": _APPLICABLE_E2E_CHECKS, "plan_review": _APPLICABLE_E2E_REVIEW},
        {
            **_PLAN_ASSURANCE_RESOLVER,
            **node_result_resolver({"review-cycle": {"status": "succeeded"}}),
            **fx(True),
        },
        False,
    ),
)

for _gid, _alias, _checks_key, _review, _checks, _plan_gate in [
    (
        "fuzz-codegen-precondition-gate",
        "fuzz_plan_review",
        "fuzz_plan_checks",
        _APPLICABLE_FUZZ_REVIEW,
        _APPLICABLE_FUZZ_CHECKS,
        "fuzz-plan-review-gate",
    ),
    (
        "performance-codegen-precondition-gate",
        "performance_plan_review",
        "performance_plan_checks",
        _APPLICABLE_PERF_REVIEW,
        _APPLICABLE_PERF_CHECKS,
        "performance-plan-review-gate",
    ),
]:
    CORPUS[f"gate:{_gid}:skip_when"] = (
        (
            {
                _checks_key: {
                    **_INAPPLICABLE_CHECKS,
                    "layer": _checks["layer"],
                    "applicability": {
                        **_INAPPLICABLE_CHECKS["applicability"],
                        "layer": _checks["layer"],
                    },
                },
                _alias: None,
            },
            _PLAN_ASSURANCE_RESOLVER,
        ),
        ({_checks_key: _checks, _alias: _review}, _PLAN_ASSURANCE_RESOLVER, False),
    )
    CORPUS[f"gate:{_gid}:pass_when"] = (
        (
            {_checks_key: _checks, _alias: _review},
            {
                **_PLAN_ASSURANCE_RESOLVER,
                **node_result_resolver({"review-cycle": {"status": "succeeded"}}),
                **gvfx("pass", True),
                "capabilities_present": lambda _r, _d: True,
            },
        ),
        ({}, {**_PLAN_ASSURANCE_RESOLVER, **gvfx("pass", True)}, False),
    )
    CORPUS[f"gate:{_gid}:stop_when"] = (
        (
            {_checks_key: {"schema_version": "1"}, _alias: _review},
            {
                **_PLAN_ASSURANCE_RESOLVER,
                **node_result_resolver({"review-cycle": {"status": "succeeded"}}),
                **fx(True),
            },
        ),
        (
            {_checks_key: _checks, _alias: _review},
            {
                **_PLAN_ASSURANCE_RESOLVER,
                **node_result_resolver({"review-cycle": {"status": "succeeded"}}),
                **fx(True),
            },
            False,
        ),
    )

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
            "healing": {"status": "resolved"},
            "case_review": {"decision": "pass"},
            "api_plan_review": {"decision": "pass"},
            "plan_review": {"decision": "pass"},
            "failure_analysis": {"source_batch_id": "b1", "failures": []},
        },
        {},
    ),
    ({}, {}, MISS),
)
CORPUS["gate:archive-gate:stop_when"] = (
    ({"execution": {"final_status": "FAIL"}}, {}),
    ({}, {}, MISS),
)

BUILTIN_CORPUS: dict[str, tuple[tuple[dict, dict], tuple[dict, dict, object]]] = {
    "builtin:plan_assurance_state:valid": (
        (
            {
                "api_plan_checks": _APPLICABLE_CHECKS,
                "api_plan_review": _APPLICABLE_REVIEW,
                "data_knowledge": {"version": 1, "capabilities": {"domain_factories": {}}},
            },
            _PLAN_ASSURANCE_RESOLVER,
        ),
        (
            {
                "api_plan_checks": {"schema_version": "1"},
                "api_plan_review": _APPLICABLE_REVIEW,
                "data_knowledge": {"version": 1},
            },
            _PLAN_ASSURANCE_RESOLVER,
            False,
        ),
    ),
    "builtin:plan_assurance_state:missing": (
        (
            {
                "api_plan_checks": None,
                "api_plan_review": None,
                "data_knowledge": None,
            },
            _PLAN_ASSURANCE_RESOLVER,
        ),
        (
            {
                "api_plan_checks": _APPLICABLE_CHECKS,
                "api_plan_review": None,
                "data_knowledge": None,
            },
            _PLAN_ASSURANCE_RESOLVER,
            False,
        ),
    ),
}


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


def test_replayable_gate_builtins_are_semantically_fingerprinted() -> None:
    from assurance_agent.workflow.graph.replay_schema import _REPLAYABLE_PLAN_BUILTINS
    from assurance_agent.workflow.orchestration.gate_semantics import discover_replay_semantic_dependencies

    manifest = discover_replay_semantic_dependencies()
    assert _REPLAYABLE_PLAN_BUILTINS <= {
        "plan_assurance_state",
        "capabilities_present",
        "check_failed",
        "defined",
        "len",
    }
    required = {
        "assurance_agent.verification.gate_state.plan_assurance_state",
        "assurance_agent.knowledge.capabilities.capabilities_present",
        "assurance_agent.workflow.orchestration.dsl._eval_call",
    }
    assert required <= manifest


@pytest.mark.parametrize("loc", sorted(BUILTIN_CORPUS))
def test_builtin_truth_and_missing_pair(loc: str) -> None:
    if loc.endswith(":valid"):
        expr = "plan_assurance_state(api_plan_checks, api_plan_review, data_knowledge, 'api') == 'applicable'"
    else:
        expr = "plan_assurance_state(api_plan_checks, api_plan_review, data_knowledge, 'api') == 'invalid'"
    node = parse_expression(expr)
    (tv, tkw), (mv, mkw, mexp) = BUILTIN_CORPUS[loc]
    assert evaluate(node, _scope(tv, tkw)) is True, f"{loc} 真值路径应为 True：{expr}"
    assert evaluate(node, _scope(mv, mkw)) is mexp, f"{loc} missing/否定路径不符：{expr}"


_FUZZ_PERF_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "assurance" / "fuzz-performance-gates.yaml"
)


def _load_fuzz_perf_fixture_gates():
    raw = yaml.safe_load(_FUZZ_PERF_FIXTURE.read_text(encoding="utf-8"))
    return normalize_gates(raw)


def _fixture_schema_for_layer(layer: str) -> WorkflowSchemaV2:
    gates = _load_fuzz_perf_fixture_gates()
    return WorkflowSchemaV2(
        schema_version="2",
        name="fuzz-performance-fixture",
        params={name: ParamDef(type="str") for name in ("run_mode", "test_types")},
        entrypoints={"execute": EntrypointDef(graph="assurance")},
        graphs={"assurance": GraphDef(max_supersteps=10, nodes={}, edges=[], routes=[])},
        gates=gates,
    )


def _topology_spec(layer: str) -> LayerTopologySpec:
    profile = get_layer_assurance_profile(layer)
    return LayerTopologySpec(
        layer=profile.layer,
        plan_artifacts=profile.plan_artifacts,
        review_artifact=profile.review_artifact,
        review_alias=profile.review_alias,
        checks_artifact=profile.checks_artifact,
        gate_id=profile.gate_id,
    )


def test_fixture_excludes_legacy_policy_and_applicability_references() -> None:
    text = _FUZZ_PERF_FIXTURE.read_text(encoding="utf-8")
    assert "policy.fuzz.required_when_endpoint_has_auth" not in text
    assert "layer_applicable" not in text


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_codegen_expressions_contain_hard_predicates(layer: str) -> None:
    schema = _fixture_schema_for_layer(layer)
    spec = _topology_spec(layer)
    gate_id = f"{layer}-codegen-precondition-gate"
    errors = _codegen_ast_errors(
        schema,
        spec,
        codegen_gate_id=gate_id,
        cycle_call_node_id="review-cycle",
    )
    assert not errors, errors
