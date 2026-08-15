"""Fail-fast composite: materialize-trace-projection then build-coverage-gap-signals."""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics import coverage_gaps


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
        node_id="materialize-trace-projection",
        graph_id="inspect-with-issues",
        target="operation:materialize-trace-and-coverage-gaps",
        input={"with": {}},
    )


def test_composite_skips_gaps_when_materialize_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    built: list[str] = []

    def fake_materialize(
        task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="failed", error="cannot fold", error_kind="invalid_input")

    def fake_gaps(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        del task, workspace, context
        built.append("called")
        return TaskResult(status="succeeded", value={"written": True})

    monkeypatch.setattr(coverage_gaps, "materialize_trace_projection", fake_materialize)
    monkeypatch.setattr(coverage_gaps, "build_coverage_gap_signals_operation", fake_gaps)

    result = coverage_gaps.materialize_trace_and_coverage_gaps(
        _task(), _workspace(project), _context(project)
    )

    assert result.status == "failed"
    assert result.error == "cannot fold"
    assert built == []


def test_composite_merges_values_when_both_succeed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()

    def fake_materialize(
        task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
    ) -> TaskResult:
        del task, workspace, context
        return TaskResult(
            status="succeeded",
            value={"phase": "reconciled", "sufficient": True},
            candidate_outputs={"change:inspect/trace-projection.json": "ok"},
        )

    def fake_gaps(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded", value={"written": True, "gap_count": 2})

    monkeypatch.setattr(coverage_gaps, "materialize_trace_projection", fake_materialize)
    monkeypatch.setattr(coverage_gaps, "build_coverage_gap_signals_operation", fake_gaps)

    result = coverage_gaps.materialize_trace_and_coverage_gaps(
        _task(), _workspace(project), _context(project)
    )

    assert result.status == "succeeded"
    assert result.value == {
        "phase": "reconciled",
        "sufficient": True,
        "coverage_gaps": {"written": True, "gap_count": 2},
    }
    assert result.candidate_outputs == {"change:inspect/trace-projection.json": "ok"}
