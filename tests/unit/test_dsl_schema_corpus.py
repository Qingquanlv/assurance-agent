"""对拍：打包 schema 的每条表达式都有真值 + missing 覆盖（位置 id 驱动，与 schema 一一对应）。"""

from pathlib import Path

import pytest

from assurance_agent.workflow.orchestration.dsl import MISSING as MISS
from assurance_agent.workflow.orchestration.dsl import Scope, evaluate, parse_expression
from assurance_agent.workflow.orchestration.schema import WorkflowSchema, load_workflow_schema


def _collect(schema: WorkflowSchema) -> dict[str, str]:
    """位置 id → 表达式串（从加载后的 schema 直接抽取，绝不手抄）。"""
    out: dict[str, str] = {}
    for p in schema.phases:
        if p.when:
            out[f"phase:{p.id}:when"] = p.when
        if p.ready_when:
            out[f"phase:{p.id}:ready_when"] = p.ready_when
    for lid, loop in schema.loops.items():
        if loop.allocate_on:
            out[f"loop:{lid}:allocate_on"] = loop.allocate_on
    for gid, g in schema.gates.items():
        for r in g.rules:
            out[f"gate:{gid}:{r.field}"] = r.expr
    return out


# ---- 作用域构造小工具 -------------------------------------------------------
def gv(v):
    return {"gate_verdict": lambda _i, _v=v: _v}


def fx(b):
    return {"file_exists": lambda _p, _b=b: _b}


def gvfx(v, b):
    return {"gate_verdict": lambda _i, _v=v: _v, "file_exists": lambda _p, _b=b: _b}


# run_mode/test_types/run_tests/auto_archive 全满足——覆盖所有「params.* when」的真值路径。
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

# 每项：位置 id -> ((真值 vars, 真值 kw), (missing vars, missing kw, 期望))
CORPUS: dict[str, tuple[tuple[dict, dict], tuple[dict, dict, object]]] = {}

# --- 纯 params 的 phase when：P_FULL 全为真；空作用域全为 MISSING ---
for _pid in [
    "explore",
    "case-design",
    "fact-baseline",
    "api-plan",
    "api-plan-review",
    "api-codegen",
    "e2e-plan",
    "e2e-plan-review",
    "e2e-codegen",
    "fuzz-plan",
    "fuzz-plan-review",
    "fuzz-codegen",
    "performance-plan",
    "performance-plan-review",
    "performance-codegen",
    "execution",
    "archive",
]:
    CORPUS[f"phase:{_pid}:when"] = ((P_FULL, {}), ({}, {}, MISS))

# --- gate() 驱动的 phase when ---
CORPUS["phase:case-fix:when"] = ((P_FULL, gv("needs_fix")), ({}, gv(MISS), MISS))
CORPUS["phase:api-plan-fix:when"] = ((P_FULL, gv("needs_fix")), ({}, gv(MISS), MISS))
CORPUS["phase:e2e-plan-fix:when"] = ((P_FULL, gv("needs_fix")), ({}, gv(MISS), MISS))
CORPUS["phase:fix-proposal:when"] = ((P_FULL, gv("enter")), ({}, gv(MISS), MISS))

# --- any(...) 驱动的 phase when ---
CORPUS["phase:api-codegen-fix:when"] = (
    ({"fix_proposal": {"proposals": [{"target": "api", "eligible": True}]}}, {}),
    ({}, {}, MISS),
)
CORPUS["phase:e2e-codegen-fix:when"] = (
    ({"fix_proposal": {"proposals": [{"target": "e2e", "eligible": True}]}}, {}),
    ({}, {}, MISS),
)

# --- phase ready_when（report）---
CORPUS["phase:report:ready_when"] = (
    ({"state": {"phases": {"healing": {"status": "resolved"}}}}, {}),
    ({"state": {"phases": {}}}, {}, MISS),
)

# --- loop allocate_on（healing）---
CORPUS["loop:healing:allocate_on"] = (
    ({"fix_proposal": {"summary": {"eligible_count": 2}}}, fx(True)),
    ({}, fx(True), MISS),
)

