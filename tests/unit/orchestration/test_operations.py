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
from assurance_agent.workflow.orchestration.gates import check_gate
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
from tests.helpers_aa import loc_for

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
        apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="a1", skill="aa-explore")
    assert not any(e["type"] == "phase_outcome_committed" for e in read_events(change))

    # Manual outcome skips guard / dispatch lookup.
    applied = apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id=None, skill="aa-explore")
    assert applied.disposition == "committed"
    assert applied.attempt_id.startswith("manual:")


def test_apply_outcome_driver_happy_and_replay(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _advisory(change)
    record_dispatch(change, phase_id="explore", kind="dispatch_phase", attempt_id="a1")
    first = apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="a1", skill="aa-explore")
    assert first.disposition == "committed"
    assert first.applied_status == "done"
    entry = _phase_entry(change, "explore")
    assert entry["attempt_id"] == "a1"
    second = apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="a1", skill="aa-explore")
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
    result = apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="a1", skill="aa-explore")
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
    result = apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="old", skill="aa-explore")
    assert result.disposition == "superseded"
    assert _phase_entry(change, "explore")["attempt_id"] == "new"


def test_apply_outcome_missing_dispatch_fail_closed(tmp_path: Path) -> None:
    change = _change(tmp_path)
    _advisory(change)
    with pytest.raises(AaError, match="no matching dispatch"):
        apply_phase_outcome(tmp_path, change, _SCHEMA, "explore", attempt_id="ghost", skill="aa-explore")


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


def test_record_decision_binds_current_audited_gate_read(tmp_path: Path) -> None:
    """A gate accept_risk decision auto-binds the current audited review artifact."""
    import hashlib
    import json

    schema_dir = tmp_path / ".aa"
    schema_dir.mkdir()
    schema_dir.joinpath("workflow-schema.yaml").write_text(
        """
schema_version: "1"
name: t
phases:
  - id: api-plan-review
    skill: aa-api-plan-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/api-plan-review.json]
    gate: api-plan-review-gate
gates:
  api-plan-review-gate:
    reads: [review/api-plan-review.json]
    needs_human_review_when: "decision == 'needs_human_review'"
    pass_when: "decision == 'pass'"
""",
        encoding="utf-8",
    )
    change = _change(tmp_path)
    review = change / "review" / "api-plan-review.json"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(json.dumps({"decision": "needs_human_review"}), encoding="utf-8")
    expected_sha = hashlib.sha256(review.read_bytes()).hexdigest()

    record_decision(
        tmp_path,
        change,
        checkpoint="api-plan-review-gate",
        action="accept_risk",
        reason="benchmark accept",
        who="tester",
    )
    decision = [e for e in read_events(change) if e["type"] == "human_decision"][-1]
    assert decision["review_file"] == "review/api-plan-review.json"
    assert decision["review_sha256"] == expected_sha


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
gates:
  gate:
    reads: [workflow-state.yaml]
    pass_when: "file_exists('workflow-state.yaml')"
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


_GATED_SCHEMA = parse_schema(
    """
schema_version: "1"
name: t
phases:
  - id: case-review
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: case-review-gate
gates:
  case-review-gate:
    reads: [review/case-review.json]
    pass_when: "decision == 'pass'"
"""
)


def test_apply_outcome_records_gate_verdict_with_reads_sha256(tmp_path: Path) -> None:
    change = _change(tmp_path)
    review = change / "review" / "case-review.json"
    review.parent.mkdir(parents=True, exist_ok=True)
    payload = b'{"decision":"pass"}'
    review.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    applied = apply_phase_outcome(
        tmp_path,
        change,
        _GATED_SCHEMA,
        "case-review",
        attempt_id=None,
        skill="aa-case-reviewer",
    )
    assert applied.disposition == "committed"

    events = read_events(change)
    gate_events = [e for e in events if e["type"] == "gate_verdict"]
    assert len(gate_events) == 1
    assert gate_events[0]["gate"] == "case-review-gate"
    assert gate_events[0]["verdict"] == "pass"
    assert gate_events[0]["reads_sha256"] == {"review/case-review.json": expected}

    committed = [e for e in events if e["type"] == "phase_outcome_committed"]
    assert committed[-1]["gate_report"]["reads_sha256"] == {"review/case-review.json": expected}


# ── healing.safety decisions bind the fixer safety artifact (TS decide.ts) ────


