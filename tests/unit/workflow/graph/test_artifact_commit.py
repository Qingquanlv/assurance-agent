"""Shared tree-edge / interrupt-tree helpers for ArtifactCommitter."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.core.graph_events import (
    GraphInvocationStartedEvent,
    SuperstepCommittedEvent,
)
from assurance_agent.workflow.core.progression import transaction
from assurance_agent.workflow.graph.artifact_commit import (
    committed_tree_targets,
    last_committed_tree_edge,
    latest_resolved_interrupt_tree,
)
from assurance_agent.workflow.graph.models import GraphProjection, InterruptProjection
from tests.helpers_graph_v6 import v6_started_bindings

INV = "inv-root"


def _started() -> GraphInvocationStartedEvent:
    return GraphInvocationStartedEvent(
        type="graph_invocation_started",
        invocation_id=INV,
        entrypoint="full",
        graph_id="workflow",
        graph_digest="dg",
        contract_digests={},
        params={},
        params_sha256="",
        root_tree_id="tree-root",
        max_parallel_tasks=1,
        checkpoint_ns=INV,
        structural_path=INV,
        **v6_started_bindings(),
    )


def _committed(
    *,
    superstep_id: str,
    checkpoint_id: str,
    target_tree_id: str,
    write_set_ids: list[str],
) -> SuperstepCommittedEvent:
    return SuperstepCommittedEvent(
        type="superstep_committed",
        invocation_id=INV,
        checkpoint_ns=INV,
        superstep_id=superstep_id,
        checkpoint_id=checkpoint_id,
        write_set_ids=write_set_ids,
        target_tree_id=target_tree_id,
        state_values={},
        committed_task_ids=[],
    )


def _projection(**updates: object) -> GraphProjection:
    payload: dict[str, object] = {
        "invocation_id": INV,
        "entrypoint": "full",
        "checkpoint_ns": INV,
        "structural_path": INV,
        "graph_digest": "dg",
        "contract_digests": {},
        "params": {},
        "root_tree_id": "tree-root",
        "current_tree_id": "tree-root",
    }
    payload.update(updates)
    return GraphProjection.model_validate(payload)


def _seed(change_dir: Path, events: list[Mapping[str, object] | BaseModel]) -> None:
    change_dir.mkdir(parents=True, exist_ok=True)
    with transaction(change_dir) as txn:
        for event in events:
            txn.append_strict(event)


def test_last_committed_tree_edge_walks_superstep_commits(tmp_path: Path) -> None:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    _seed(
        change_dir,
        [
            _started(),
            _committed(
                superstep_id="ss-1",
                checkpoint_id="ck-1",
                target_tree_id="tree-a",
                write_set_ids=["ws-1"],
            ),
            _committed(
                superstep_id="ss-2",
                checkpoint_id="ck-2",
                target_tree_id="tree-b",
                write_set_ids=["ws-2", "ws-3"],
            ),
        ],
    )
    events = read_events_strict(change_dir)
    projection = _projection(current_tree_id="tree-b")

    edge = last_committed_tree_edge(events, projection)

    assert edge.prev_tree_id == "tree-a"
    assert edge.target_tree_id == "tree-b"
    assert edge.publication_id == "ck-2"
    assert edge.write_set_ids == ("ws-2", "ws-3")
    assert committed_tree_targets(events, INV) == ("tree-a", "tree-b")


def test_last_committed_tree_edge_empty_when_no_commits() -> None:
    projection = _projection()
    edge = last_committed_tree_edge([], projection)
    assert edge.prev_tree_id is None
    assert edge.target_tree_id is None
    assert edge.publication_id is None
    assert edge.write_set_ids == ()
    assert committed_tree_targets([], INV) == ()


def test_latest_resolved_interrupt_tree_is_leaf_owned_only() -> None:
    projection = _projection(
        interrupts={
            "parent-int": InterruptProjection(
                interrupt_id="parent-int",
                checkpoint_ns=f"{INV}/review/{INV}-child",
                node_id="review",
                checkpoint="gate",
                actions=("accept_risk",),
                audited_reads_sha256={},
                resolved_action="accept_risk",
                source_gate_tree_id="tree-child-gate",
            ),
            "leaf-int": InterruptProjection(
                interrupt_id="leaf-int",
                checkpoint_ns=INV,
                node_id="review",
                checkpoint="gate",
                actions=("accept_risk",),
                audited_reads_sha256={},
                resolved_action="accept_risk",
                source_gate_tree_id="tree-leaf-gate",
            ),
        }
    )

    assert latest_resolved_interrupt_tree(projection) == "tree-leaf-gate"


def test_latest_resolved_interrupt_tree_skips_unresolved() -> None:
    projection = _projection(
        interrupts={
            "open": InterruptProjection(
                interrupt_id="open",
                checkpoint_ns=INV,
                node_id="review",
                checkpoint="gate",
                actions=("accept_risk",),
                audited_reads_sha256={},
                source_gate_tree_id="tree-open",
            )
        }
    )
    assert latest_resolved_interrupt_tree(projection) is None
