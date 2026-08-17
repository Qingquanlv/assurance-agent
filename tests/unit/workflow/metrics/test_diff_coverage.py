"""collect-diff-coverage — materialize diff line coverage (Task 6 / §5-A1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.workflow.metrics.batch_io import is_forbidden_coverage_sidecar
from assurance_agent.workflow.metrics.diff_coverage import collect_diff_coverage


def test_collect_diff_coverage_joins_changed_lines_with_coverage_json() -> None:
    coverage_json = {
        "files": {
            "app/models/admin.py": {"executed_lines": [10, 11, 12, 20]},
        }
    }
    evidence = collect_diff_coverage(
        change_id="CH-DIFF-001",
        batch_id="20260805-100000",
        coverage_json=coverage_json,
        changed_lines={"app/models/admin.py": [10, 11, 99]},
    )
    assert evidence.total_changed_lines == 3
    assert evidence.covered_changed_lines == 2
    assert evidence.value == pytest.approx(2 / 3)
    assert evidence.files[0].uncovered_lines == (99,)
    assert evidence.collection_gaps == ()


def test_missing_coverage_json_is_typed_collection_gap() -> None:
    evidence = collect_diff_coverage(
        change_id="CH-DIFF-001",
        batch_id="b1",
        coverage_json=None,
        changed_lines={"a.py": [1]},
    )
    assert evidence.value is None
    assert len(evidence.collection_gaps) == 1
    assert evidence.collection_gaps[0].code == "collection_failed"
    assert evidence.collection_gaps[0].metric == "diff_coverage"


def test_missing_changed_lines_json_is_typed_collection_gap(tmp_path: Path) -> None:
    """Operation must not silently publish empty changed-lines as a clean value."""
    from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
    from assurance_agent.workflow.graph.workspace import TaskWorkspace
    from assurance_agent.workflow.metrics.diff_coverage import collect_diff_coverage_operation
    from tests.helpers_aa import write_aa_config

    change_id = "CH-DIFF-001"
    batch_id = "20260805-100000"
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / change_id
    change_dir.mkdir(parents=True)
    raw = change_dir / "execution" / "runs" / batch_id / "raw"
    raw.mkdir(parents=True)
    (raw / "coverage.json").write_text(
        '{"files": {"app/x.py": {"executed_lines": [1]}}}',
        encoding="utf-8",
    )
    # deliberately omit raw/changed-lines.json
    (change_dir / "execution" / "execution-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": change_id,
                "batch_id": batch_id,
                "executed_at": "2026-08-05T10:00:00+00:00",
                "selected_targets": {
                    "api": True,
                    "e2e": False,
                    "fuzz": False,
                    "performance": False,
                },
                "result_files": {},
                "final_status": "PASS",
            }
        ),
        encoding="utf-8",
    )

    result = collect_diff_coverage_operation(
        ExecutableTask.model_construct(
            task_id="t1",
            node_id="collect-diff-coverage",
            graph_id="assurance",
            target="operation:collect-diff-coverage",
            input={"with": {"batch_id": batch_id}},
        ),
        TaskWorkspace(
            task_id="t1",
            root=project_root,
            project_root=project_root,
            repo_root=project_root,
            change_dir=change_dir,
            base_tree_id="tree-0",
        ),
        RuntimeContext.model_construct(
            project_root=project_root,
            repo_root=project_root,
            change_dir=change_dir,
            change_id=change_id,
            params={},
        ),
    )
    assert result.status == "succeeded"
    payload = json.loads(
        (change_dir / "execution" / "runs" / batch_id / "coverage-diff.json").read_text(encoding="utf-8")
    )
    assert payload["value"] is None
    assert payload["collection_gaps"]
    assert payload["collection_gaps"][0]["code"] == "collection_failed"
    assert "changed-lines" in payload["collection_gaps"][0]["detail"]


def test_corrupt_coverage_json_is_artifact_corrupt_gap() -> None:
    evidence = collect_diff_coverage(
        change_id="CH-DIFF-001",
        batch_id="b1",
        coverage_json={"totals": {}},  # no files mapping
        changed_lines={"a.py": [1]},
    )
    assert evidence.collection_gaps[0].code == "artifact_corrupt"


def test_freeze_excluded_coverage_sidecar_outside_raw_is_forbidden(tmp_path: Path) -> None:
    raw = tmp_path / "execution" / "runs" / "b1" / "raw"
    raw.mkdir(parents=True)
    sidecar = tmp_path / ".coverage"
    sidecar.write_bytes(b"\x00")
    assert is_forbidden_coverage_sidecar(sidecar, allowed_raw_dir=raw) is True
    allowed = raw / ".coverage.hostname.1"
    allowed.write_bytes(b"\x00")
    assert is_forbidden_coverage_sidecar(allowed, allowed_raw_dir=raw) is False