# --- registry-gate ---
CORPUS["gate:registry-gate:pass_when"] = (
    ({"state": {"phases": {"skill_registry_check": {"status": "pass"}}}}, {}),
    ({"state": {"phases": {}}}, {}, MISS),
)
CORPUS["gate:registry-gate:stop_when"] = (
    ({"state": {"phases": {"skill_registry_check": {"status": "fail"}}}}, {}),
    ({"state": {"phases": {}}}, {}, MISS),
)

# --- case-review-gate / api|e2e-plan-review-gate（decision 型，四态）---
_NF = ({"decision": "needs_fix", "auto_fix_allowed": True}, {})
for _gid in ["case-review-gate", "api-plan-review-gate", "e2e-plan-review-gate"]:
    CORPUS[f"gate:{_gid}:needs_fix_when"] = (_NF, ({}, {}, MISS))
    CORPUS[f"gate:{_gid}:needs_human_review_when"] = (
        ({"decision": "needs_human_review"}, {}),
        ({}, {}, MISS),
    )
    CORPUS[f"gate:{_gid}:reject_when"] = (({"decision": "reject"}, {}), ({}, {}, MISS))
CORPUS["gate:case-review-gate:pass_when"] = (({"decision": "pass"}, {}), ({}, {}, MISS))
for _gid in ["api-plan-review-gate", "e2e-plan-review-gate"]:
    CORPUS[f"gate:{_gid}:pass_when"] = (
        ({"decision": "pass", "codegen_readiness": "ready"}, {}),
        ({}, {}, MISS),
    )

# --- fuzz|performance-plan-review-gate（decision in [...] 型）---
for _gid in ["fuzz-plan-review-gate", "performance-plan-review-gate"]:
    CORPUS[f"gate:{_gid}:needs_fix_when"] = (_NF, ({}, {}, MISS))
    CORPUS[f"gate:{_gid}:needs_human_review_when"] = (({"decision": "changes_requested"}, {}), ({}, {}, MISS))
    CORPUS[f"gate:{_gid}:reject_when"] = (({"decision": "reject"}, {}), ({}, {}, MISS))
    CORPUS[f"gate:{_gid}:pass_when"] = (({"decision": "approved"}, {}), ({}, {}, MISS))

# --- case-design-gate ---
CORPUS["gate:case-design-gate:pass_when"] = (
    ({"state": {"run_context": {"interaction_mode": "autonomous"}}}, {}),
    ({}, {}, MISS),
)

# --- api|e2e-codegen-precondition-gate（gate() + file_exists）---
for _gid in ["api-codegen-precondition-gate", "e2e-codegen-precondition-gate"]:
    CORPUS[f"gate:{_gid}:pass_when"] = (({}, gvfx("pass", True)), ({}, gvfx(MISS, True), MISS))
    # stop_when = not file_exists(...)：真值 file_exists=False；否定路径 file_exists=True → False（无 MISSING 字段）
    CORPUS[f"gate:{_gid}:stop_when"] = (({}, fx(False)), ({}, fx(True), False))

# --- fuzz|performance-codegen-precondition-gate（纯 gate()）---
for _gid in ["fuzz-codegen-precondition-gate", "performance-codegen-precondition-gate"]:
    CORPUS[f"gate:{_gid}:pass_when"] = (({}, gv("pass")), ({}, gv(MISS), MISS))
    CORPUS[f"gate:{_gid}:stop_when"] = (({}, gv("stop")), ({}, gv(MISS), MISS))

# --- fixer-safety-gate ---
CORPUS["gate:fixer-safety-gate:pass_when"] = (
    (
        {
            "passed": True,
            "product_code_modified": False,
            "assertion_expected_value_changes_detected": False,
            "skip_or_xfail_added": False,
            "unrelated_tests_modified": False,
            "high_risk_proposal_applied": False,
        },
        {},
    ),
    ({}, {}, MISS),
)
CORPUS["gate:fixer-safety-gate:needs_human_review_when"] = (({"passed": False}, {}), ({}, {}, MISS))

