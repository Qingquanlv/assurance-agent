"""Retro operation handlers for ``operation:retro-collect`` and ``operation:retro-accept``.

Both handlers delegate to the shared stage functions in ``assurance_agent.retro``
so the same logic serves the nightly driver and the graph runtime.
"""

from __future__ import annotations

from assurance_agent.exceptions import AaError
from assurance_agent.retro.accept_stage import run_retro_accept
from assurance_agent.retro.collect_stage import run_retro_collect
from assurance_agent.retro.window import RetroWindowSelection
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace


def _selection_from_params(params: dict) -> RetroWindowSelection:
    change_ids = params.get("retro_change_ids") or params.get("change_ids")
    since = params.get("retro_since") or params.get("since")
    until = params.get("retro_until") or params.get("until")
    last = params.get("retro_last") or params.get("last")

    if isinstance(change_ids, (list, tuple)) and change_ids:
        return RetroWindowSelection(change_ids=tuple(str(item) for item in change_ids), last=None)
    if isinstance(since, str) or isinstance(until, str):
        return RetroWindowSelection(
            since=since if isinstance(since, str) else None,
            until=until if isinstance(until, str) else None,
            last=None,
        )
    try:
        last_int = int(last) if isinstance(last, (int, str)) else 10
    except (TypeError, ValueError):
        last_int = 10
    return RetroWindowSelection(last=max(last_int, 1))


def retro_collect(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
    """Collect retro evidence and write ``qa/retro/<id>/context.json``."""
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    try:
        selection = _selection_from_params(context.params)
        result = run_retro_collect(
            context.project_root,
            retro_id=retro_id,
            selection=selection,
            write_root=workspace.project_root,
        )
    except (AaError, OSError, ValueError) as err:
        return task_failure("internal", str(err))
    return TaskResult(
        status="succeeded",
        value={"retro_id": result.retro_id, "signal_count": result.signal_count},
    )


def retro_accept(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
    """Reconcile Candidates into the Improvement Ledger and write accept receipt."""
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    min_evidence = context.params.get("retro_min_evidence")
    try:
        min_evidence_int = int(min_evidence) if isinstance(min_evidence, (int, str)) else 2
    except (TypeError, ValueError):
        min_evidence_int = 2
    try:
        receipt = run_retro_accept(
            workspace.project_root,
            retro_id=retro_id,
            min_evidence=min_evidence_int,
        )
    except AaError as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(
        status="succeeded",
        value={
            "result": receipt.result,
            "improvement_count": len(receipt.improvement_ids),
            "candidate_batch_digest": receipt.candidate_batch_digest,
        },
    )


__all__ = ["retro_collect", "retro_accept"]
