"""operation:materialize-minimum-coverage handler registration + write path."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.trace import (
    TraceExecution,
    TraceProjection,
    TraceRow,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.minimum_coverage import (
    MINIMUM_COVERAGE_RESULT_REL,
    materialize_minimum_coverage_operation,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-MRC-OP-001"
BATCH_ID = "20260725-120000"
TS = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="materialize-minimum-coverage",
        graph_id="assurance",
        target="operation:materialize-minimum-coverage",
        input={},
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def _write_projection(change_dir: Path, case_id: str = "TC_DEPT_API_001") -> None:
    projection = TraceProjection(
        schema_version="1",
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id=BATCH_ID,
        sources=(),
        rows=(
            TraceRow(
                case_id=case_id,
                module="system.dept",
                case_type="API",
                automation_required=True,
                assertions=("ok",),
                covering_tests=(),
                coverage_state="covered",
                latest_execution=TraceExecution(
                    batch_id=BATCH_ID,
                    target="api",
                    status="passed",
                    ts=TS,
                    ts_source="executed_at",
                ),
                freshest_pass=TraceExecution(
                    batch_id=BATCH_ID,
                    target="api",
                    status="passed",
                    ts=TS,
                    ts_source="executed_at",
                ),
                presence_in_current_batch="executed",
                atemporal_kinds_present=(),
                open_problem_ids=(),
            ),
        ),
        unmapped_tests=(),
        gaps=(),
        integrity="complete",
    )
    atomic_write_bytes(change_dir / "inspect/trace-projection.json", canonical_json_bytes(projection))


def test_operation_is_registered_under_exact_target() -> None:
    ops = default_operations()
    assert "operation:materialize-minimum-coverage" in ops
    assert ops["operation:materialize-minimum-coverage"] is materialize_minimum_coverage_operation


def test_operation_writes_minimum_coverage_result(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "trace").mkdir(parents=True)
    (change_dir / "trace" / "minimum-coverage-matrix.json").write_text(
        json.dumps(
            [
                {
                    "mrc_id": "MRC-API-001",
                    "key": "create_dept",
                    "required": True,
                    "covered_by_cases": ["TC_DEPT_API_001"],
                    "status": "covered",
                    "skip_reason": None,
                    "category": "api",
                    "layer": "api",
                },
                {
                    "mrc_id": "MRC-NEG-001",
                    "key": "invented_by_llm",
                    "required": True,
                    "covered_by_cases": ["TC_DEPT_API_001"],
                    "status": "covered",
                    "category": "negative",
                    "layer": "api",
                },
            ]
        ),
        encoding="utf-8",
    )
    (change_dir / "explore").mkdir(parents=True)
    (change_dir / "explore" / "advisory.json").write_text(
        json.dumps(
            {
                "minimum_required_coverage": {
                    "api": ["create_dept"],
                    "negative": ["invented_by_llm"],
                }
            }
        ),
        encoding="utf-8",
    )
    (project_root / ".aa" / "data-knowledge.yaml").write_text(
        yaml.safe_dump({"schema_version": "1", "entities": {}, "auth": {}, "auth_matrix": {}}),
        encoding="utf-8",
    )
    _write_projection(change_dir)

    result: TaskResult = materialize_minimum_coverage_operation(
        _task(), _workspace(project_root), _context(project_root)
    )
    assert result.status == "succeeded"
    out = change_dir / MINIMUM_COVERAGE_RESULT_REL
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["change_id"] == CHANGE_ID
    assert payload["items"][0]["status"] == "covered"
    assert payload["items"][0]["executed_case_ids"] == ["TC_DEPT_API_001"]
    # §12.12: unknown closed key surfaces as a mechanical finding (join still written).
    assert any(
        f["code"] == "unknown_closed_key" and f["key"] == "invented_by_llm" for f in payload["findings"]
    )


def test_operation_fail_open_when_projection_missing(tmp_path: Path) -> None:
    """Missing whole projection matches missing-matrix: succeed, written=false."""
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "trace").mkdir(parents=True)
    (change_dir / "trace" / "minimum-coverage-matrix.json").write_text(
        json.dumps(
            [
                {
                    "mrc_id": "MRC-API-001",
                    "key": "create_dept",
                    "required": True,
                    "covered_by_cases": ["TC_A"],
                    "status": "covered",
                    "category": "api",
                    "layer": "api",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = materialize_minimum_coverage_operation(_task(), _workspace(project_root), _context(project_root))
    assert result.status == "succeeded"
    assert result.value == {
        "change_id": CHANGE_ID,
        "written": False,
        "reason": "no_trace_projection",
    }
    assert not (change_dir / MINIMUM_COVERAGE_RESULT_REL).exists()


def test_operation_fail_closed_when_category_unresolved(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "trace").mkdir(parents=True)
    (change_dir / "trace" / "minimum-coverage-matrix.json").write_text(
        json.dumps(
            [
                {
                    "mrc_id": "missing_required_fields",
                    "key": "missing_required_fields",
                    "required": True,
                    "covered_by_cases": ["TC_A"],
                    "status": "covered",
                }
            ]
        ),
        encoding="utf-8",
    )
    _write_projection(change_dir, case_id="TC_A")

    result = materialize_minimum_coverage_operation(_task(), _workspace(project_root), _context(project_root))
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert result.error is not None
    assert "mrc_category_unresolved" in result.error


def test_operation_fail_open_when_matrix_missing(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    _write_projection(change_dir)

    result = materialize_minimum_coverage_operation(_task(), _workspace(project_root), _context(project_root))
    assert result.status == "succeeded"
    assert result.value == {
        "change_id": CHANGE_ID,
        "written": False,
        "reason": "no_mrc_matrix",
    }
