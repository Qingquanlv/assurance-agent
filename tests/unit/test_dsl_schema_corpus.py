"""Gate expression corpus against packaged schema_version \"2\"."""

from pathlib import Path
from typing import Any

import pytest

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


def _scope(values, resolvers: dict[str, Any]):
    return Scope(
        values,
        file_exists=resolvers.get("file_exists"),
        gate_verdict=resolvers.get("gate_verdict"),
        node_result=resolvers.get("node_result", lambda _node_id: {}),
        capabilities_present=resolvers.get("capabilities_present"),
    )


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

for _gid in ["api-codegen-precondition-gate", "e2e-codegen-precondition-gate"]:
    CORPUS[f"gate:{_gid}:pass_when"] = (({}, gvfx("pass", True)), ({}, gvfx(MISS, True), MISS))
    CORPUS[f"gate:{_gid}:stop_when"] = (({}, fx(False)), ({}, fx(True), False))

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
