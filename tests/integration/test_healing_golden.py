"""healing 两机制 golden：打包 gate 锚点 + event-derived 全链路推演。"""

import json
from pathlib import Path

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.gates import check_gate
from assurance_agent.workflow.orchestration.healing_episode import HealingEpisodeAction
from assurance_agent.workflow.orchestration.healing_state import derive_healing_state
from assurance_agent.workflow.orchestration.schema import load_workflow_schema, parse_schema
from tests.helpers_aa import loc_for


def _mk_change(tmp_path: Path) -> Path:
    change = tmp_path / "qa" / "changes" / "C1"
    (change / "inspect").mkdir(parents=True)
    return change


def _loc(change: Path):
    # change = <root>/qa/changes/C1 → project_root is <root>.
    return loc_for(change, project_root=change.parents[2])


def _j(change: Path, rel: str, payload) -> None:
    p = change / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload))


def _touch(change: Path, rel: str) -> None:
    p = change / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")


def _pv(st, pid):
    return next(p for p in st.phases if p.id == pid)


def _ready(st):
    return [d.phase_id for d in st.next_dispatch]


def _event(change: Path, payload: dict) -> None:
    append_event_strict(change, payload)


def _outcome(change: Path, phase: str, attempt_id: str) -> None:
    _event(
        change,
        {
            "source": "progression",
            "type": "phase_outcome_committed",
            "phase": phase,
            "attempt_id": attempt_id,
            "gate_report": None,
        },
    )


def _allocate(change: Path, number: int, episode_id: str = "e1") -> None:
    if number == 1:
        _event(
            change,
            {
                "source": "heal",
                "type": "healing_entry_baseline_pinned",
                "artifact_file": "healing/entry-baseline.json",
                "artifact_sha256": "x",
                "entry_batch_id": "b1",
                "episode_id": episode_id,
            },
        )
    _event(
        change,
        {
            "source": "progression",
            "type": "healing_attempt_allocated",
            "episode_id": episode_id,
            "attempt_id": f"ha{number}",
            "attempt_number": number,
            "operation_id": f"op{number}",
            "source_batch_id": "b1",
        },
    )


def _commit_allocation_intent(change: Path, action: HealingEpisodeAction) -> None:
    intent = action.allocation
    assert intent is not None
    if intent.pin_entry_baseline:
        _event(
            change,
            {
                "source": "heal",
                "type": "healing_entry_baseline_pinned",
                "artifact_file": "healing/entry-baseline.json",
                "artifact_sha256": "x",
                "entry_batch_id": intent.source_batch_id,
                "episode_id": intent.episode_id,
            },
        )
    _event(
        change,
        {
            "source": "progression",
            "type": "healing_attempt_allocated",
            "episode_id": intent.episode_id,
            "attempt_id": intent.attempt_id,
            "attempt_number": intent.attempt_number,
            "operation_id": intent.operation_id,
            "source_batch_id": intent.source_batch_id,
        },
    )


def _apply(change: Path, number: int) -> None:
    _event(
        change,
        {
            "source": "heal",
            "type": "heal_record_apply",
            "target": "api",
            "proposal_sha256": f"p{number}",
            "source_batch_id": "b1",
            "attempt_key": f"p{number}:b1",
            "summary_sha256": f"s{number}",
            "files_modified": ["tests/test_api.py"],
        },
    )


def _state(change: Path, execution_status: str = "FAIL") -> WorkflowState:
    base = WorkflowState.model_validate(
        {
            "phases": {
                "execution": {"status": execution_status, "batch_id": "b1"},
                "inspect": {"inspect_mode": "primary"},
            },
            "gates": {"healing_available": True},
        }
    )
    data = base.model_dump(mode="python", exclude_none=True)
    data["phases"]["healing"] = derive_healing_state(change).model_dump(mode="python", exclude_none=True)
    return WorkflowState.model_validate(data)


