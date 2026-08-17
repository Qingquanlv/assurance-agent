"""Fail-fast composite: run-tests then collect-pr-metrics-batch."""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.workflow.execution import graph_ops
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace


def _workspace(project: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="task-1",
        root=project,
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / "CH-1",
        base_tree_id="tree-0",
    )


def _context(project: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={},
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="task-1",
        node_id="execution",
        graph_id="assurance",
        target="operation:run-tests-and-collect-pr-metrics",
        input={"with": {}},
    )


def test_composite_skips_collect_when_run_tests_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    collected: list[str] = []

    def fake_run_tests(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="failed", error="boom", error_kind="timeout")

    def fake_collect(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        del task, workspace, context
        collected.append("called")
        return TaskResult(status="succeeded", value={"batch_id": "b-1"})

    monkeypatch.setattr(graph_ops, "run_tests", fake_run_tests)
    monkeypatch.setattr(graph_ops, "collect_pr_metrics_batch_operation", fake_collect)

    result = graph_ops.run_tests_and_collect_pr_metrics(_task(), _workspace(project), _context(project))

    assert result.status == "failed"
    assert result.error == "boom"
    assert collected == []


def test_composite_merges_values_when_both_succeed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()

    def fake_run_tests(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        del task, workspace, context
        return TaskResult(
            status="succeeded",
            value={"batch_id": "b-1", "final_status": "PASS"},
            candidate_outputs={"change:execution/execution-manifest.json": "ok"},
        )

    def fake_collect(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded", value={"collectors": []})

    monkeypatch.setattr(graph_ops, "run_tests", fake_run_tests)
    monkeypatch.setattr(graph_ops, "collect_pr_metrics_batch_operation", fake_collect)

    result = graph_ops.run_tests_and_collect_pr_metrics(_task(), _workspace(project), _context(project))

    assert result.status == "succeeded"
    assert result.value == {
        "batch_id": "b-1",
        "final_status": "PASS",
        "metrics_batch": {"collectors": []},
    }
    assert result.candidate_outputs == {"change:execution/execution-manifest.json": "ok"}
