"""operation:materialize-trace-projection — authoritative reconciled trace fold."""

from __future__ import annotations

from assurance_agent.evidence.trace import fold_trace
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.improvements.ledger import atomic_write_json


def materialize_trace_projection(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    del task
    projection = fold_trace(workspace.project_root, context.change_id, phase="reconciled")
    atomic_write_json(
        workspace.change_dir / "inspect" / "trace-projection.json",
        projection.model_dump(mode="json"),
    )
    return TaskResult(
        status="succeeded",
        value={"phase": projection.phase, "integrity": projection.integrity},
    )


__all__ = ["materialize_trace_projection"]
