import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.orchestration.healing_episode import (
    HealingEpisodeAction,
    project_healing_episode,
)
from assurance_agent.workflow.orchestration.healing_state import derive_healing_state
from assurance_agent.workflow.orchestration.schema import parse_schema
from tests.helpers_aa import loc_for

FIXTURE = Path(__file__).parents[1] / "fixtures/healing-episode-schema.yaml"
SCHEMA = parse_schema(FIXTURE.read_text())


def _j(change: Path, rel: str, payload: object) -> None:
    path = change / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _event(change: Path, payload: dict) -> None:
    append_event_strict(change, payload)


def _outcome(change: Path, phase: str, attempt: str) -> None:
    _event(
        change,
        {
            "source": "progression",
            "type": "phase_outcome_committed",
            "phase": phase,
            "attempt_id": attempt,
            "gate_report": None,
        },
    )


def _allocate(change: Path, number: int) -> None:
    if number == 1:
        _event(
            change,
            {
                "source": "heal",
                "type": "healing_entry_baseline_pinned",
                "artifact_file": "healing/entry-baseline.json",
                "artifact_sha256": "x",
                "entry_batch_id": "b1",
                "episode_id": "e1",
            },
        )
    _event(
        change,
        {
            "source": "progression",
            "type": "healing_attempt_allocated",
            "episode_id": "e1",
            "attempt_id": f"ha{number}",
            "attempt_number": number,
            "operation_id": f"op{number}",
            "source_batch_id": "b1",
        },
    )


def _fail_state(*, healing_available: bool = True) -> WorkflowState:
    return WorkflowState.model_validate(
        {
            "phases": {
                "execution": {"status": "FAIL", "batch_id": "b1"},
                "inspect": {"inspect_mode": "primary"},
            },
            "gates": {"healing_available": healing_available},
        }
    )


def test_allocate_action_requires_complete_typed_intent():
    with pytest.raises(ValidationError):
        HealingEpisodeAction(kind="allocate_attempt")


