"""S0a：NodeHistory、migration、generation reducer。"""

from __future__ import annotations

import pytest

from assurance_agent.workflow.core.events import LedgerIntegrityError
from assurance_agent.workflow.graph.checkpoint import fold_invocation_events
from assurance_agent.workflow.graph.migrate_events import migrate_graph_event_stream
from assurance_agent.workflow.graph.models import GraphProjection
from assurance_agent.workflow.graph.node_history import node_history_key
from assurance_agent.workflow.graph.planner import _next_generation_ordinal
from tests.helpers_graph_v6 import v6_started_bindings


def _started(inv: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "graph_invocation_started",
        "invocation_id": inv,
        "entrypoint": "full",
        "graph_id": "main",
        "graph_digest": "gd-1",
        "contract_digests": {"skill:noop": "cd-1"},
        **v6_started_bindings(),
        "params": {"run_mode": "full"},
        "params_sha256": "ps-1",
        "root_tree_id": "tree-0",
        "max_parallel_tasks": 2,
        "checkpoint_ns": inv,
        "structural_path": "main",
    }


def test_migration_backfills_generation_ordinal_on_activate_and_skip() -> None:
    graph_only = [
        {
            "type": "graph_invocation_started",
            "invocation_id": "inv-1",
            "entrypoint": "full",
            "graph_id": "main",
            "graph_digest": "gd-1",
            "contract_digests": {},
            **v6_started_bindings(),
            "params": {},
            "params_sha256": "ps",
            "root_tree_id": "tree-0",
            "max_parallel_tasks": 2,
            "checkpoint_ns": "inv-1",
            "structural_path": "main",
        },
        {
            "type": "node_activated",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "graph_id": "main",
            "node_id": "a",
            "activation_id": "act-0",
            "input_sha256": "in",
            "source_reads_sha256": {},
        },
        {
            "type": "node_skipped",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "graph_id": "main",
            "node_id": "b",
            "expression": "false",
            "input_sha256": "in",
            "source_reads_sha256": {},
        },
    ]
    migrated = migrate_graph_event_stream(graph_only)
    assert migrated[1]["generation_ordinal"] == 0
    assert migrated[2]["generation_ordinal"] == 0


def test_fold_builds_node_history_from_activate_skip() -> None:
    events = [
        _started(),
        {
            "source": "graph",
            "type": "node_activated",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "graph_id": "main",
            "node_id": "alpha",
            "generation_ordinal": 0,
            "activation_id": "act-0",
            "input_sha256": "in",
            "source_reads_sha256": {},
        },
        {
            "source": "graph",
            "type": "node_skipped",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "graph_id": "main",
            "node_id": "beta",
            "generation_ordinal": 0,
            "expression": "false",
            "input_sha256": "in",
            "source_reads_sha256": {},
        },
    ]
    projection = fold_invocation_events("inv-1", events)
    key_a = node_history_key("inv-1", "main", "alpha")
    key_b = node_history_key("inv-1", "main", "beta")
    assert projection.node_histories[key_a].generations_by_ordinal[0].status == "activated"
    assert projection.node_histories[key_b].generations_by_ordinal[0].status == "skipped"


def test_activate_skip_idempotent_replay_is_noop() -> None:
    activated = {
        "source": "graph",
        "type": "node_activated",
        "invocation_id": "inv-1",
        "checkpoint_ns": "inv-1",
        "graph_id": "main",
        "node_id": "alpha",
        "generation_ordinal": 0,
        "activation_id": "act-0",
        "input_sha256": "in",
        "source_reads_sha256": {},
    }
    events = [_started(), activated, dict(activated)]
    projection = fold_invocation_events("inv-1", events)
    key = node_history_key("inv-1", "main", "alpha")
    assert projection.node_histories[key].latest_generation_ordinal == 0


def _skip(node_id: str, expression: str, source_reads: dict[str, str]) -> dict:
    return {
        "source": "graph",
        "type": "node_skipped",
        "invocation_id": "inv-1",
        "checkpoint_ns": "inv-1",
        "graph_id": "main",
        "node_id": node_id,
        "generation_ordinal": 0,
        "expression": expression,
        "input_sha256": "in",
        "source_reads_sha256": source_reads,
    }


def test_repeated_skip_tolerates_source_reads_drift() -> None:
    """同一代重发同一 skip 决策：上游新落盘文件改变 source_reads，不是冲突。"""
    events = [
        _started(),
        _skip("gamma", "(no incoming edge selected)", {"change:inspect/failure-analysis.json": "h1"}),
        _skip(
            "gamma",
            "(no incoming edge selected)",
            {"change:healing/fix-proposal.json": "h2", "change:inspect/failure-analysis.json": "h1"},
        ),
    ]
    projection = fold_invocation_events("inv-1", events)
    key = node_history_key("inv-1", "main", "gamma")
    assert projection.node_histories[key].generations_by_ordinal[0].status == "skipped"


def test_conflicting_skip_decision_in_same_generation_raises() -> None:
    events = [
        _started(),
        _skip("gamma", "(no incoming edge selected)", {}),
        _skip("gamma", "params.run_tests == false", {}),
    ]
    with pytest.raises(LedgerIntegrityError, match="node_skipped conflict for gamma"):
        fold_invocation_events("inv-1", events)


