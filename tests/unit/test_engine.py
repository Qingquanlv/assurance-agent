import json
from pathlib import Path

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.state import read_state, write_state
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.schema import parse_schema
from tests.helpers_aa import loc_for

# 进度由 produces 文件存在性驱动；gate DSL 按字面读取 state.phases.<下划线 key>.status。
# 注意 phase id 用连字符（skill-registry-check），gate 读的 state key 用下划线（skill_registry_check）——
# 忠实复刻打包 schema 的词汇差异（本轮 P0）。
SCHEMA = parse_schema("""
schema_version: "1"
name: t
params:
  run_mode: { type: enum, values: [full, case-only], default: full }
phases:
  - id: skill-registry-check
    skill: null
    requires: []
    produces: [workflow-state.yaml]
    gate: reg-gate
  - id: explore
    skill: aa-explore
    agent: aa-doc-author
    requires: [skill-registry-check]
    produces: [explore/advisory.json]
  - id: case
    skill: aa-case-design
    agent: aa-doc-author
    requires: [explore]
    produces: [.qa.yaml]
    when: "params.run_mode == 'full'"
gates:
  reg-gate:
    reads: [workflow-state.yaml]
    pass_when: "state.phases.skill_registry_check.status == 'pass'"
    stop_when: "state.phases.skill_registry_check.status == 'fail'"
""")

# needs_fix→healing 路由 + scope 归属；覆盖 produces-存在性 / repair 路由 / 耗尽三条 P0。
HEAL_SCHEMA = parse_schema("""
schema_version: "1"
name: h
params:
  max_healing_attempts: { type: int, default: 3 }
phases:
  - id: intake
    skill: aa-intake
    agent: aa-doc-author
    owned_by: [full]
    requires: []
    produces: [proposal.md]
  - id: execution
    skill: null
    owned_by: [full, execute]
    requires: [intake]
    produces: [execution/execution-manifest.yaml]
    gate: exec-gate
  - id: fix-proposal
    skill: aa-fixer
    agent: aa-test-author
    owned_by: [full, execute]
    requires: []
    when: "gate('healing-entry-gate').verdict == 'enter'"
    produces: [healing/fix-proposal.json]
gates:
  exec-gate:
    reads: [inspect/failure-analysis.json]
    needs_fix_when: "fix_proposal_eligible == true"
    pass_when: "fix_proposal_eligible == false"
    missing_file_is: pass
  healing-entry-gate:
    reads: [inspect/failure-analysis.json]
    enter_when: "fix_proposal_eligible == true and state.phases.healing.attempts_used < params.max_healing_attempts"
    skip_when: "fix_proposal_eligible == false"
    missing_file_is: skip
""")


def read_state_or(cd: Path) -> WorkflowState:
    return read_state(cd) or WorkflowState()


def _pv(status, pid):
    return next(p for p in status.phases if p.id == pid)


def _write_fa(change_dir: Path, eligible: bool) -> None:
    d = change_dir / "inspect"
    d.mkdir(parents=True, exist_ok=True)
    (d / "failure-analysis.json").write_text(json.dumps({"fix_proposal_eligible": eligible}))


def _touch(change_dir: Path, rel: str) -> None:
    p = change_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")


def test_initial_ready_is_reg_no_gate_adjudication(tmp_path: Path):
    """P0: 无 produces → skill-registry-check 只是 ready（未运行），其 exit gate 不被裁决。"""
    st = compute_status(SCHEMA, loc_for(tmp_path), WorkflowState(), {})
    assert _pv(st, "skill-registry-check").status == "ready"
    assert [d.phase_id for d in st.next_dispatch] == ["skill-registry-check"]
    assert st.next_dispatch[0].kind == "orchestrator"  # _INTERNAL_PHASES
    assert st.terminal is None


def test_when_prunes_case(tmp_path: Path):
    st = compute_status(SCHEMA, loc_for(tmp_path), WorkflowState(), {"run_mode": "case-only"})
    assert _pv(st, "case").status == "pruned"


