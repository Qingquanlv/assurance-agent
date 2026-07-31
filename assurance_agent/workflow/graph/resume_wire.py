"""S3 resume/interrupt wire helpers（v3 ResumeAnchor）。"""

from __future__ import annotations

from assurance_agent.workflow.core.graph_events import GraphInterruptedEvent, ResumeAnchor
from assurance_agent.workflow.graph.models import ExecutableTask, InterruptProjection


def interrupt_anchor(
    *,
    invocation_id: str,
    checkpoint_ns: str,
    interrupt: InterruptProjection,
) -> ResumeAnchor:
    return ResumeAnchor(
        invocation_id=invocation_id,
        checkpoint_ns=checkpoint_ns,
        node_id=interrupt.node_id,
        interrupt_id=interrupt.interrupt_id,
    )


def build_graph_interrupted_event(
    *,
    task: ExecutableTask,
    interrupt: InterruptProjection,
    event_schema_version: int,
    checkpoint_ns: str | None = None,
    parent_anchor_ref: str | None = None,
) -> GraphInterruptedEvent:
    ns = checkpoint_ns if checkpoint_ns is not None else interrupt.checkpoint_ns
    anchor = (
        interrupt_anchor(invocation_id=task.invocation_id, checkpoint_ns=ns, interrupt=interrupt)
        if event_schema_version >= 3
        else None
    )
    return GraphInterruptedEvent(
        type="graph_interrupted",
        invocation_id=task.invocation_id,
        checkpoint_ns=ns,
        interrupt_id=interrupt.interrupt_id,
        node_id=interrupt.node_id,
        checkpoint=interrupt.checkpoint,
        actions=list(interrupt.actions),
        audited_reads_sha256=dict(interrupt.audited_reads_sha256),
        artifact_view=interrupt.artifact_view,
        anchor=anchor,
        parent_anchor_ref=parent_anchor_ref,
        revision_owner_invocation_id=interrupt.revision_owner_invocation_id,
        revision_base_tree_id=interrupt.revision_base_tree_id,
        revision_view=interrupt.revision_view,
        revision_paths=list(interrupt.revision_paths) if interrupt.revision_paths is not None else None,
        revision_before_sha256=(
            dict(interrupt.revision_before_sha256) if interrupt.revision_before_sha256 is not None else None
        ),
        source_gate_attempt_id=interrupt.source_gate_attempt_id,
        source_gate_tree_id=interrupt.source_gate_tree_id,
    )


__all__ = ["build_graph_interrupted_event", "interrupt_anchor"]
