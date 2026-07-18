"""Bounded review->fix->re-review loop: projection + engine integration + schema."""

import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.review_fix_episode import project_review_fix_loop
from assurance_agent.workflow.orchestration.schema import SchemaError, parse_schema
from tests.helpers_aa import loc_for

SCHEMA = parse_schema("""
schema_version: "1"
name: rf
params:
  max_plan_fix_attempts: { type: int, default: 2 }
phases:
  - id: plan
    skill: aa-plan
    agent: aa-doc-author
    requires: []
    produces: [plans/plan.md]
  - id: review
    skill: aa-reviewer
    agent: aa-reviewer
    requires: [plan]
    produces: [review/review.json]
    gate: review-gate
  - id: fix
    skill: aa-fixer
    agent: aa-doc-author
    requires: [review]
    when: "gate('review-gate').verdict == 'needs_fix'"
    repair_of: review
    max_attempts_param: max_plan_fix_attempts
    loop: repair
  - id: codegen
    skill: aa-codegen
    agent: aa-test-author
    requires: [review]
    produces: [codegen/summary.md]
loops:
  repair:
    kind: review_fix
    members: [fix]
    review_phase: review
    fix_phase: fix
    gate: review-gate
    max_param: max_plan_fix_attempts
gates:
  review-gate:
    reads: [review/review.json]
    needs_fix_when: "decision == 'needs_fix' and auto_fix_allowed == true"
    pass_when: "decision == 'pass'"
""")

PARAMS = {"max_plan_fix_attempts": 2}
LOOP = SCHEMA.loops["repair"]


def _touch(change: Path, rel: str, payload: str = "{}") -> None:
    p = change / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload)


def _review(change: Path, decision: str, auto_fix: bool = True) -> None:
    _touch(change, "review/review.json", json.dumps({"decision": decision, "auto_fix_allowed": auto_fix}))


def _commit(change: Path, phase: str) -> None:
    append_event_strict(
        change,
        {
            "source": "progression",
            "type": "phase_outcome_committed",
            "phase": phase,
            "attempt_id": f"{phase}:x",
            "gate_report": None,
        },
    )


def _project(change: Path, *, review_active: bool = True):
    return project_review_fix_loop(
        SCHEMA, loc_for(change), WorkflowState(), PARAMS, LOOP, review_active=review_active
    )


# ---- projection ------------------------------------------------------------
def test_inactive_when_review_phase_out_of_scope(tmp_path: Path):
    _review(tmp_path, "needs_fix")
    assert _project(tmp_path, review_active=False).state == "inactive"


def test_inactive_before_first_review_artifact(tmp_path: Path):
    assert _project(tmp_path).state == "inactive"


def test_inactive_when_gate_passes(tmp_path: Path):
    _review(tmp_path, "pass")
    _commit(tmp_path, "review")
    assert _project(tmp_path).state == "inactive"


def test_needs_fix_dispatches_fixer_first(tmp_path: Path):
    _review(tmp_path, "needs_fix")
    _commit(tmp_path, "review")
    snap = _project(tmp_path)
    assert snap.state == "active"
    assert snap.dispatch == ["fix"]
    assert snap.attempts_used == 0


def test_fix_after_review_triggers_re_review(tmp_path: Path):
    _review(tmp_path, "needs_fix")
    _commit(tmp_path, "review")
    _commit(tmp_path, "fix")  # fix landed against current review
    snap = _project(tmp_path)
    assert snap.state == "active"
    assert snap.dispatch == ["review"]  # re-run reviewer, not another fix
    assert snap.attempts_used == 1


def test_exhausted_after_max_attempts_stops(tmp_path: Path):
    _review(tmp_path, "needs_fix")
    _commit(tmp_path, "review")
    _commit(tmp_path, "fix")
    _commit(tmp_path, "review")
    _commit(tmp_path, "fix")
    _commit(tmp_path, "review")  # latest review still needs_fix; 2 fixes used == max
    snap = _project(tmp_path)
    assert snap.state == "terminal"
    assert snap.terminal_kind == "stopped"
    assert snap.attempts_used == 2
    assert "attempts exhausted: 2/2" in (snap.reason or "")


def test_attempt_allowed_below_budget(tmp_path: Path):
    _review(tmp_path, "needs_fix")
    _commit(tmp_path, "review")
    _commit(tmp_path, "fix")
    _commit(tmp_path, "review")  # 1 fix used, budget 2 → another fix allowed
    snap = _project(tmp_path)
    assert snap.state == "active"
    assert snap.dispatch == ["fix"]
    assert snap.attempts_used == 1


# ---- engine integration ----------------------------------------------------
def test_engine_routes_to_fixer_and_is_not_terminal(tmp_path: Path):
    _touch(tmp_path, "plans/plan.md")
    _review(tmp_path, "needs_fix")
    _commit(tmp_path, "review")
    st = compute_status(SCHEMA, loc_for(tmp_path), WorkflowState(), PARAMS)
    assert st.terminal is None
    assert [d.phase_id for d in st.next_dispatch] == ["fix"]


def test_engine_re_review_then_pass_unblocks_codegen(tmp_path: Path):
    _touch(tmp_path, "plans/plan.md")
    _review(tmp_path, "needs_fix")
    _commit(tmp_path, "review")
    _commit(tmp_path, "fix")
    # reviewer re-run is what the loop asks for next
    st = compute_status(SCHEMA, loc_for(tmp_path), WorkflowState(), PARAMS)
    assert [d.phase_id for d in st.next_dispatch] == ["review"]
    # reviewer re-runs and now passes
    _review(tmp_path, "pass")
    _commit(tmp_path, "review")
    st = compute_status(SCHEMA, loc_for(tmp_path), WorkflowState(), PARAMS)
    assert st.terminal is None
    assert [d.phase_id for d in st.next_dispatch] == ["codegen"]


def test_engine_exhaustion_is_terminal_stopped(tmp_path: Path):
    _touch(tmp_path, "plans/plan.md")
    _review(tmp_path, "needs_fix")
    _commit(tmp_path, "review")
    _commit(tmp_path, "fix")
    _commit(tmp_path, "review")
    _commit(tmp_path, "fix")
    _commit(tmp_path, "review")
    st = compute_status(SCHEMA, loc_for(tmp_path), WorkflowState(), PARAMS)
    assert st.terminal is not None
    assert st.terminal.kind == "stopped"
    assert st.terminal.phase == "fix"
    assert st.next_dispatch == []


# ---- schema validation -----------------------------------------------------
def test_review_fix_loop_requires_gate_and_max_param():
    with pytest.raises(SchemaError):
        parse_schema("""
schema_version: "1"
name: bad
params: {}
phases:
  - id: review
    skill: aa-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/review.json]
    gate: g
  - id: fix
    skill: aa-fixer
    agent: aa-doc-author
    requires: [review]
    loop: repair
loops:
  repair:
    kind: review_fix
    members: [fix]
    review_phase: review
    fix_phase: fix
gates:
  g:
    reads: [review/review.json]
    pass_when: "decision == 'pass'"
""")
