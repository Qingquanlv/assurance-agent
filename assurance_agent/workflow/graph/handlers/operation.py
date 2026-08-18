"""Kernel operation handler: dispatch + host-path helpers.

Domain callables live in capability packages and are assembled by
``workflow.driver.operations_catalog``. This module only owns:

- ``OperationHandler`` dispatch;
- ``operation:no-op`` / ``operation:stop``;
- host runtime/coordinator path reattachment used by domain ops and agent.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping

from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace

OperationResult = TaskResult
OperationFn = Callable[[ExecutableTask, TaskWorkspace, RuntimeContext], OperationResult]

# Captured trees omit these (size / absolute interpreter links / agent config).
# Reattach them in the task root so test operations reuse the host runtime and
# OpenCode can resolve the bounded agent named by an AgentRequest.
_HOST_RUNTIME_DIRS = (".venv", "node_modules", ".opencode")
# Coordinator files excluded from content trees but required by in-workspace CLI
# (e.g. ``aa heal record-apply`` reads the strict allocation ledger).
_HOST_CHANGE_COORDINATOR_FILES = ("events.jsonl",)


def _link_host_runtime_dirs(workspace: TaskWorkspace, context: RuntimeContext) -> None:
    """Symlink omitted host runtime/config dirs into the task-private project root."""
    host_root = context.resolved_host_root.resolve()
    task_root = workspace.project_root.resolve()
    if host_root == task_root:
        return
    for name in _HOST_RUNTIME_DIRS:
        host = host_root / name
        task = task_root / name
        if not host.exists():
            continue
        if task.is_symlink() and task.resolve() == host.resolve():
            continue
        if task.exists() or task.is_symlink():
            if task.is_dir() and not task.is_symlink():
                shutil.rmtree(task)
            else:
                task.unlink(missing_ok=True)
        try:
            task.symlink_to(host, target_is_directory=True)
        except OSError:
            continue


def _link_host_change_coordinator_files(workspace: TaskWorkspace, context: RuntimeContext) -> None:
    """Symlink canonical change-dir coordinator files into the task-private change root."""
    host_change = context.change_dir.resolve()
    task_change = workspace.change_dir.resolve()
    if host_change == task_change:
        return
    for name in _HOST_CHANGE_COORDINATOR_FILES:
        host = host_change / name
        task = task_change / name
        if not host.is_file():
            continue
        if task.is_symlink() and task.resolve() == host.resolve():
            continue
        if task.exists() or task.is_symlink():
            task.unlink(missing_ok=True)
        try:
            task.symlink_to(host)
        except OSError:
            continue


def link_host_runtime_dirs(workspace: TaskWorkspace, context: RuntimeContext) -> None:
    """Reattach only the omitted host runtime/config dirs.

    Callers whose write-set is captured from the task tree must use this rather
    than :func:`link_host_task_paths`: a coordinator-file symlink points outside
    the task root and capture refuses such an escape.
    """
    _link_host_runtime_dirs(workspace, context)


def link_host_task_paths(workspace: TaskWorkspace, context: RuntimeContext) -> None:
    """Reattach host runtime dirs and coordinator files the tree capture omits."""
    _link_host_runtime_dirs(workspace, context)
    _link_host_change_coordinator_files(workspace, context)


def no_op(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> OperationResult:
    del task, workspace, context
    return TaskResult(status="succeeded")


def stop_operation(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> OperationResult:
    del workspace, context
    reason = task_with(task).get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return task_failure("invalid_input", "operation:stop requires a non-empty with.reason")
    return TaskResult(status="stopped", value={"reason": reason})


class OperationHandler:
    """按精确 target 分发到注册的 operation callable。"""

    def __init__(self, operations: Mapping[str, OperationFn]) -> None:
        self._operations = dict(operations)

    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        fn = self._operations.get(task.target)
        if fn is None:
            return task_failure("contract", f"unknown operation target: {task.target}")
        return fn(task, workspace, context)


__all__ = [
    "OperationFn",
    "OperationHandler",
    "OperationResult",
    "link_host_runtime_dirs",
    "link_host_task_paths",
    "no_op",
    "stop_operation",
]