# --- healing-entry-gate ---
CORPUS["gate:healing-entry-gate:enter_when"] = (
    (
        {
            "state": {
                "phases": {
                    "execution": {"status": "FAIL"},
                    "inspect": {"inspect_mode": "primary"},
                    "healing": {"attempts_used": 0},
                },
                "gates": {"healing_available": True},
            },
            "failure_analysis": {"failures": [{"fix_proposal_eligible": True}]},
            "params": {"max_healing_attempts": 3},
        },
        {},
    ),
    ({}, {}, MISS),
)
CORPUS["gate:healing-entry-gate:stop_when"] = (
    (
        {
            "state": {"phases": {"execution": {"status": "FAIL"}}, "gates": {"healing_available": False}},
            "failure_analysis": {"failures": [{"fix_proposal_eligible": True}]},
        },
        {},
    ),
    ({}, {}, MISS),
)
CORPUS["gate:healing-entry-gate:skip_when"] = (
    ({"state": {"phases": {"execution": {"status": "PASS"}}}}, {}),
    ({}, {}, MISS),
)

# --- healing-loop-gate ---
CORPUS["gate:healing-loop-gate:exit_when"] = (
    ({"state": {"phases": {"execution": {"status": "PASS"}}}}, {}),
    ({"state": {"phases": {}}}, {}, MISS),
)
CORPUS["gate:healing-loop-gate:continue_when"] = (
    (
        {
            "state": {"phases": {"execution": {"status": "FAIL"}, "healing": {"attempts_used": 0}}},
            "failure_analysis": {"failures": [{"fix_proposal_eligible": True}]},
            "params": {"max_healing_attempts": 3},
        },
        {},
    ),
    ({}, {}, MISS),
)
CORPUS["gate:healing-loop-gate:stop_when"] = (
    ({"state": {"phases": {"healing": {"attempts_used": 3}}}, "params": {"max_healing_attempts": 3}}, {}),
    ({"state": {"phases": {"healing": {}}}}, {}, MISS),
)
CORPUS["gate:healing-loop-gate:reject_when"] = (
    (
        {
            "state": {"phases": {"execution": {"status": "FAIL"}}},
            "failure_analysis": {"failures": [{"fix_proposal_eligible": False}]},
        },
        {},
    ),
    (
        {
            "state": {"phases": {"execution": {"status": "FAIL"}}},
            "failure_analysis": {"failures": [{"fix_proposal_eligible": True}]},
        },
        {},
        False,
    ),
)

# --- archive-gate ---
CORPUS["gate:archive-gate:pass_when"] = (
    (
        {
            **P_FULL,
            "state": {
                "user_requested_archive": False,
                "phases": {
                    "execution": {"status": "PASS", "batch_id": "b1"},
                    "healing": {"status": "resolved"},
                },
            },
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
    ({"state": {"phases": {"execution": {"status": "FAIL"}}}}, {}),
    ({}, {}, MISS),
)


def test_corpus_covers_every_packaged_expression(tmp_path: Path):
    locs = _collect(load_workflow_schema(tmp_path))  # 包内默认 schema
    assert set(locs) == set(CORPUS), (
        "CORPUS 必须与打包 schema 的表达式一一对应；"
        f"缺失={set(locs) - set(CORPUS)} 多余={set(CORPUS) - set(locs)}"
    )


@pytest.mark.parametrize("loc", sorted(CORPUS))
def test_truth_and_missing_pair(tmp_path: Path, loc: str):
    expr = _collect(load_workflow_schema(tmp_path))[loc]
    node = parse_expression(expr)
    (tv, tkw), (mv, mkw, mexp) = CORPUS[loc]
    assert evaluate(node, Scope(tv, **tkw)) is True, f"{loc} 真值路径应为 True：{expr}"
    assert evaluate(node, Scope(mv, **mkw)) is mexp, f"{loc} missing/否定路径不符：{expr}"