def test_entry_gate_stop_emits_complete_failed(tmp_path: Path):
    """healing_available=false → entry stop_when → complete(failed), not bare stop."""
    _j(tmp_path, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    state = _fail_state(healing_available=False)
    healing = derive_healing_state(tmp_path)
    episode = project_healing_episode(SCHEMA, loc_for(tmp_path), state, {}, healing, SCHEMA.loops["healing"])
    assert episode.state == "terminal"
    assert episode.stage == "entry"
    assert episode.next_actions == [HealingEpisodeAction(kind="complete", outcome="failed")]


def test_allocate_on_false_emits_complete_failed(tmp_path: Path):
    """Proposal committed but eligible_count=0 → complete(failed) instead of stranding status."""
    _j(tmp_path, "execution/execution-manifest.yaml", {})
    _j(tmp_path, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(
        tmp_path,
        "healing/fix-proposal.json",
        {"summary": {"eligible_count": 0}, "proposals": []},
    )
    _outcome(tmp_path, "fix-proposal", "p1")
    state = _fail_state()
    healing = derive_healing_state(tmp_path)
    episode = project_healing_episode(SCHEMA, loc_for(tmp_path), state, {}, healing, SCHEMA.loops["healing"])
    assert episode.state == "terminal"
    assert episode.stage == "allocate"
    assert episode.next_actions == [HealingEpisodeAction(kind="complete", outcome="failed")]


def test_safety_gate_stop_emits_complete_failed(tmp_path: Path):
    """fixer-safety-gate stop (passed=false) → complete(failed) so report can unblock."""
    _j(tmp_path, "execution/execution-manifest.yaml", {})
    _j(tmp_path, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(
        tmp_path,
        "healing/fix-proposal.json",
        {"summary": {"eligible_count": 1}, "proposals": [{"target": "api", "eligible": True}]},
    )
    _j(tmp_path, "healing/fixer-safety-check.json", {"passed": False, "needs_review": False})
    _allocate(tmp_path, 1)
    _event(
        tmp_path,
        {
            "source": "heal",
            "type": "heal_record_apply",
            "target": "api",
            "proposal_sha256": "p",
            "source_batch_id": "b1",
            "attempt_key": "p:b1",
            "summary_sha256": "s",
            "files_modified": ["tests/x.py"],
        },
    )
    state = _fail_state()
    healing = derive_healing_state(tmp_path)
    episode = project_healing_episode(SCHEMA, loc_for(tmp_path), state, {}, healing, SCHEMA.loops["healing"])
    assert episode.state == "terminal"
    assert episode.stage == "safety"
    assert episode.next_actions == [HealingEpisodeAction(kind="complete", outcome="failed")]


def test_loop_exhausted_emits_complete_exhausted(tmp_path: Path):
    """Max attempts reached with still-eligible failures → complete(exhausted)."""
    _j(tmp_path, "execution/execution-manifest.yaml", {})
    _j(tmp_path, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(
        tmp_path,
        "healing/fix-proposal.json",
        {"summary": {"eligible_count": 1}, "proposals": [{"target": "api", "eligible": True}]},
    )
    _j(tmp_path, "healing/fixer-safety-check.json", {"passed": True, "needs_review": False})
    for n in (1, 2):
        _allocate(tmp_path, n)
        _event(
            tmp_path,
            {
                "source": "heal",
                "type": "heal_record_apply",
                "target": "api",
                "proposal_sha256": f"p{n}",
                "source_batch_id": "b1",
                "attempt_key": f"p{n}:b1",
                "summary_sha256": f"s{n}",
                "files_modified": ["tests/x.py"],
            },
        )
        _outcome(tmp_path, "healing-rerun", f"r{n}")
        _outcome(tmp_path, "healing-reinspect", f"i{n}")
    state = _fail_state()
    healing = derive_healing_state(tmp_path)
    assert healing.attempts_used == 2
    episode = project_healing_episode(SCHEMA, loc_for(tmp_path), state, {}, healing, SCHEMA.loops["healing"])
    assert episode.state == "terminal"
    assert episode.stage == "decide"
    assert episode.next_actions == [HealingEpisodeAction(kind="complete", outcome="exhausted")]


def test_terminal_status_short_circuits_episode(tmp_path: Path):
    """Once heal_transition is recorded, episode is inactive — no repeat complete."""
    _j(tmp_path, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _event(
        tmp_path,
        {
            "source": "status",
            "type": "heal_transition",
            "from": "pending",
            "to": "failed",
        },
    )
    state = _fail_state(healing_available=False)
    healing = derive_healing_state(tmp_path)
    assert healing.status == "failed"
    episode = project_healing_episode(SCHEMA, loc_for(tmp_path), state, {}, healing, SCHEMA.loops["healing"])
    assert episode.state == "inactive"
    assert episode.next_actions == []


def test_shared_old_outputs_do_not_skip_rerun_or_reinspect(tmp_path: Path):
    _j(tmp_path, "execution/execution-manifest.yaml", {})  # old execution output
    _j(tmp_path, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(tmp_path, "inspect/quality-gate-result.json", {})  # old inspect output
    _j(
        tmp_path,
        "healing/fix-proposal.json",
        {"summary": {"eligible_count": 1}, "proposals": [{"target": "api", "eligible": True}]},
    )
    _j(tmp_path, "healing/fixer-safety-check.json", {"passed": True, "needs_review": False})
    _allocate(tmp_path, 1)
    _event(
        tmp_path,
        {
            "source": "heal",
            "type": "heal_record_apply",
            "target": "api",
            "proposal_sha256": "p",
            "source_batch_id": "b1",
            "attempt_key": "p:b1",
            "summary_sha256": "s",
            "files_modified": ["tests/x.py"],
        },
    )
    state = WorkflowState.model_validate(
        {
            "phases": {
                "execution": {"status": "FAIL", "batch_id": "b1"},
                "inspect": {"inspect_mode": "primary"},
            },
            "gates": {"healing_available": True},
        }
    )
    healing = derive_healing_state(tmp_path)
    episode = project_healing_episode(SCHEMA, loc_for(tmp_path), state, {}, healing, SCHEMA.loops["healing"])
    assert episode.next_actions[0].phase == "healing-rerun"

    _outcome(tmp_path, "healing-rerun", "r1")
    episode = project_healing_episode(SCHEMA, loc_for(tmp_path), state, {}, healing, SCHEMA.loops["healing"])
    assert episode.next_actions[0].phase == "healing-reinspect"


def test_allocate_operation_id_is_stable_and_ignores_persisted_attempts(tmp_path: Path):
    _j(tmp_path, "execution/execution-manifest.yaml", {})
    _j(tmp_path, "inspect/failure-analysis.json", {"failures": [{"fix_proposal_eligible": True}]})
    _j(
        tmp_path,
        "healing/fix-proposal.json",
        {"summary": {"eligible_count": 1}, "proposals": [{"target": "api", "eligible": True}]},
    )
    _outcome(tmp_path, "fix-proposal", "p1")
    # 旧 state 中伪造的 attempts_used=99 不能覆盖 event-derived snapshot(0)。
    state = WorkflowState.model_validate(
        {
            "phases": {"execution": {"status": "FAIL", "batch_id": "b1"}, "healing": {"attempts_used": 99}},
            "gates": {"healing_available": True},
        }
    )
    healing = derive_healing_state(tmp_path)
    first = project_healing_episode(SCHEMA, loc_for(tmp_path), state, {}, healing, SCHEMA.loops["healing"])
    second = project_healing_episode(SCHEMA, loc_for(tmp_path), state, {}, healing, SCHEMA.loops["healing"])
    assert first.next_actions[0].kind == "allocate_attempt"
    assert first.next_actions[0].allocation is not None
    assert second.next_actions[0].allocation is not None
    assert first.next_actions[0].allocation.operation_id == second.next_actions[0].allocation.operation_id
    assert first.next_actions[0].allocation.pin_entry_baseline is True
    assert first.attempt_number == 0