# ---------- 打包 schema 锚点：真实 healing gate 三态 ----------
def test_packaged_healing_entry_enters_on_eligible_failure(tmp_path: Path):
    schema = load_workflow_schema(tmp_path)
    change = _mk_change(tmp_path)
    _j(change, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    state = _state(change)
    assert (
        check_gate(schema, "healing-entry-gate", _loc(change), state, {"max_healing_attempts": 3}).verdict
        == "enter"
    )


def test_packaged_healing_loop_exits_on_pass(tmp_path: Path):
    schema = load_workflow_schema(tmp_path)
    change = _mk_change(tmp_path)
    _j(change, "inspect/failure-analysis.json", {"failures": []})
    _allocate(change, 1)
    state = _state(change, execution_status="PASS")
    assert (
        check_gate(schema, "healing-loop-gate", _loc(change), state, {"max_healing_attempts": 3}).verdict
        == "exit"
    )


def test_packaged_healing_loop_stops_when_exhausted(tmp_path: Path):
    schema = load_workflow_schema(tmp_path)
    change = _mk_change(tmp_path)
    _j(change, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _allocate(change, 1)
    _allocate(change, 2)
    _allocate(change, 3)
    state = _state(change)
    assert (
        check_gate(schema, "healing-loop-gate", _loc(change), state, {"max_healing_attempts": 3}).verdict
        == "stop"
    )


def test_compute_status_dispatches_registry_on_fresh_change(tmp_path: Path):
    schema = load_workflow_schema(tmp_path)
    change = _mk_change(tmp_path)
    st = compute_status(schema, _loc(change), WorkflowState(), {})
    assert "skill-registry-check" in _ready(st)


# ---------- 合成 schema：两机制全链路推演 ----------
GOLDEN = parse_schema("""
schema_version: "1"
name: golden
params:
  max_case_fix_attempts: { type: int, default: 2 }
  max_healing_attempts: { type: int, default: 2 }
phases:
  # repair_of 修复环
  - id: case-review
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: case-review-gate
  - id: case-fix
    skill: aa-case-fixer
    agent: aa-doc-author
    requires: [case-review]
    produces: [review/case-review-apply-summary.md]
    when: "gate('case-review-gate').verdict == 'needs_fix'"
    repair_of: case-review
    max_attempts_param: max_case_fix_attempts
  # healing 治愈环（execution 无 gate；FAIL 由 failure-analysis + healing-entry-gate 表达）
  - id: execution
    skill: null
    requires: [case-review]
    produces: [execution/execution-manifest.yaml]
  - id: inspect
    skill: aa-inspect
    agent: aa-reviewer
    requires: [execution]
    produces: [inspect/failure-analysis.json, inspect/quality-gate-result.json]
  - id: fix-proposal
    skill: aa-fix-proposal
    agent: aa-doc-author
    requires: [inspect]
    produces: [healing/fix-proposal.json]
    when: "gate('healing-entry-gate').verdict == 'enter'"
    loop: healing
  - id: api-codegen-fix
    skill: aa-api-codegen-fixer
    agent: aa-test-author
    requires: [fix-proposal]
    produces: [healing/api-apply-summary.json]
    when: "any(fix_proposal.proposals, target == 'api' and eligible == true)"
    loop: healing
  - id: healing-rerun
    skill: null
    requires: [api-codegen-fix]
    produces: [execution/execution-manifest.yaml]
    gate: fixer-safety-gate
    loop: healing
  - id: healing-reinspect
    skill: aa-inspect
    agent: aa-reviewer
    requires: [healing-rerun]
    produces: [inspect/failure-analysis.json, inspect/quality-gate-result.json]
    loop: healing
gates:
  case-review-gate:
    reads: [review/case-review.json]
    needs_fix_when: "decision == 'needs_fix' and auto_fix_allowed == true"
    pass_when: "decision == 'pass'"
  healing-entry-gate:
    reads: [{ path: inspect/failure-analysis.json, as: failure_analysis }]
    enter_when: "any(failure_analysis.failures, fix_proposal_eligible == true) and state.phases.healing.attempts_used < params.max_healing_attempts"
    skip_when: "not any(failure_analysis.failures, fix_proposal_eligible == true)"
  fixer-safety-gate:
    reads: [healing/fixer-safety-check.json]
    missing_file_is: stop
    needs_human_review_when: "needs_review == true"
    pass_when: "passed == true"
  healing-loop-gate:
    reads: [{ path: inspect/failure-analysis.json, as: failure_analysis }]
    exit_when: "not any(failure_analysis.failures, fix_proposal_eligible == true)"
    continue_when: "any(failure_analysis.failures, fix_proposal_eligible == true) and state.phases.healing.attempts_used < params.max_healing_attempts"
    stop_when: "state.phases.healing.attempts_used >= params.max_healing_attempts"
loops:
  healing:
    members: [fix-proposal, api-codegen-fix, healing-rerun, healing-reinspect]
    counter: state.phases.healing.attempts_used
    max_param: max_healing_attempts
    allocate_on: "file_exists('healing/fix-proposal.json') and fix_proposal.summary.eligible_count > 0"
    exit_gate: healing-loop-gate
""")


def test_golden_full_progression_enter_rerun_exit(tmp_path: Path):
    change = _mk_change(tmp_path)

    # Step 1 — case-review 判 needs_fix → case-fix 经 repair 路由变 ready（下游 execution 被阻塞）
    _j(change, "review/case-review.json", {"decision": "needs_fix", "auto_fix_allowed": True})
    st = compute_status(GOLDEN, _loc(change), _state(change), {})
    assert _pv(st, "case-review").gate_verdict == "needs_fix"
    assert _pv(st, "case-fix").status == "ready"
    assert _ready(st) == ["case-fix"]
    assert _pv(st, "execution").status == "blocked"

    # Step 2 — driver apply 修复 + 复评 pass → case-review done、case-fix pruned、execution ready
    _touch(change, "review/case-review-apply-summary.md")
    _j(change, "review/case-review.json", {"decision": "pass"})
    st = compute_status(GOLDEN, _loc(change), _state(change), {})
    assert _pv(st, "case-review").status == "done"
    assert _pv(st, "case-fix").status == "pruned"
    assert _pv(st, "execution").status == "ready"

    # Step 3 — 已有 execution/inspect canonical 产物仍应进入 episode。
    _touch(change, "execution/execution-manifest.yaml")
    _j(change, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(change, "inspect/quality-gate-result.json", {"decision": "fail"})
    st = compute_status(GOLDEN, _loc(change), _state(change), {})
    assert st.healing_episode.stage == "proposal"
    assert _ready(st) == ["fix-proposal"]

    # Step 4 — proposal outcome 只产生 allocate 写意图；driver 再严格追加 baseline/allocation。
    _j(
        change,
        "healing/fix-proposal.json",
        {"summary": {"eligible_count": 1}, "proposals": [{"target": "api", "eligible": True}]},
    )
    _outcome(change, "fix-proposal", "p1")
    st = compute_status(GOLDEN, _loc(change), _state(change), {})
    assert st.healing_episode.next_actions[0].kind == "allocate_attempt"
    _commit_allocation_intent(change, st.healing_episode.next_actions[0])

    # Step 5 — apply audit 完成后，即使旧 execution manifest 已存在也必须重派 rerun。
    _apply(change, 1)
    _touch(change, "healing/api-apply-summary.json")
    _j(change, "healing/fixer-safety-check.json", {"passed": True, "needs_review": False})
    st = compute_status(GOLDEN, _loc(change), _state(change), {})
    assert st.healing_episode.stage == "rerun"
    assert _ready(st) == ["healing-rerun"]

    # Step 6 — rerun outcome 后，旧 inspect JSON 也不能跳过 reinspect。
    _outcome(change, "healing-rerun", "r1")
    st = compute_status(GOLDEN, _loc(change), _state(change), {})
    assert st.healing_episode.stage == "reinspect"
    assert _ready(st) == ["healing-reinspect"]

    # Step 7 — reinspect commit 后覆盖 canonical failure-analysis，无 eligible → resolved。
    _j(change, "inspect/failure-analysis.json", {"failures": []})
    _j(change, "inspect/quality-gate-result.json", {"decision": "pass"})
    _outcome(change, "healing-reinspect", "i1")
    st = compute_status(GOLDEN, _loc(change), _state(change), {})
    assert st.healing_episode.next_actions[0].outcome == "resolved"
    assert st.terminal is not None and st.terminal.kind == "completed"


def test_golden_second_attempt_exhausts_exactly_from_events(tmp_path: Path):
    change = _mk_change(tmp_path)
    _j(change, "review/case-review.json", {"decision": "pass"})
    _touch(change, "execution/execution-manifest.yaml")
    _j(change, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(change, "inspect/quality-gate-result.json", {"decision": "fail"})
    _j(
        change,
        "healing/fix-proposal.json",
        {"summary": {"eligible_count": 1}, "proposals": [{"target": "api", "eligible": True}]},
    )
    _j(change, "healing/fixer-safety-check.json", {"passed": True, "needs_review": False})
    _touch(change, "healing/api-apply-summary.json")

    _outcome(change, "fix-proposal", "p1")
    _allocate(change, 1)
    _apply(change, 1)
    _outcome(change, "healing-rerun", "r1")
    _outcome(change, "healing-reinspect", "i1")
    st = compute_status(GOLDEN, _loc(change), _state(change), {})
    assert st.healing_episode.stage == "proposal"

    _outcome(change, "fix-proposal", "p2")
    pending = compute_status(GOLDEN, _loc(change), _state(change), {})
    action = pending.healing_episode.next_actions[0]
    assert action.allocation is not None
    repeated = compute_status(GOLDEN, _loc(change), _state(change), {}).healing_episode.next_actions[0]
    assert repeated.allocation is not None
    assert action.allocation.operation_id == repeated.allocation.operation_id
    assert action.allocation.pin_entry_baseline is False
    _commit_allocation_intent(change, action)
    _apply(change, 2)
    _outcome(change, "healing-rerun", "r2")
    _outcome(change, "healing-reinspect", "i2")

    stopped = compute_status(GOLDEN, _loc(change), _state(change), {})
    assert derive_healing_state(change).attempts_used == 2
    assert stopped.healing_episode.terminal_kind == "stopped"
    assert "2/2" in (stopped.healing_episode.reason or "")
    assert stopped.terminal is not None and stopped.terminal.kind == "stopped"


def test_golden_latest_human_stop_overrides_dispatch(tmp_path: Path):
    change = _mk_change(tmp_path)
    _event(
        change,
        {
            "source": "decide",
            "type": "human_decision",
            "checkpoint": "workflow",
            "action": "stop",
            "reason": "operator halt",
            "who": "reviewer",
        },
    )

    status = compute_status(GOLDEN, _loc(change), _state(change), {})

    assert status.terminal is not None
    assert status.terminal.kind == "stopped"
    assert status.terminal.reason == "operator halt"
    assert status.terminal.phase == "workflow"
    assert status.next_dispatch == []


def test_golden_later_non_stop_decision_resumes_projection(tmp_path: Path):
    change = _mk_change(tmp_path)
    _event(
        change,
        {
            "source": "decide",
            "type": "human_decision",
            "checkpoint": "workflow",
            "action": "stop",
            "reason": "pause",
            "who": "reviewer",
        },
    )
    _event(
        change,
        {
            "source": "decide",
            "type": "human_decision",
            "checkpoint": "workflow",
            "action": "fix_and_proceed",
            "reason": "resume",
            "who": "reviewer",
        },
    )

    status = compute_status(GOLDEN, _loc(change), _state(change), {})

    assert status.terminal is None or status.terminal.kind != "stopped"
    assert status.next_dispatch