def _fixer_safety_artifact(change: Path) -> str:
    path = change / "healing" / "fixer-safety-check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "change_id": change.name,
        "modified_files": ["tests/api/test_cases_api.py"],
        "product_code_modified": False,
        "assertion_expected_value_changes_detected": False,
        "skip_or_xfail_added": "undetermined",
        "unrelated_tests_modified": False,
        "high_risk_proposal_applied": False,
        "needs_review": True,
        "passed": False,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_record_decision_healing_safety_binds_fixer_safety_check(tmp_path: Path) -> None:
    """accept_risk at healing.safety binds the safety artifact (TS requireChangeArtifact)."""
    change = _change(tmp_path)
    expected_sha = _fixer_safety_artifact(change)

    record_decision(
        tmp_path,
        change,
        checkpoint="healing.safety",
        action="accept_risk",
        reason="accept documented skip marker",
        who="tester",
    )
    decision = [e for e in read_events(change) if e["type"] == "human_decision"][-1]
    assert decision["review_file"] == "healing/fixer-safety-check.json"
    assert decision["review_sha256"] == expected_sha


def test_record_decision_healing_safety_requires_artifact(tmp_path: Path) -> None:
    """Missing safety artifact fails closed before any event is recorded."""
    change = _change(tmp_path)
    with pytest.raises(AaError, match=r"healing/fixer-safety-check\.json is required"):
        record_decision(
            tmp_path,
            change,
            checkpoint="healing.safety",
            action="accept_risk",
            reason="no artifact yet",
            who="tester",
        )
    assert not [e for e in read_events(change) if e["type"] == "human_decision"]


def test_record_decision_healing_safety_rejects_fix_and_proceed(tmp_path: Path) -> None:
    """healing.safety only supports accept_risk/stop (TS SPECIAL_DECISION_SUPPORT)."""
    change = _change(tmp_path)
    _fixer_safety_artifact(change)
    with pytest.raises(AaError, match="does not support action 'fix_and_proceed'"):
        record_decision(
            tmp_path,
            change,
            checkpoint="healing.safety",
            action="fix_and_proceed",
            reason="not a safety acceptance",
            who="tester",
        )
    assert not [e for e in read_events(change) if e["type"] == "human_decision"]


_FIXER_SAFETY_SCHEMA = parse_schema(
    """
schema_version: "1"
name: t
phases:
  - id: healing-rerun
    skill: null
    requires: []
    produces: [execution/execution-manifest.yaml]
    gate: fixer-safety-gate
gates:
  fixer-safety-gate:
    reads: [healing/fixer-safety-check.json]
    missing_file_is: stop
    needs_human_review_when: "passed == false or needs_review == true"
    pass_when: "passed == true"
"""
)


def test_healing_safety_accept_risk_unblocks_fixer_safety_gate(tmp_path: Path) -> None:
    """Minimal end-to-end: decide at healing.safety turns the gate verdict to pass."""
    change = _change(tmp_path)
    _fixer_safety_artifact(change)
    loc = loc_for(change)

    before = check_gate(_FIXER_SAFETY_SCHEMA, "fixer-safety-gate", loc, WorkflowState(), {})
    assert before.verdict == "needs_human_review"

    record_decision(
        tmp_path,
        change,
        checkpoint="healing.safety",
        action="accept_risk",
        reason="accept documented skip marker",
        who="tester",
    )
    after = check_gate(_FIXER_SAFETY_SCHEMA, "fixer-safety-gate", loc, WorkflowState(), {})
    assert after.verdict == "pass"
    assert "human_decision:accept_risk" in (after.matched_rule or "")


# ── decision-support matrix (TS decision_support.ts / decide.ts) ────────────

_MATRIX_SCHEMA_YAML = """
schema_version: "1"
name: t
phases:
  - id: explore
    skill: aa-explore
    agent: aa-doc-author
    requires: []
    produces: [explore/advisory.json]
  - id: case-review
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: case-review-gate
  - id: lint
    skill: null
    requires: []
    produces: []
    gate: lint-gate
gates:
  case-review-gate:
    reads: [review/case-review.json]
    needs_human_review_when: "decision == 'needs_human_review'"
    pass_when: "decision == 'pass'"
  lint-gate:
    reads: [workflow-state.yaml]
    pass_when: "file_exists('workflow-state.yaml')"
"""


def _matrix_change(tmp_path: Path) -> Path:
    schema_dir = tmp_path / ".aa"
    schema_dir.mkdir()
    schema_dir.joinpath("workflow-schema.yaml").write_text(_MATRIX_SCHEMA_YAML, encoding="utf-8")
    return _change(tmp_path)


def _case_review_artifact(change: Path, decision: str = "needs_human_review") -> str:
    path = change / "review" / "case-review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"decision": decision}), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _human_decisions(change: Path) -> list[dict]:
    return [e for e in read_events(change) if e["type"] == "human_decision"]


