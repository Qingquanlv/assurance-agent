"""``gate('x')`` resolution inside a frozen artifact view.

A gate adjudicated by a node in the same graph is frozen evidence and wins. A gate
adjudicated inside a *subgraph* leaves no node outcome in the parent's view, so the
reference has to be re-adjudicated against the same view — returning stop for that
case silently turns every cross-subgraph reference into a permanent block, which is
what happened to the ``*-codegen-precondition-gate`` family.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import yaml

from assurance_agent.workflow.core.events import append_event_strict
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from tests.helpers_graph_v6 import v6_started_bindings
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
    needs_human_review_when: "leaf.decision == 'needs_human_review'"
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


def test_reference_readjudication_applies_anchored_accept_risk(tmp_path: Path) -> None:
    """Parent hard gates must see a decision accepted inside the review subgraph."""
    context = _context(tmp_path)
    _write_leaf(context, "needs_human_review")
    review_path = context.change_dir / "review" / "leaf.json"
    coordinator_dir = tmp_path / "coordinator" / "CH-1"
    coordinator_dir.mkdir(parents=True)
    review_sha256 = hashlib.sha256(review_path.read_bytes()).hexdigest()
    append_event_strict(
        coordinator_dir,
        {
            "source": "graph",
            "type": "graph_interrupted",
            "invocation_id": "review-invocation",
            "checkpoint_ns": "root/review-cycle/review-invocation",
            "interrupt_id": "interrupt-1",
            "node_id": "human-review",
            "checkpoint": "leaf-gate",
            "actions": ["accept_risk", "stop"],
            "audited_reads_sha256": {"review/leaf.json": review_sha256},
            "source_gate_attempt_id": "ga-1",
            "source_gate_tree_id": "tree-src",
        },
    )
    append_event_strict(
        coordinator_dir,
        {
            "source": "graph",
            "type": "graph_resumed",
            "invocation_id": "root-invocation",
            "checkpoint_ns": "root-invocation",
            "interrupt_id": "interrupt-1",
            "action": "accept_risk",
            "reason": "benchmark accepted risk",
            "who": "benchmark",
            "audited_reads_sha256": {"review/leaf.json": review_sha256},
            "payload": {},
            "source_gate_attempt_id": "ga-1",
            "source_gate_tree_id": "tree-src",
        },
    )
    append_event_strict(
        coordinator_dir,
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": "review-invocation",
            "checkpoint_ns": "root/review-cycle/review-invocation",
            "superstep_id": "ss-1",
            "task_id": "gate-task",
            "attempt_id": "ga-1",
            "gate_report": {"gate_id": "leaf-gate", "verdict": "needs_human_review"},
        },
    )
    context = replace(
        context,
        audit_events_dir=coordinator_dir,
        committed_tree_id="tree-src",
        invocation_id="review-invocation",
    )

    report = check_gate_in_view(GATES, "referring-gate", context)

    assert report.verdict.value == "pass"

    _write_leaf(context, "reject")
    drifted = check_gate_in_view(GATES, "referring-gate", context)
    assert drifted.verdict.value == "stop"


def test_reference_readjudication_treats_corrupt_authoritative_ledger_as_no_acceptance(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _write_leaf(context, "needs_human_review")
    review_path = context.change_dir / "review" / "leaf.json"
    coordinator_dir = tmp_path / "coordinator" / "CH-1"
    coordinator_dir.mkdir(parents=True)
    review_sha256 = hashlib.sha256(review_path.read_bytes()).hexdigest()
    append_event_strict(
        coordinator_dir,
        {
            "source": "graph",
            "type": "graph_interrupted",
            "invocation_id": "review-invocation",
            "checkpoint_ns": "root/review-cycle/review-invocation",
            "interrupt_id": "interrupt-1",
            "node_id": "human-review",
            "checkpoint": "leaf-gate",
            "actions": ["accept_risk", "stop"],
            "audited_reads_sha256": {"review/leaf.json": review_sha256},
        },
    )
    append_event_strict(
        coordinator_dir,
        {
            "source": "graph",
            "type": "graph_resumed",
            "invocation_id": "root-invocation",
            "checkpoint_ns": "root-invocation",
            "interrupt_id": "interrupt-1",
            "action": "accept_risk",
            "reason": "benchmark accepted risk",
            "who": "benchmark",
            "audited_reads_sha256": {"review/leaf.json": review_sha256},
            "payload": {},
        },
    )
    with (coordinator_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{corrupt-json\n")
    context = replace(context, audit_events_dir=coordinator_dir)

    report = check_gate_in_view(GATES, "referring-gate", context)

    assert report.verdict.value == "stop"


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


def test_v5_decision_requires_matching_source_gate_epoch(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write_leaf(context, "needs_human_review")
    review_sha256 = hashlib.sha256((context.change_dir / "review" / "leaf.json").read_bytes()).hexdigest()
    append_event_strict(
        context.change_dir,
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "leaf",
            "entrypoint": "full",
            "graph_id": "g",
            "graph_digest": "dg",
            "contract_digests": {},
            **v6_started_bindings(),
            "params": {},
            "params_sha256": "",
            "root_tree_id": "tree-src",
            "max_parallel_tasks": 1,
            "checkpoint_ns": "leaf",
            "structural_path": "g",
        },
    )
    append_event_strict(
        context.change_dir,
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "superstep_id": "ss-1",
            "task_id": "gate-task",
            "attempt_id": "ga-1",
            "gate_report": {"gate_id": "leaf-gate", "verdict": "needs_human_review"},
        },
    )
    append_event_strict(
        context.change_dir,
        {
            "source": "graph",
            "type": "graph_interrupted",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "interrupt_id": "interrupt-1",
            "node_id": "human-review",
            "checkpoint": "leaf-gate",
            "actions": ["accept_risk", "stop"],
            "audited_reads_sha256": {"review/leaf.json": review_sha256},
            "source_gate_attempt_id": "ga-1",
            "source_gate_tree_id": "tree-src",
        },
    )
    append_event_strict(
        context.change_dir,
        {
            "source": "graph",
            "type": "graph_resumed",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "interrupt_id": "interrupt-1",
            "action": "accept_risk",
            "reason": "accepted",
            "who": "reviewer",
            "audited_reads_sha256": {"review/leaf.json": review_sha256},
            "payload": {},
            "source_gate_attempt_id": "ga-1",
            "source_gate_tree_id": "tree-src",
        },
    )

    matched = replace(
        context,
        audit_events_dir=context.change_dir,
        committed_tree_id="tree-src",
        event_schema_version=6,
        invocation_id="leaf",
    )
    assert check_gate_in_view(GATES, "leaf-gate", matched).verdict.value == "pass"

    mismatched_tree = replace(matched, committed_tree_id="tree-after-revision")
    assert check_gate_in_view(GATES, "leaf-gate", mismatched_tree).verdict.value == "needs_human_review"


def test_pairless_resume_is_ineligible_as_v5_gate_override(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _write_leaf(context, "needs_human_review")
    review_sha256 = hashlib.sha256((context.change_dir / "review" / "leaf.json").read_bytes()).hexdigest()
    append_event_strict(
        context.change_dir,
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "leaf",
            "entrypoint": "full",
            "graph_id": "g",
            "graph_digest": "dg",
            "contract_digests": {},
            **v6_started_bindings(),
            "params": {},
            "params_sha256": "",
            "root_tree_id": "tree-src",
            "max_parallel_tasks": 1,
            "checkpoint_ns": "leaf",
            "structural_path": "g",
        },
    )
    append_event_strict(
        context.change_dir,
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "superstep_id": "ss-1",
            "task_id": "gate-task",
            "attempt_id": "ga-1",
            "gate_report": {"gate_id": "leaf-gate", "verdict": "needs_human_review"},
        },
    )
    append_event_strict(
        context.change_dir,
        {
            "source": "graph",
            "type": "graph_interrupted",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "interrupt_id": "interrupt-1",
            "node_id": "human-review",
            "checkpoint": "leaf-gate",
            "actions": ["accept_risk", "stop"],
            "audited_reads_sha256": {"review/leaf.json": review_sha256},
        },
    )
    append_event_strict(
        context.change_dir,
        {
            "source": "graph",
            "type": "graph_resumed",
            "invocation_id": "leaf",
            "checkpoint_ns": "leaf",
            "interrupt_id": "interrupt-1",
            "action": "accept_risk",
            "reason": "accepted",
            "who": "reviewer",
            "audited_reads_sha256": {"review/leaf.json": review_sha256},
            "payload": {},
        },
    )
    context = replace(
        context,
        audit_events_dir=context.change_dir,
        committed_tree_id="tree-src",
        event_schema_version=6,
        invocation_id="leaf",
    )
    assert check_gate_in_view(GATES, "leaf-gate", context).verdict.value == "needs_human_review"