def test_downstream_ready_after_produces_and_gate_pass(tmp_path: Path):
    """produces 存在（workflow-state.yaml）+ gate pass → done；下游 explore → ready。"""
    # write_state 落盘 workflow-state.yaml（= skill-registry-check 的 produces），并写入 gate 读取的 status
    write_state(
        tmp_path, WorkflowState.model_validate({"phases": {"skill_registry_check": {"status": "pass"}}})
    )
    st = compute_status(SCHEMA, loc_for(tmp_path), read_state_or(tmp_path), {})
    assert _pv(st, "skill-registry-check").status == "done"
    assert _pv(st, "explore").status == "ready"


def test_ran_phase_gate_stop_is_terminal(tmp_path: Path):
    write_state(
        tmp_path, WorkflowState.model_validate({"phases": {"skill_registry_check": {"status": "fail"}}})
    )
    st = compute_status(SCHEMA, loc_for(tmp_path), read_state_or(tmp_path), {})
    assert st.terminal is not None and st.terminal.kind == "stopped"
    assert _pv(st, "skill-registry-check").status == "stopped"


def test_completed_when_all_produces_present_and_gates_pass(tmp_path: Path):
    write_state(
        tmp_path, WorkflowState.model_validate({"phases": {"skill_registry_check": {"status": "pass"}}})
    )
    _touch(tmp_path, "explore/advisory.json")  # explore produces（无 gate → done）
    _touch(tmp_path, ".qa.yaml")  # case produces（无 gate → done）
    st = compute_status(SCHEMA, loc_for(tmp_path), read_state_or(tmp_path), {})
    assert st.terminal is not None and st.terminal.kind == "completed"


def test_scope_execute_marks_full_only_phase_out_of_scope(tmp_path: Path):
    """P0: --scope execute 生效 → 仅 full 的 intake（produces 未生成）out_of_scope，execution 仍可调度。"""
    st = compute_status(HEAL_SCHEMA, loc_for(tmp_path), WorkflowState(), {}, scope="execute")
    assert _pv(st, "intake").status == "out_of_scope"
    assert _pv(st, "execution").status == "ready"
    assert "intake" not in [d.phase_id for d in st.next_dispatch]


def test_needs_fix_routes_to_healing_not_terminal(tmp_path: Path):
    """P0: execution produces 已生成 + gate=needs_fix → awaiting_gate（不终止）；fix-proposal 经 gate() when → ready。"""
    _write_fa(tmp_path, eligible=True)
    _touch(tmp_path, "execution/execution-manifest.yaml")  # execution 已运行（produces 存在）
    _touch(tmp_path, "proposal.md")  # intake 已完成（无 gate → done）
    st = compute_status(HEAL_SCHEMA, loc_for(tmp_path), WorkflowState(), {})
    assert st.terminal is None
    ev = _pv(st, "execution")
    assert ev.status == "awaiting_gate" and ev.gate_verdict == "needs_fix"
    assert _pv(st, "fix-proposal").status == "ready"
    assert [d.phase_id for d in st.next_dispatch] == ["fix-proposal"]


# Regression: execution must wait for ALL active codegen suites, not fire on the
# first one. A sibling codegen blocked behind a pending plan-review gate must
# hold execution back; `any_active` let execution run early against a missing
# test file (API SKIPPED, false `completed`, archive blocked).
EXEC_ALL_SCHEMA = parse_schema("""
schema_version: "1"
name: exec-all
params:
  test_types: { type: list, default: [api, e2e] }
phases:
  - id: api-plan-review
    skill: null
    requires: []
    produces: [review/api-plan-review.json]
    gate: api-plan-review-gate
    when: "'api' in params.test_types"
  - id: api-codegen
    skill: aa-api-codegen
    agent: aa-test-author
    requires: [api-plan-review]
    produces: [codegen/api-codegen-summary.md]
    when: "'api' in params.test_types"
  - id: e2e-plan-review
    skill: null
    requires: []
    produces: [review/plan-review.json]
    gate: e2e-plan-review-gate
    when: "'e2e' in params.test_types"
  - id: e2e-codegen
    skill: aa-e2e-codegen
    agent: aa-test-author
    requires: [e2e-plan-review]
    produces: [codegen/e2e-codegen-summary.md]
    when: "'e2e' in params.test_types"
  - id: execution
    skill: null
    requires: [api-codegen, e2e-codegen]
    requires_mode: all
    produces: [execution/execution-manifest.yaml]
gates:
  api-plan-review-gate:
    reads: [review/api-plan-review.json]
    needs_human_review_when: "decision == 'needs_human_review'"
    pass_when: "decision == 'pass'"
  e2e-plan-review-gate:
    reads: [review/plan-review.json]
    pass_when: "decision == 'pass'"
""")


