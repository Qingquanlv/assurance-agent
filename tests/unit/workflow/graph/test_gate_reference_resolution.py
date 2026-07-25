"""``gate('x')`` resolution inside a frozen artifact view.

A gate adjudicated by a node in the same graph is frozen evidence and wins. A gate
adjudicated inside a *subgraph* leaves no node outcome in the parent's view, so the
reference has to be re-adjudicated against the same view — returning stop for that
case silently turns every cross-subgraph reference into a permanent block, which is
what happened to the ``*-codegen-precondition-gate`` family.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from assurance_agent.workflow.orchestration.schema import normalize_gates

GATES = normalize_gates(
    yaml.safe_load(
        """
  leaf-gate:
    reads:
      - review/leaf.json
    invalid_json: stop
    missing_field_is: stop
    missing_file_is: stop
    reject_when: "leaf.decision == 'reject'"
    pass_when: "leaf.decision == 'pass'"
  referring-gate:
    reads: []
    stop_when: "gate('leaf-gate').verdict == 'reject'"
    pass_when: "gate('leaf-gate').verdict == 'pass'"
  cycle-a:
    reads: []
    pass_when: "gate('cycle-b').verdict == 'pass'"
  cycle-b:
    reads: []
    pass_when: "gate('cycle-a').verdict == 'pass'"
"""
    )
)


def _context(tmp_path: Path, *, node_results: dict | None = None) -> GateEvaluationContext:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True, exist_ok=True)
    return GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=change_dir,
        change_id="CH-1",
        params={},
        state_values={},
        node_results=node_results or {},
    )


def _write_leaf(context: GateEvaluationContext, decision: str) -> None:
    path = context.change_dir / "review" / "leaf.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"decision": decision}), encoding="utf-8")


def test_reference_is_readjudicated_when_no_node_froze_it(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write_leaf(context, "pass")

    report = check_gate_in_view(GATES, "referring-gate", context)

    assert report.verdict.value == "pass"


def test_frozen_node_outcome_wins_over_readjudication(tmp_path: Path) -> None:
    """The committed verdict is the evidence of record, even if the read drifted."""
    context = _context(
        tmp_path,
        node_results={"review": {"gate": {"gate_id": "leaf-gate", "verdict": "pass"}}},
    )
    _write_leaf(context, "reject")

    report = check_gate_in_view(GATES, "referring-gate", context)

    assert report.verdict.value == "pass"


def test_reference_fails_closed_when_referenced_read_is_missing(tmp_path: Path) -> None:
    context = _context(tmp_path)

    report = check_gate_in_view(GATES, "referring-gate", context)

    assert report.verdict.value == "stop"


def test_reference_cycle_fails_closed_without_recursing_forever(tmp_path: Path) -> None:
    context = _context(tmp_path)

    report = check_gate_in_view(GATES, "cycle-a", context)

    assert report.verdict.value == "stop"
