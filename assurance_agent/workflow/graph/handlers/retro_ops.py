"""Retro operation handlers for ``operation:retro-collect`` and ``operation:retro-accept``.

Both handlers delegate to the shared stage functions in ``assurance_agent.retro``
so the same logic serves the nightly driver and the graph runtime.
"""

from __future__ import annotations

from assurance_agent.exceptions import AaError
from assurance_agent.retro.accept_stage import run_retro_accept
from assurance_agent.retro.collect_stage import run_retro_collect
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace


def retro_collect(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Collect retro candidates and write ``qa/retro/<id>/context.json``."""
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    last = context.params.get("retro_last")
    try:
        last_int = int(last) if last is not None else 10
    except (TypeError, ValueError):
        last_int = 10
    try:
        result = run_retro_collect(
            context.project_root,
            retro_id=retro_id,
            last=last_int,
            write_root=workspace.project_root,
        )
    except (AaError, OSError) as err:
        return task_failure("internal", str(err))
    return TaskResult(
        status="succeeded",
        value={"retro_id": result.retro_id, "signal_count": result.signal_count},
    )


def retro_accept(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Validate proposals, write ``review-queue.md``, and mark the retro stage complete."""
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    min_evidence = context.params.get("retro_min_evidence")
    try:
        min_evidence_int = int(min_evidence) if min_evidence is not None else 2
    except (TypeError, ValueError):
        min_evidence_int = 2
    try:
        proposals = run_retro_accept(
            workspace.project_root,
            retro_id=retro_id,
            min_evidence=min_evidence_int,
        )
    except AaError as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(
        status="succeeded",
        value={"proposal_count": len(proposals)},
    )


__all__ = ["retro_collect", "retro_accept"]
