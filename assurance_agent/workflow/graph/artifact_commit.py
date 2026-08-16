"""Shared committed-tree edge and ordinary materialization repair.

Runtime recovery and Scheduler selected-wave reserve both walk the same
``superstep_committed`` edges and rehydrate the same ordinary trees. Keep the
protocol in one place so a repair change cannot diverge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from assurance_agent.workflow.graph.models import GraphProjection, RuntimeContext
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceError


@dataclass(frozen=True, slots=True)
class CommittedTreeEdge:
    prev_tree_id: str | None
    target_tree_id: str | None
    publication_id: str | None
    write_set_ids: tuple[str, ...]


def last_committed_tree_edge(
    events: Sequence[Mapping[str, Any]],
    projection: GraphProjection,
) -> CommittedTreeEdge:
    """Last ``superstep_committed`` edge for ``projection.invocation_id``."""
    cursor = projection.root_tree_id
    last_prev: str | None = None
    last_target: str | None = None
    last_publication_id: str | None = None
    last_write_set_ids: tuple[str, ...] = ()
    for raw in events:
        if raw.get("source") != "graph" or raw.get("invocation_id") != projection.invocation_id:
            continue
        if raw.get("type") != "superstep_committed":
            continue
        target_tree = raw.get("target_tree_id")
        if isinstance(target_tree, str):
            last_prev = cursor
            last_target = target_tree
            raw_checkpoint_id = raw.get("checkpoint_id")
            last_publication_id = raw_checkpoint_id if isinstance(raw_checkpoint_id, str) else None
            raw_ids = raw.get("write_set_ids")
            last_write_set_ids = tuple(
                value for value in (raw_ids if isinstance(raw_ids, list) else []) if isinstance(value, str)
            )
            cursor = target_tree
    return CommittedTreeEdge(last_prev, last_target, last_publication_id, last_write_set_ids)


def committed_tree_targets(
    events: Sequence[Mapping[str, Any]],
    invocation_id: str,
) -> tuple[str, ...]:
    """All committed ``target_tree_id`` values for ``invocation_id``, in ledger order."""
    targets: list[str] = []
    for raw in events:
        if raw.get("source") != "graph" or raw.get("invocation_id") != invocation_id:
            continue
        if raw.get("type") != "superstep_committed":
            continue
        target = raw.get("target_tree_id")
        if isinstance(target, str):
            targets.append(target)
    return tuple(targets)


def latest_resolved_interrupt_tree(projection: GraphProjection) -> str | None:
    """Leaf-owned resolved interrupt gate tree, if the leaf is this invocation."""
    for interrupt in reversed(tuple(projection.interrupts.values())):
        if (
            interrupt.resolved_action is not None
            and interrupt.source_gate_tree_id is not None
            and interrupt.checkpoint_ns.rsplit("/", 1)[-1] == projection.invocation_id
        ):
            return interrupt.source_gate_tree_id
    return None


def ordinary_materialization_drift(
    store: TreeStore,
    projection: GraphProjection,
    context: RuntimeContext,
    events: Sequence[Mapping[str, Any]],
) -> bool:
    """True when live workspace bytes differ from the last ordinary committed tree."""
    edge = last_committed_tree_edge(events, projection)
    if edge.target_tree_id is None or edge.publication_id is None:
        return False
    if edge.write_set_ids:
        write_sets = [store.load_write_set(write_set_id) for write_set_id in edge.write_set_ids]
        if any(write_set.synchronized_paths for write_set in write_sets):
            return False
    try:
        current = store.capture(context.project_root, repo_root=context.repo_root)
    except WorkspaceError:
        return False
    return current != edge.target_tree_id


def repair_ordinary_materialization(
    store: TreeStore,
    projection: GraphProjection,
    context: RuntimeContext,
    events: Sequence[Mapping[str, Any]],
    *,
    tree_overlays: Mapping[str, str] | None = None,
) -> None:
    """Rehydrate the ledger-pinned ordinary tree for recovery or wave reserve.

    ``tree_overlays is None`` is the Runtime recovery path: skip when there is
    no ordinary drift; apply a resolved leaf gate tree only when it is the last
    committed target.

    A non-empty overlay map is the Scheduler selected-wave path: apply a
    leaf-owned gate tree first, then overlay-aware nested/sync repair.
    """
    overlays = dict(tree_overlays) if tree_overlays is not None else None
    if overlays is None and not ordinary_materialization_drift(store, projection, context, events):
        return

    if overlays is not None:
        gate_tree = latest_resolved_interrupt_tree(projection)
        if gate_tree is not None:
            store.apply_tree(
                context.project_root,
                gate_tree,
                base_tree_id=gate_tree,
                restore_change_drift=True,
            )
            return

    edge = last_committed_tree_edge(events, projection)
    if overlays is None:
        resumed_gate_tree = latest_resolved_interrupt_tree(projection)
        if resumed_gate_tree is not None and edge.target_tree_id == resumed_gate_tree:
            store.apply_tree(
                context.project_root,
                resumed_gate_tree,
                base_tree_id=resumed_gate_tree,
                restore_change_drift=True,
            )
            return
        target = edge.target_tree_id
        assert target is not None
        if projection.parent_task_id is not None:
            targets = committed_tree_targets(events, projection.invocation_id)
            store.apply_tree_delta(
                context.project_root,
                target,
                source_base_tree_id=projection.root_tree_id,
                destination_base_tree_id=projection.root_tree_id,
                acceptable_live_tree_ids=targets[:-1],
            )
            return
        base = edge.prev_tree_id if edge.prev_tree_id is not None else projection.root_tree_id
        store.apply_tree(
            context.project_root,
            target,
            base_tree_id=base,
            restore_change_drift=True,
        )
        return

    if edge.target_tree_id is None or edge.publication_id is None:
        return
    if edge.write_set_ids:
        write_sets = [store.load_write_set(write_set_id) for write_set_id in edge.write_set_ids]
        if any(write_set.synchronized_paths for write_set in write_sets):
            if projection.parent_task_id is None:
                return
            targets = committed_tree_targets(events, projection.invocation_id)
            source_base = overlays.get(projection.root_tree_id, projection.root_tree_id)
            overlaid_target = overlays.get(edge.target_tree_id, edge.target_tree_id)
            store.apply_tree_delta(
                context.project_root,
                overlaid_target,
                source_base_tree_id=source_base,
                destination_base_tree_id=source_base,
                acceptable_live_tree_ids=tuple(overlays.get(tree_id, tree_id) for tree_id in targets[:-1]),
            )
            return
    try:
        current = store.capture(context.project_root, repo_root=context.repo_root)
    except WorkspaceError:
        return
    if current == edge.target_tree_id:
        return
    base = edge.prev_tree_id if edge.prev_tree_id is not None else projection.root_tree_id
    store.apply_tree(
        context.project_root,
        overlays.get(edge.target_tree_id, edge.target_tree_id),
        base_tree_id=overlays.get(base, base),
        restore_change_drift=True,
    )


__all__ = [
    "CommittedTreeEdge",
    "committed_tree_targets",
    "last_committed_tree_edge",
    "latest_resolved_interrupt_tree",
    "ordinary_materialization_drift",
    "repair_ordinary_materialization",
]
