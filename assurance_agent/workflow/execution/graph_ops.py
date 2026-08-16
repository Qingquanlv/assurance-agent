"""Execution-domain graph operations (``operation:run-tests``)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from assurance_agent.config import load_config
from assurance_agent.workflow.execution.runner import run_change
from assurance_agent.workflow.graph.handlers.operation import link_host_task_paths
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.pr_metrics import collect_pr_metrics_batch_operation


@contextmanager
def _host_uv_environment(context: RuntimeContext) -> Iterator[None]:
    """Force ``uv run`` to reuse the host project venv (not a task-local rebuild)."""
    host_venv = context.project_root.resolve() / ".venv"
    host_python = host_venv / "bin" / "python"
    keys = ("UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "VIRTUAL_ENV")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        if host_venv.is_dir():
            os.environ["UV_PROJECT_ENVIRONMENT"] = str(host_venv)
            os.environ["VIRTUAL_ENV"] = str(host_venv)
        if host_python.is_file():
            os.environ["UV_PYTHON"] = str(host_python)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_tests(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
    """在 task 私有 workspace 中执行测试并发布 execution evidence（不 spawn ``aa run``）。

    ``run_change`` 内部经 ``uv run pytest`` 在 workspace project root 下跑测试；
    测试失败不是 task 失败——final_status 进 value，由 gate/route 裁决。
    """
    del task  # contract signature
    link_host_task_paths(workspace, context)
    with _host_uv_environment(context):
        config = load_config(workspace.project_root)
        manifest = run_change(workspace.project_root, workspace.change_dir, config)
    final_status = getattr(manifest.final_status, "value", manifest.final_status)
    return TaskResult(
        status="succeeded",
        value={"batch_id": manifest.batch_id, "final_status": final_status},
    )


def run_tests_and_collect_pr_metrics(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    executed = run_tests(task, workspace, context)
    if executed.status != "succeeded":
        return executed
    collected = collect_pr_metrics_batch_operation(task, workspace, context)
    if collected.status != "succeeded":
        return collected
    executed_value = executed.value if isinstance(executed.value, dict) else {}
    return TaskResult(
        status="succeeded",
        value={**executed_value, "metrics_batch": collected.value},
        candidate_outputs=executed.candidate_outputs,
    )


__all__ = ["run_tests", "run_tests_and_collect_pr_metrics"]