def test_conflicting_activation_in_same_generation_raises() -> None:
    def activated(activation_id: str) -> dict:
        return {
            "source": "graph",
            "type": "node_activated",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "graph_id": "main",
            "node_id": "alpha",
            "generation_ordinal": 0,
            "activation_id": activation_id,
            "input_sha256": "in",
            "source_reads_sha256": {},
        }

    with pytest.raises(LedgerIntegrityError, match="node_activated conflict for alpha"):
        fold_invocation_events("inv-1", [_started(), activated("act-0"), activated("act-1")])


def test_next_generation_ordinal_uses_node_history() -> None:
    key = node_history_key("inv-1", "main", "loop")
    projection = GraphProjection(
        invocation_id="inv-1",
        entrypoint="full",
        checkpoint_ns="inv-1",
        structural_path="main",
        graph_digest="gd",
        contract_digests={},
        params={},
        root_tree_id="t0",
        current_tree_id="t0",
        node_histories={
            key: __import__("assurance_agent.workflow.graph.models", fromlist=["NodeHistory"]).NodeHistory(
                latest_generation_ordinal=1, generations_by_ordinal={}
            ),
        },
    )
    assert _next_generation_ordinal(projection, "main", "loop") == 2


def test_invocation_started_maps_ir_digest() -> None:
    projection = fold_invocation_events("inv-1", [_started()])
    assert projection.ir_digest == "gd-1"
    assert projection.event_schema_version == 6


def _task_started(node_id: str, task_id: str) -> dict:
    return {
        "source": "graph",
        "type": "task_attempt_started",
        "invocation_id": "inv-1",
        "checkpoint_ns": "inv-1",
        "superstep_id": "ss-1",
        "task_id": task_id,
        "attempt_id": f"{task_id}-a0",
        "node_id": node_id,
        "input_sha256": "in",
        "graph_digest": "gd-1",
        "contract_digest": "cd-1",
        "attempt_number": 1,
        "lease_expires_at": "2026-07-24T00:00:00Z",
        "started_at": "2026-07-24T00:00:00Z",
    }


def _graph_interrupted(node_id: str, ns: str = "inv-1") -> dict:
    return {
        "source": "graph",
        "type": "graph_interrupted",
        "invocation_id": "inv-1",
        "checkpoint_ns": ns,
        "interrupt_id": "int-1",
        "node_id": node_id,
        "checkpoint": "cp-1",
        "actions": ["accept", "reject"],
        "audited_reads_sha256": {},
    }


def test_graph_interrupt_drives_generation_status_interrupted() -> None:
    events = [
        _started(),
        {
            "source": "graph",
            "type": "node_activated",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "graph_id": "main",
            "node_id": "review",
            "generation_ordinal": 0,
            "activation_id": "act-0",
            "input_sha256": "in",
            "source_reads_sha256": {},
        },
        {
            "source": "graph",
            "type": "superstep_planned",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "superstep_id": "ss-1",
            "checkpoint_id": "cp-0",
            "task_ids": ["review-task"],
        },
        _task_started("review", "review-task"),
        _graph_interrupted("review"),
    ]
    projection = fold_invocation_events("inv-1", events)
    key = node_history_key("inv-1", "main", "review")
    assert projection.node_histories[key].generations_by_ordinal[0].status == "interrupted"


def test_fan_out_generation_interrupted_by_graph_interrupted() -> None:
    events = [
        _started(),
        {
            "source": "graph",
            "type": "node_activated",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "graph_id": "main",
            "node_id": "fan",
            "generation_ordinal": 0,
            "activation_id": "act-0",
            "input_sha256": "in",
            "source_reads_sha256": {},
        },
        {
            "source": "graph",
            "type": "fan_out_expanded",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "graph_id": "main",
            "node_id": "fan",
            "generation_ordinal": 0,
            "source_reads_sha256": {},
            "items": ["a", "b"],
            "task_keys": ["a", "b"],
            "task_ids": ["fan-a", "fan-b"],
        },
        {
            "source": "graph",
            "type": "superstep_planned",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "superstep_id": "ss-1",
            "checkpoint_id": "cp-0",
            "task_ids": ["fan-a", "fan-b"],
        },
        _task_started("fan", "fan-a"),
        _task_started("fan", "fan-b"),
        _graph_interrupted("fan"),
    ]
    projection = fold_invocation_events("inv-1", events)
    key = node_history_key("inv-1", "main", "fan")
    assert projection.node_histories[key].generations_by_ordinal[0].status == "interrupted"


def test_nested_child_interrupt_does_not_touch_parent_generation() -> None:
    events = [
        _started(),
        {
            "source": "graph",
            "type": "node_activated",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "graph_id": "main",
            "node_id": "sub",
            "generation_ordinal": 0,
            "activation_id": "act-0",
            "input_sha256": "in",
            "source_reads_sha256": {},
        },
        {
            "source": "graph",
            "type": "superstep_planned",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "superstep_id": "ss-1",
            "checkpoint_id": "cp-0",
            "task_ids": ["sub-task"],
        },
        _task_started("sub", "sub-task"),
        # interrupt bubbled from a nested child invocation (different ns) must not
        # drive this graph's generation to interrupted.
        _graph_interrupted("deep-node", ns="inv-1/sub/child-inv"),
    ]
    projection = fold_invocation_events("inv-1", events)
    key = node_history_key("inv-1", "main", "sub")
    assert projection.node_histories[key].generations_by_ordinal[0].status == "running"
