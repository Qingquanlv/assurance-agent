"""Read-side status audits (Layer 3)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.workflow.core.audit import apply_audits_to_report, run_status_audits
from assurance_agent.workflow.core.events import append_event_best_effort, append_event_strict
from assurance_agent.workflow.core.state import write_state
from assurance_agent.workflow.orchestration.engine import PhaseView, Terminal, WorkflowStatus
from assurance_agent.workflow.orchestration.schema import parse_schema
from tests.helpers_aa import loc_for

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
  - id: execution
    skill: null
    requires: []
    produces: [execution/execution-manifest.yaml]
gates:
  case-review-gate:
    reads: [review/case-review.json]
    needs_fix_when: "decision == 'needs_fix'"
    pass_when: "decision == 'pass'"
""")


def _status(*phases: PhaseView, terminal: Terminal | None = None) -> WorkflowStatus:
    return WorkflowStatus(phases=list(phases), next_dispatch=[], terminal=terminal)


def test_skill_load_violation_when_terminal_without_skill(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_state(
        change,
        WorkflowState.model_validate(
            {
                "phases": {
                    "explore": {"status": "done", "skill_loaded": False},
                    "execution": {"status": "PASS"},
                }
            }
        ),
    )
    report = _status(
        PhaseView(id="explore", status="done"),
        PhaseView(id="execution", status="done"),
    )
    loc = loc_for(change, project_root=tmp_path)
    audit = run_status_audits(loc, report, SCHEMA)
    assert any(
        i.code == "SKILL_LOAD_GATE_VIOLATION" and i.phase == "explore" for i in audit.issues
    )
    assert not any(i.phase == "execution" for i in audit.issues)
    adjusted = apply_audits_to_report(report, audit)
    assert adjusted.terminal is not None
    assert adjusted.terminal.kind == "stopped"


def test_artifact_tampered_when_settled_gate_read_hash_drifts(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_state(change, WorkflowState())
    review = change / "review" / "case-review.json"
    review.parent.mkdir(parents=True)
    review.write_bytes(b'{"decision":"pass"}')
    original = hashlib.sha256(b'{"decision":"pass"}').hexdigest()
    append_event_best_effort(
        change,
        {
            "source": "gate",
            "type": "gate_verdict",
            "phase": "case-review",
            "gate": "case-review-gate",
            "verdict": "pass",
            "reads_sha256": {"review/case-review.json": original},
        },
    )
    # Tamper after settlement.
    review.write_bytes(b'{"decision":"pass","tampered":true}')

    report = _status(
        PhaseView(id="case-review", status="done", gate="case-review-gate", gate_verdict="pass"),
    )
    loc = loc_for(change, project_root=tmp_path)
    audit = run_status_audits(loc, report, SCHEMA)
    assert any(i.code == "ARTIFACT-TAMPERED" and i.phase == "case-review" for i in audit.issues)
    adjusted = apply_audits_to_report(report, audit)
    assert adjusted.phases[0].status == "tampered"
    assert adjusted.terminal is not None and adjusted.terminal.kind == "stopped"


def test_gate_transition_illegal_needs_fix_to_pass_without_repair(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_state(change, WorkflowState())
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
    append_event_best_effort(
        change,
        {
            "source": "gate",
            "type": "gate_verdict",
            "phase": "case-review",
            "gate": "case-review-gate",
            "verdict": "pass",
        },
    )
    report = _status(PhaseView(id="case-review", status="done", gate="case-review-gate"))
    loc = loc_for(change, project_root=tmp_path)
    audit = run_status_audits(loc, report, SCHEMA)
    assert any(i.code == "GATE-TRANSITION-ILLEGAL" for i in audit.issues)


def test_reclassify_without_event(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_state(change, WorkflowState())
    inspect = change / "inspect"
    inspect.mkdir()
    (inspect / "failure-analysis.json").write_text(
        json.dumps(
            {
                "failures": [
                    {
                        "id": "FAIL-001",
                        "case_id": "TC_1",
                        "reclassified": {
                            "from": "test_data_failure",
                            "evidence": "x",
                            "at": "t",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = _status(PhaseView(id="inspect", status="done"))
    loc = loc_for(change, project_root=tmp_path)
    audit = run_status_audits(loc, report, SCHEMA)
    assert any(i.code == "RECLASSIFY-WITHOUT-EVENT" for i in audit.issues)


def test_state_integrity_folded_into_audit(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    write_state(change, WorkflowState.model_validate({"phases": {"explore": {"status": "done"}}}))
    # Corrupt integrity stamp without going through write_state.
    path = change / "workflow-state.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("done", "tampered"), encoding="utf-8")

    report = _status(PhaseView(id="explore", status="done"))
    loc = loc_for(change, project_root=tmp_path)
    audit = run_status_audits(loc, report, SCHEMA)
    assert any(i.code == "STATE-INTEGRITY-TAMPERED" for i in audit.issues)
