"""Layer 1: write-side guards at progression commit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import append_event_best_effort, read_events
from assurance_agent.workflow.core.state import write_state
from assurance_agent.workflow.orchestration.operations import apply_phase_outcome
from assurance_agent.workflow.orchestration.schema import parse_schema

SCHEMA = parse_schema("""
schema_version: "1"
name: t
phases:
  - id: case-review
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: case-review-gate
  - id: case-fix
    skill: aa-case-fixer
    agent: aa-reviewer
    requires: []
    produces: []
    repair_of: case-review
  - id: explore
    skill: aa-explore
    agent: aa-doc-author
    requires: []
    produces: [explore/advisory.json]
gates:
  case-review-gate:
    reads: [review/case-review.json]
    needs_fix_when: "decision == 'needs_fix'"
    pass_when: "decision == 'pass'"
""")


def _change(tmp_path: Path) -> Path:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_state(change, WorkflowState())
    return change


def test_refuse_terminal_outcome_without_skill_attestation(tmp_path: Path) -> None:
    change = _change(tmp_path)
    (change / "explore").mkdir()
    (change / "explore" / "advisory.json").write_text("{}")

    with pytest.raises(AaError, match="SKILL_LOAD_GATE_VIOLATION"):
        apply_phase_outcome(tmp_path, change, SCHEMA, "explore", attempt_id=None, skill=None)

    assert not any(e["type"] == "phase_outcome_committed" for e in read_events(change))


def test_allow_terminal_outcome_with_skill_attestation(tmp_path: Path) -> None:
    change = _change(tmp_path)
    (change / "explore").mkdir()
    (change / "explore" / "advisory.json").write_text("{}")

    applied = apply_phase_outcome(
        tmp_path,
        change,
        SCHEMA,
        "explore",
        attempt_id=None,
        skill="aa-explore",
    )
    assert applied.disposition == "committed"


def test_refuse_needs_fix_to_pass_gate_verdict_without_repair(tmp_path: Path) -> None:
    change = _change(tmp_path)
    review = change / "review" / "case-review.json"
    review.parent.mkdir(parents=True)
    review.write_text(json.dumps({"decision": "pass"}))

    append_event_best_effort(
        change,
        {
            "source": "gate",
            "type": "gate_verdict",
            "phase": "case-review",
            "gate": "case-review-gate",
            "verdict": "needs_fix",
        },
    )

    with pytest.raises(AaError, match="GATE-TRANSITION-ILLEGAL"):
        apply_phase_outcome(
            tmp_path,
            change,
            SCHEMA,
            "case-review",
            attempt_id=None,
            skill="aa-case-reviewer",
        )

    assert not any(e["type"] == "phase_outcome_committed" for e in read_events(change))
