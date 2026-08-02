"""operation:materialize-trace-projection — reconciled trace projection authority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.evidence.trace import fold_trace
from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.handlers.trace_projection import materialize_trace_projection
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-1"
BATCH_ID = "20260729-120000"


@pytest.fixture()
def workspace(tmp_path: Path) -> TaskWorkspace:
    project_root = tmp_path / "proj"
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    write_aa_config(project_root)
    return TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree",
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="materialize-trace-projection",
        graph_id="inspect-with-issues",
        target="operation:materialize-trace-projection",
        input={},
    )


def _context() -> RuntimeContext:
    return RuntimeContext.model_construct(change_id=CHANGE_ID, params={})


def _write_minimal_case(change_dir: Path) -> None:
    case_path = change_dir / "cases" / "dept" / "case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "added": [
                    {
                        "case_id": "TC_DEPT_API_001",
                        "module": "system.dept",
                        "type": "API",
                        "title": "create",
                        "status": "active",
                        "priority": "P0",
                        "severity": "blocker",
                        "automation": {"required": True},
                    }
                ],
                "modified": [],
                "removed": [],
            }
        ),
        encoding="utf-8",
    )


def _write_manifest(change_dir: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "executed_at": "2026-07-29T12:00:00+00:00",
        "selected_targets": SelectedTargets(api=True, e2e=False, fuzz=False, performance=False).model_dump(),
        "result_files": {},
    }
    manifest_path = change_dir / "execution" / "execution-manifest.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_materialize_writes_reconciled_projection(workspace: TaskWorkspace) -> None:
    _write_minimal_case(workspace.change_dir)
    _write_manifest(workspace.change_dir)
    result = materialize_trace_projection(_task(), workspace, _context())
    assert isinstance(result, TaskResult) and result.status == "succeeded"
    out = workspace.change_dir / "inspect" / "trace-projection.json"
    assert out.is_file()
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["phase"] == "reconciled"
    assert doc["schema_version"] == "2"
    expected = fold_trace(workspace.project_root, CHANGE_ID, phase="reconciled")
    assert doc == json.loads(expected.model_dump_json())


def test_missing_sources_still_writes_projection_with_gaps(workspace: TaskWorkspace) -> None:
    result = materialize_trace_projection(_task(), workspace, _context())
    assert result.status == "succeeded"
    out = workspace.change_dir / "inspect" / "trace-projection.json"
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["phase"] == "reconciled"
    assert doc["schema_version"] == "2"
    assert doc["gaps"]
    gap_codes = {gap["code"] for gap in doc["gaps"]}
    assert "manifest_missing" in gap_codes


@pytest.mark.parametrize(
    "state",
    ["analysis_failed", "reconcile_failed", "project_sync_pending"],
)
def test_materialize_writes_v2_for_recovery_states(workspace: TaskWorkspace, state: str) -> None:
    from tests.unit.evidence import test_issue_replay_authority as auth
    from tests.unit.evidence.test_fold_trace_reconciled import _write_selected_api_result

    _write_minimal_case(workspace.change_dir)
    auth._write_json(workspace.change_dir / auth.FAILURE_SOURCE, auth._failure_payload())
    auth.write_recovery_state(workspace.change_dir, state)
    _write_selected_api_result(workspace.change_dir, auth.BATCH_ID)
    # Preseed a B0 projection that must be replaced.
    preseed = workspace.change_dir / "inspect" / "trace-projection.json"
    preseed.parent.mkdir(parents=True, exist_ok=True)
    preseed.write_text(
        json.dumps(
            {
                "schema_version": "2",
                "change_id": CHANGE_ID,
                "phase": "reconciled",
                "authoritative_batch_id": "B0",
                "sources": [],
                "rows": [],
                "unmapped_tests": [],
                "gaps": [],
                "integrity": "complete",
            }
        ),
        encoding="utf-8",
    )
    result = materialize_trace_projection(_task(), workspace, _context())
    assert result.status == "succeeded"
    doc = json.loads(preseed.read_text(encoding="utf-8"))
    assert doc["schema_version"] == "2"
    assert doc["authoritative_batch_id"] == auth.BATCH_ID
    assert doc["integrity"] == "incomplete"
    assert any(
        gap["code"]
        == {
            "analysis_failed": "issue_analysis_failed",
            "reconcile_failed": "issue_reconcile_failed",
            "project_sync_pending": "project_sync_pending",
        }[state]
        for gap in doc["gaps"]
    )


def test_materialize_lets_summary_errors_escape(
    workspace: TaskWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.evidence import trace as trace_mod
    from assurance_agent.evidence.layer_summary import TraceLayerSummaryError

    _write_minimal_case(workspace.change_dir)
    _write_manifest(workspace.change_dir)

    def boom(*_args, **_kwargs):
        raise TraceLayerSummaryError("materializer must not catch")

    monkeypatch.setattr(trace_mod, "summarize_projection_by_layer", boom)
    with pytest.raises(TraceLayerSummaryError):
        materialize_trace_projection(_task(), workspace, _context())
    out = workspace.change_dir / "inspect" / "trace-projection.json"
    assert not out.exists()


def test_operation_registered_in_default_operations() -> None:
    assert "operation:materialize-trace-projection" in default_operations()
