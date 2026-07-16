"""Domain operations: guard, replay, and kill-prefix reconciliation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import append_event_strict, read_events
from assurance_agent.workflow.core.state import read_state, state_guard, write_state
from assurance_agent.workflow.orchestration.healing_episode import HealingAttemptIntent
from assurance_agent.workflow.orchestration.operations import (
    StaleDispatchError,
    allocate_healing_attempt,
    apply_phase_outcome,
    record_decision,
    record_dispatch,
    record_heal_transition,
)
from assurance_agent.workflow.orchestration.schema import parse_schema

_SCHEMA = parse_schema(
    """
schema_version: "1"
name: t
params:
  max_healing_attempts: { type: int, default: 3 }
phases:
  - id: explore
    skill: aa-explore
    agent: aa-doc-author
    requires: []
    produces: [explore/advisory.json]
  - id: skill-registry-check
    skill: null
    requires: []
    produces: []
gates: {}
"""
)


def _change(tmp_path: Path) -> Path:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_state(change, WorkflowState())
    return change


def _advisory(change: Path) -> None:
    path = change / "explore" / "advisory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def _phase_entry(change: Path, phase_id: str) -> dict:
    extra = read_state(change).phases.model_extra or {}
    entry = extra.get(phase_id)
    assert isinstance(entry, dict)
    return entry


def test_record_dispatch_replay_and_conflict(tmp_path: Path) -> None:
    change = _change(tmp_path)
    r1 = record_dispatch(change, phase_id="explore", kind="dispatch_phase", attempt_id="a1")
    assert r1.disposition == "committed"
    r2 = record_dispatch(change, phase_id="explore", kind="dispatch_phase", attempt_id="a1")
    assert r2.disposition == "replayed"
    assert len([e for e in read_events(change) if e["type"] == "dispatch_signed"]) == 1
    with pytest.raises(AaError, match="conflict"):
        record_dispatch(change, phase_id="other", kind="dispatch_phase", attempt_id="a1")


def test_apply_outcome_guard_stale_and_manual(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _advisory(change)
    receipt = record_dispatch(change, phase_id="explore", kind="dispatch_phase", attempt_id="a1")
    # Drift the signed guard by writing state outside the dispatch txn.
    write_state(
        change,
        WorkflowState.model_validate({"phases": {"noise": {"status": "done"}}}),
    )
    assert state_guard(change) != receipt.state_guard
    with pytest.raises(StaleDispatchError):
        apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="a1")
    assert not any(e["type"] == "phase_outcome_committed" for e in read_events(change))

    # Manual outcome skips guard / dispatch lookup.
    applied = apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id=None)
    assert applied.disposition == "committed"
    assert applied.attempt_id.startswith("manual:")


def test_apply_outcome_driver_happy_and_replay(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _advisory(change)
    record_dispatch(change, phase_id="explore", kind="dispatch_phase", attempt_id="a1")
    first = apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="a1")
    assert first.disposition == "committed"
    assert first.applied_status == "done"
    entry = _phase_entry(change, "explore")
    assert entry["attempt_id"] == "a1"
    second = apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="a1")
    assert second.disposition == "replayed"
    assert len([e for e in read_events(change) if e["type"] == "phase_outcome_committed"]) == 1


def test_apply_outcome_reconcile_event_without_marker(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _advisory(change)
    append_event_strict(
        change,
        {
            "source": "progression",
            "type": "phase_outcome_committed",
            "phase": "explore",
            "attempt_id": "a1",
            "gate_report": None,
        },
    )
    # State has no explore marker — legitimate kill-prefix.
    result = apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="a1")
    assert result.disposition == "reconciled"
    assert _phase_entry(change, "explore")["attempt_id"] == "a1"
    assert len([e for e in read_events(change) if e["type"] == "phase_outcome_committed"]) == 1


def test_apply_outcome_superseded_keeps_newer_marker(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _advisory(change)
    append_event_strict(
        change,
        {
            "source": "progression",
            "type": "phase_outcome_committed",
            "phase": "explore",
            "attempt_id": "old",
            "gate_report": None,
        },
    )
    append_event_strict(
        change,
        {
            "source": "progression",
            "type": "phase_outcome_committed",
            "phase": "explore",
            "attempt_id": "new",
            "gate_report": None,
        },
    )
    write_state(
        change,
        WorkflowState.model_validate({"phases": {"explore": {"status": "done", "attempt_id": "new"}}}),
    )
    result = apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="old")
    assert result.disposition == "superseded"
    assert _phase_entry(change, "explore")["attempt_id"] == "new"


def test_apply_outcome_missing_dispatch_fail_closed(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _advisory(change)
    with pytest.raises(AaError, match="no matching dispatch"):
        apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="ghost")


def test_apply_orchestrator_internal_status_pass(tmp_path: Path) -> None:
    change = _change(tmp_path)
    result = apply_phase_outcome(tmp_path, change, _SCHEMA, "skill-registry-check", attempt_id=None)
    assert result.applied_status == "pass"


def test_allocate_healing_full_replay_and_prefixes(tmp_path: Path) -> None:
    change = _change(tmp_path)
    intent = HealingAttemptIntent(
        episode_id="ep1",
        attempt_id="ha1",
        attempt_number=1,
        operation_id="op1",
        source_batch_id="b1",
        pin_entry_baseline=True,
    )
    r1 = allocate_healing_attempt(change, intent)
    assert r1.disposition == "committed"
    assert (change / "healing" / "entry-baseline.json").is_file()
    r2 = allocate_healing_attempt(change, intent)
    assert r2.disposition == "replayed"
    assert len([e for e in read_events(change) if e["type"] == "healing_attempt_allocated"]) == 1

    # File-only prefix on a fresh change.
    change2 = tmp_path / "qa" / "changes" / "CH-2"
    change2.mkdir(parents=True)
    write_state(change2, WorkflowState())
    payload = {
        "schema_version": "1.0",
        "episode_id": "ep2",
        "entry_batch_id": "b2",
    }
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    baseline = change2 / "healing" / "entry-baseline.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes(data)
    intent2 = HealingAttemptIntent(
        episode_id="ep2",
        attempt_id="ha2",
        attempt_number=1,
        operation_id="op2",
        source_batch_id="b2",
        pin_entry_baseline=True,
    )
    r3 = allocate_healing_attempt(change2, intent2)
    assert r3.disposition == "reconciled"
    types = [e["type"] for e in read_events(change2)]
    assert types == ["healing_entry_baseline_pinned", "healing_attempt_allocated"]

    # Baseline-event-only prefix (file missing).
    change3 = tmp_path / "qa" / "changes" / "CH-3"
    change3.mkdir(parents=True)
    write_state(change3, WorkflowState())
    payload3 = {
        "schema_version": "1.0",
        "episode_id": "ep3",
        "entry_batch_id": "b3",
    }
    data3 = (json.dumps(payload3, sort_keys=True, separators=(",", ":")) + "\n").encode()
    sha3 = hashlib.sha256(data3).hexdigest()
    append_event_strict(
        change3,
        {
            "source": "heal",
            "type": "healing_entry_baseline_pinned",
            "artifact_file": "healing/entry-baseline.json",
            "artifact_sha256": sha3,
            "entry_batch_id": "b3",
            "episode_id": "ep3",
        },
    )
    intent3 = HealingAttemptIntent(
        episode_id="ep3",
        attempt_id="ha3",
        attempt_number=1,
        operation_id="op3",
        source_batch_id="b3",
        pin_entry_baseline=True,
    )
    r4 = allocate_healing_attempt(change3, intent3)
    assert r4.disposition == "reconciled"
    assert (change3 / "healing" / "entry-baseline.json").read_bytes() == data3
    assert len([e for e in read_events(change3) if e["type"] == "healing_attempt_allocated"]) == 1


def test_allocate_conflict(tmp_path: Path) -> None:
    change = _change(tmp_path)
    intent = HealingAttemptIntent(
        episode_id="ep1",
        attempt_id="ha1",
        attempt_number=1,
        operation_id="op1",
        source_batch_id="b1",
        pin_entry_baseline=False,
    )
    allocate_healing_attempt(change, intent)
    with pytest.raises(AaError, match="conflict"):
        allocate_healing_attempt(
            change,
            intent.model_copy(update={"attempt_id": "other"}),
        )


def test_heal_transition_replay_and_reconcile(tmp_path: Path) -> None:
    change = _change(tmp_path)
    t1 = record_heal_transition(change, "resolved")
    assert t1.disposition == "committed"
    t2 = record_heal_transition(change, "resolved")
    assert t2.disposition == "replayed"
    # Event present, state lagging.
    write_state(change, WorkflowState())  # resets healing status to pending
    t3 = record_heal_transition(change, "resolved")
    assert t3.disposition == "reconciled"
    assert read_state(change).phases.healing.status == "resolved"
    assert len([e for e in read_events(change) if e["type"] == "heal_transition"]) == 1


def test_record_decision_stop_and_repeat(tmp_path: Path) -> None:
    # Override packaged schema so registry-gate does not mark the DAG stopped.
    schema_dir = tmp_path / ".aa"
    schema_dir.mkdir()
    schema_dir.joinpath("workflow-schema.yaml").write_text(
        """
schema_version: "1"
name: t
params:
  max_healing_attempts: { type: int, default: 3 }
phases:
  - id: explore
    skill: aa-explore
    agent: aa-doc-author
    requires: []
    produces: [explore/advisory.json]
gates: {}
""",
        encoding="utf-8",
    )
    change = _change(tmp_path)
    record_decision(
        tmp_path,
        change,
        checkpoint="gate",
        action="accept_risk",
        reason="ok",
        who="tester",
    )
    assert len(read_events(change)) == 1
    record_decision(
        tmp_path,
        change,
        checkpoint="gate",
        action="accept_risk",
        reason="again",
        who="tester",
    )
    assert len([e for e in read_events(change) if e["type"] == "human_decision"]) == 2

    record_decision(
        tmp_path,
        change,
        checkpoint="gate",
        action="stop",
        reason="halt",
        who="tester",
    )
    assert read_state(change).model_dump().get("terminal") is not None
    with pytest.raises(AaError, match="already terminal"):
        record_decision(
            tmp_path,
            change,
            checkpoint="gate",
            action="stop",
            reason="again",
            who="tester",
        )