def test_execution_blocked_while_sibling_codegen_blocked(tmp_path: Path):
    """`all` mode: e2e-codegen done but api-codegen blocked (plan-review pending
    human review) → execution stays blocked, not ready."""
    loc = loc_for(tmp_path)
    # e2e branch fully done: review pass + codegen produced.
    (tmp_path / "review").mkdir(parents=True, exist_ok=True)
    (tmp_path / "review" / "plan-review.json").write_text(json.dumps({"decision": "pass"}))
    _touch(tmp_path, "codegen/e2e-codegen-summary.md")
    # api branch stuck: review produced but gate needs human review → api-codegen blocked.
    (tmp_path / "review" / "api-plan-review.json").write_text(json.dumps({"decision": "needs_human_review"}))
    st = compute_status(EXEC_ALL_SCHEMA, loc, WorkflowState(), {})
    assert _pv(st, "e2e-codegen").status == "done"
    assert _pv(st, "api-codegen").status == "blocked"
    assert _pv(st, "execution").status == "blocked"
    assert "execution" not in [d.phase_id for d in st.next_dispatch]


def test_execution_ready_once_all_active_codegen_done(tmp_path: Path):
    """Once the api plan-review clears and api-codegen produces its summary,
    execution becomes ready (both active suites done)."""
    loc = loc_for(tmp_path)
    (tmp_path / "review").mkdir(parents=True, exist_ok=True)
    (tmp_path / "review" / "plan-review.json").write_text(json.dumps({"decision": "pass"}))
    (tmp_path / "review" / "api-plan-review.json").write_text(json.dumps({"decision": "pass"}))
    _touch(tmp_path, "codegen/e2e-codegen-summary.md")
    _touch(tmp_path, "codegen/api-codegen-summary.md")
    st = compute_status(EXEC_ALL_SCHEMA, loc, WorkflowState(), {})
    assert _pv(st, "api-codegen").status == "done"
    assert _pv(st, "e2e-codegen").status == "done"
    assert _pv(st, "execution").status == "ready"


def test_execution_only_waits_for_active_suites(tmp_path: Path):
    """`all` over ACTIVE deps: with test_types=[api] only, e2e-codegen is pruned
    and does not block execution once api-codegen is done."""
    loc = loc_for(tmp_path)
    (tmp_path / "review").mkdir(parents=True, exist_ok=True)
    (tmp_path / "review" / "api-plan-review.json").write_text(json.dumps({"decision": "pass"}))
    _touch(tmp_path, "codegen/api-codegen-summary.md")
    st = compute_status(EXEC_ALL_SCHEMA, loc, WorkflowState(), {"test_types": ["api"]})
    assert _pv(st, "e2e-codegen").status == "pruned"
    assert _pv(st, "api-codegen").status == "done"
    assert _pv(st, "execution").status == "ready"


def test_packaged_execution_requires_all_active_codegen():
    """Guard: the packaged schema must keep execution at requires_mode=all.
    Reverting to any_active reintroduces the premature-execution / API-SKIPPED bug."""
    from assurance_agent.workflow.orchestration.schema import load_workflow_schema

    schema = load_workflow_schema(Path("/nonexistent-project-root"))
    execution = next(p for p in schema.phases if p.id == "execution")
    assert execution.requires_mode == "all", (
        "execution must wait for ALL active codegen suites; any_active lets it fire "
        "on the first sibling and run against a missing test file (API SKIPPED)."
    )
