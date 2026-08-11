"""Report-domain graph operations (``operation:inspect`` / ``generate-report``)."""

from __future__ import annotations

from assurance_agent.workflow.execution.evidence import EvidenceError
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.report.report_builder import generate_report


def inspect_operation(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
    """Classify execution evidence and publish inspect artifacts (no spawn ``aa report inspect``)."""
    del task  # contract signature
    try:
        result = inspect_change(workspace.project_root, context.change_id)
    except EvidenceError as err:
        return task_failure("invalid_input", str(err))
    return TaskResult(
        status="succeeded",
        value={
            "batch_id": result.analysis.batch_id,
            "final_status": result.analysis.final_status,
            "status": result.analysis.status,
        },
    )


def generate_report_operation(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Generate the schema-1.1 report deterministically inside the task workspace."""
    del task  # contract signature
    try:
        result = generate_report(workspace.project_root, context.change_id)
    except (EvidenceError, FileNotFoundError, OSError, ValueError) as err:
        return task_failure("invalid_input", str(err))
    return TaskResult(
        status="succeeded",
        value={
            "batch_id": result.report.batch_id,
            "final_status": result.report.final_status,
            "schema_version": result.report.schema_version,
            "issue_risk": result.report.issues.issue_risk if result.report.issues else None,
        },
    )


__all__ = ["generate_report_operation", "inspect_operation"]
