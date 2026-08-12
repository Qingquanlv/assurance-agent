"""PR cadence collectors — exact operation keys + batch evidence writes."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.auth_matrix import compute_auth_matrix_operation
from assurance_agent.workflow.metrics.constraint_coverage import (
    compute_constraint_coverage_operation,
)
from assurance_agent.workflow.metrics.diff_coverage import collect_diff_coverage_operation
from assurance_agent.workflow.metrics.journey_coverage import (
    compute_journey_coverage_operation,
)
from assurance_agent.workflow.metrics.threshold_slack import compute_threshold_slack_operation
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-PR-METRICS-001"
BATCH_ID = "20260805-100000"

EXPECTED_OPS = {
    "operation:collect-diff-coverage": collect_diff_coverage_operation,
    "operation:compute-constraint-coverage": compute_constraint_coverage_operation,
    "operation:compute-auth-matrix": compute_auth_matrix_operation,
    "operation:compute-journey-coverage": compute_journey_coverage_operation,
    "operation:compute-threshold-slack": compute_threshold_slack_operation,
}


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _task(target: str) -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id=target.removeprefix("operation:"),
        graph_id="assurance",
        target=target,
        input={"with": {"batch_id": BATCH_ID}},
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def _seed_batch(change_dir: Path) -> Path:
    batch = change_dir / "execution" / "runs" / BATCH_ID
    raw = batch / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (change_dir / "execution").mkdir(parents=True, exist_ok=True)
    (change_dir / "execution" / "execution-manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": BATCH_ID,
                "executed_at": "2026-08-05T10:00:00+00:00",
                "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": True},
                "result_files": {"performance": "performance-result.json"},
                "final_status": "PASS",
            }
        ),
        encoding="utf-8",
    )
    return batch


def test_pr_collector_operations_registered_under_exact_keys() -> None:
    ops = default_operations()
    for key, fn in EXPECTED_OPS.items():
        assert key in ops
        assert ops[key] is fn


def test_collect_diff_coverage_writes_batch_evidence_and_refuses_sidecar(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    batch = _seed_batch(change_dir)
    (batch / "raw" / "coverage.json").write_text(
        json.dumps({"files": {"app/x.py": {"executed_lines": [1, 2]}}}),
        encoding="utf-8",
    )
    (batch / "raw" / "changed-lines.json").write_text(
        json.dumps({"app/x.py": [1, 2, 3]}),
        encoding="utf-8",
    )
    (project_root / ".coverage").write_bytes(b"\x00binary")

    result = collect_diff_coverage_operation(
        _task("operation:collect-diff-coverage"),
        _workspace(project_root),
        _context(project_root),
    )
    assert result.status == "succeeded"
    out = batch / "coverage-diff.json"
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["covered_changed_lines"] == 2
    assert payload["total_changed_lines"] == 3

    # Explicit forbidden sidecar path → typed gap, still writes evidence.
    refused = collect_diff_coverage_operation(
        ExecutableTask.model_construct(
            task_id="t1",
            node_id="collect-diff-coverage",
            graph_id="assurance",
            target="operation:collect-diff-coverage",
            input={"with": {"batch_id": BATCH_ID, "coverage_data_path": ".coverage"}},
        ),
        _workspace(project_root),
        _context(project_root),
    )
    assert refused.status == "succeeded"
    refused_payload = json.loads(out.read_text(encoding="utf-8"))
    assert refused_payload["collection_gaps"][0]["code"] == "collection_failed"


def test_threshold_slack_operation_writes_perf_slack(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    batch = _seed_batch(change_dir)
    (batch / "performance-result.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": BATCH_ID,
                "kind": "performance",
                "available": True,
                "status": "PASS",
                "scenarios": [
                    {
                        "capability": "api_list",
                        "endpoint": "/api/v1/x",
                        "measured_p95_ms": 8.0,
                        "threshold_p95_ms": 2000.0,
                        "measured_error_rate": 0.0,
                        "threshold_error_rate_max": 0.01,
                        "verdict": "PASS",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = compute_threshold_slack_operation(
        _task("operation:compute-threshold-slack"),
        _workspace(project_root),
        _context(project_root),
    )
    assert result.status == "succeeded"
    payload = json.loads((batch / "perf-slack.json").read_text(encoding="utf-8"))
    assert payload["value"] == 250.0
    assert payload["shortboards"][0]["code"] == "threshold_slack_out_of_band"


_JOURNEY_KEY = "admin_creates_dept_and_sees_in_tree"
_STRONG_E2E = """
async def test_admin_creates_dept():
    await page.goto("/dept")
    await page.get_by_role("button", name="Create").click()
    await expect(page.get_by_text("Dept A")).to_have_count(1)