def test_record_decision_unknown_checkpoint_rejected(tmp_path: Path) -> None:
    change = _matrix_change(tmp_path)
    with pytest.raises(AaError, match="Unknown checkpoint 'nope'"):
        record_decision(
            tmp_path,
            change,
            checkpoint="nope",
            action="stop",
            reason="halt",
            who="tester",
        )
    assert not _human_decisions(change)


def test_record_decision_bare_phase_rejects_gate_actions(tmp_path: Path) -> None:
    change = _matrix_change(tmp_path)
    for action in ("accept_risk", "fix_and_proceed"):
        with pytest.raises(
            AaError,
            match=f"Unsupported decision: checkpoint 'explore' does not support action '{action}'",
        ):
            record_decision(
                tmp_path,
                change,
                checkpoint="explore",
                action=action,
                reason="not gated",
                who="tester",
            )
    assert not _human_decisions(change)


def test_record_decision_bare_phase_stop_allowed(tmp_path: Path) -> None:
    change = _matrix_change(tmp_path)
    _case_review_artifact(change, decision="pass")  # keep gates non-terminal
    record_decision(
        tmp_path,
        change,
        checkpoint="explore",
        action="stop",
        reason="halt",
        who="tester",
    )
    assert len(_human_decisions(change)) == 1
    assert read_state(change).model_dump().get("terminal") is not None


def test_record_decision_gated_phase_binds_audited_read(tmp_path: Path) -> None:
    """fix_and_proceed at a gated phase resolves its gate and binds the artifact."""
    change = _matrix_change(tmp_path)
    expected_sha = _case_review_artifact(change)
    record_decision(
        tmp_path,
        change,
        checkpoint="case-review",
        action="fix_and_proceed",
        reason="fix forward",
        who="tester",
    )
    decision = _human_decisions(change)[-1]
    assert decision["review_file"] == "review/case-review.json"
    assert decision["review_sha256"] == expected_sha


def test_record_decision_execution_test_changes_matrix(tmp_path: Path) -> None:
    change = _matrix_change(tmp_path)
    record_decision(
        tmp_path,
        change,
        checkpoint="execution.test-changes",
        action="allow_test_changes",
        reason="authorize current tree",
        who="tester",
    )
    assert len(_human_decisions(change)) == 1
    for action in ("accept_risk", "fix_and_proceed"):
        with pytest.raises(AaError, match="does not support action"):
            record_decision(
                tmp_path,
                change,
                checkpoint="execution.test-changes",
                action=action,
                reason="not supported here",
                who="tester",
            )
    assert len(_human_decisions(change)) == 1


def test_record_decision_bootstrap_matrix(tmp_path: Path) -> None:
    change = _matrix_change(tmp_path)
    record_decision(
        tmp_path,
        change,
        checkpoint="bootstrap",
        action="skip_branch",
        reason="skip optional branch",
        who="tester",
    )
    assert len(_human_decisions(change)) == 1
    for action in ("accept_risk", "fix_and_proceed"):
        with pytest.raises(AaError, match="does not support action"):
            record_decision(
                tmp_path,
                change,
                checkpoint="bootstrap",
                action=action,
                reason="not supported here",
                who="tester",
            )
    assert len(_human_decisions(change)) == 1


# ── audited gate decisions must bind the audited artifact (TS decide.ts) ────


def test_record_decision_audited_gate_requires_artifact(tmp_path: Path) -> None:
    """Non-stop gate decision without the audited artifact fails closed, no event."""
    change = _matrix_change(tmp_path)
    for action in ("accept_risk", "fix_and_proceed"):
        with pytest.raises(
            AaError,
            match=r"Audited artifact review/case-review\.json is required for this decision",
        ):
            record_decision(
                tmp_path,
                change,
                checkpoint="case-review-gate",
                action=action,
                reason="no artifact yet",
                who="tester",
            )
    assert not _human_decisions(change)


def test_record_decision_stop_at_audited_gate_needs_no_artifact(tmp_path: Path) -> None:
    """stop is a terminal-status decision: no audited binding required."""
    change = _matrix_change(tmp_path)
    record_decision(
        tmp_path,
        change,
        checkpoint="case-review-gate",
        action="stop",
        reason="halt",
        who="tester",
    )
    decision = _human_decisions(change)[-1]
    assert decision["action"] == "stop"
    assert "review_file" not in decision


def test_record_decision_non_audited_gate_needs_no_artifact(tmp_path: Path) -> None:
    """Gates without audited reads accept decisions without binding (TS parity)."""
    change = _matrix_change(tmp_path)
    record_decision(
        tmp_path,
        change,
        checkpoint="lint-gate",
        action="accept_risk",
        reason="lint gate has no audited reads",
        who="tester",
    )
    decision = _human_decisions(change)[-1]
    assert "review_file" not in decision