"""


def _seed_journey_inputs(project_root: Path, change_dir: Path, batch: Path) -> dict:
    (change_dir / "trace").mkdir(parents=True, exist_ok=True)
    (change_dir / "trace" / "minimum-coverage-matrix.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "mrc_id": "MRC-E2E-001",
                    "key": _JOURNEY_KEY,
                    "required": True,
                    "covered_by_cases": ["TC_E2E_001"],
                    "status": "covered",
                    "category": "e2e_if_enabled",
                    "layer": "e2e",
                }
            ]
        ),
        encoding="utf-8",
    )
    (project_root / "tests" / "e2e").mkdir(parents=True, exist_ok=True)
    (project_root / "tests" / "e2e" / "test_dept.py").write_text(_STRONG_E2E, encoding="utf-8")
    execution = {
        "batch_id": BATCH_ID,
        "target": "e2e",
        "status": "passed",
        "ts": "2026-08-05T10:00:00+00:00",
        "ts_source": "executed_at",
    }
    return {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "phase": "execution",
        "authoritative_batch_id": BATCH_ID,
        "sources": [],
        "rows": [
            {
                "case_id": "TC_E2E_001",
                "module": "system.dept",
                "case_type": "E2E",
                "automation_required": True,
                "assertions": ["ok"],
                "covering_tests": [
                    {"file": "tests/e2e/test_dept.py", "test_name": "test_admin_creates_dept"}
                ],
                "coverage_state": "covered",
                "latest_execution": execution,
                "freshest_pass": execution,
                "presence_in_current_batch": "executed",
                "atemporal_kinds_present": [],
                "open_problem_ids": [],
            }
        ],
        "unmapped_tests": [],
        "gaps": [],
        "integrity": "complete",
    }


def test_journey_coverage_reads_the_batch_projection_before_inspect_publishes_one(
    tmp_path: Path,
) -> None:
    """A4 must be collectable on a change's first batch.

    ``inspect/trace-projection.json`` is published by
    ``materialize-trace-projection``, which the graph runs *after* this collector,
    so requiring it made journey_coverage permanently ``collection_failed`` on
    every first batch. ``aa run`` writes the batch's own projection before the
    gate, and that is what the collector must use.
    """
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    batch = _seed_batch(change_dir)
    projection = _seed_journey_inputs(project_root, change_dir, batch)
    (batch / "trace-projection.json").write_text(json.dumps(projection), encoding="utf-8")
    assert not (change_dir / "inspect" / "trace-projection.json").exists()

    result = compute_journey_coverage_operation(
        _task("operation:compute-journey-coverage"),
        _workspace(project_root),
        _context(project_root),
    )

    assert result.status == "succeeded"
    payload = json.loads((batch / "journey-coverage.json").read_text(encoding="utf-8"))
    assert payload["collection_gaps"] == []
    assert payload["value"] == 1.0


def test_journey_coverage_accepts_v2_trace_projection(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    batch = _seed_batch(change_dir)
    projection = _seed_journey_inputs(project_root, change_dir, batch)
    projection["schema_version"] = "2"
    (batch / "trace-projection.json").write_text(json.dumps(projection), encoding="utf-8")

    result = compute_journey_coverage_operation(
        _task("operation:compute-journey-coverage"),
        _workspace(project_root),
        _context(project_root),
    )

    assert result.status == "succeeded"
    payload = json.loads((batch / "journey-coverage.json").read_text(encoding="utf-8"))
    assert payload["collection_gaps"] == []
    assert payload["value"] == 1.0


def test_journey_coverage_falls_back_to_the_reconciled_projection(tmp_path: Path) -> None:
    """A healing rerun holds a reconciled projection and no batch copy yet."""
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    batch = _seed_batch(change_dir)
    projection = _seed_journey_inputs(project_root, change_dir, batch)
    (change_dir / "inspect").mkdir(parents=True, exist_ok=True)
    (change_dir / "inspect" / "trace-projection.json").write_text(
        json.dumps({**projection, "phase": "reconciled"}), encoding="utf-8"
    )

    result = compute_journey_coverage_operation(
        _task("operation:compute-journey-coverage"),
        _workspace(project_root),
        _context(project_root),
    )

    assert result.status == "succeeded"
    payload = json.loads((batch / "journey-coverage.json").read_text(encoding="utf-8"))
    assert payload["collection_gaps"] == []
    assert payload["value"] == 1.0
