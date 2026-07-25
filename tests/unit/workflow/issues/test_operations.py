"""Tests for assurance_agent.workflow.issues.operations.

Coverage:
- collect_observations_operation writes all 4 required files
- Returns batch_id, evidence_bundle_digest, abnormal_count in value
- Idempotent replay: second call produces identical files and same counts
- Hard invalid_input failure when execution manifest is missing
- Hard invalid_input failure when execution manifest is corrupt
- Clean batch: writes empty observations + clean snapshot
- abnormal_count matches number of abnormal observations
- operation:collect-observations is registered in default_operations()
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.issues.operations import collect_observations_operation


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------


def _make_context(change_dir: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=change_dir.parent,
        repo_root=change_dir.parent,
        change_dir=change_dir,
        change_id=change_dir.name,
    )


def _make_task() -> ExecutableTask:
    from assurance_agent.workflow.graph.contracts import ResourceClaims
    from assurance_agent.workflow.graph.schema_v2 import RetryPolicyDef, TimeoutPolicyDef

    return ExecutableTask(
        task_id="task-001",
        invocation_id="inv-001",
        checkpoint_ns="ns-001",
        graph_id="g-001",
        node_id="collect-observations",
        structural_path="collect-observations",
        input={},
        input_sha256="0" * 64,
        contract_digest="0" * 64,
        retryable_errors=("timeout", "transport"),
        retry_policy=RetryPolicyDef(max_attempts=3),
        timeout_policy=TimeoutPolicyDef(run_seconds=300, heartbeat_seconds=60),
        target="operation:collect-observations",
        resources=ResourceClaims(),
    )


class _FakeTaskWorkspace:
    """Minimal workspace stub: change_dir is the given path."""

    def __init__(self, change_dir: Path) -> None:
        self.change_dir = change_dir
        self.project_root = change_dir.parent


# ---------------------------------------------------------------------------
# Execution fixture helpers
# ---------------------------------------------------------------------------


def _setup_execution(
    change_dir: Path,
    batch_id: str,
    *,
    cases: list[dict] | None = None,
    target: str = "api",
) -> None:
    execution_dir = change_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    batch_dir = execution_dir / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    targets_map = {"api": target == "api", "e2e": target == "e2e", "fuzz": False, "performance": False}
    result_file_key = target
    manifest = {
        "schema_version": "1.0",
        "change_id": change_dir.name,
        "batch_id": batch_id,
        "selected_targets": targets_map,
        "result_files": {result_file_key: f"runs/{batch_id}/{target}-result.json"},
    }
    (execution_dir / "execution-manifest.yaml").write_text(
        yaml.safe_dump(manifest), encoding="utf-8"
    )

    result_data = {
        "schema_version": "1.0",
        "change_id": change_dir.name,
        "batch_id": batch_id,
        "target": target,
        "status": "failed" if any(c.get("status") == "failed" for c in (cases or [])) else "passed",
        "command": f"pytest tests/{target}",
        "source": {"framework": "pytest", "raw_log": ""},
        "total": len(cases or []),
        "passed": sum(1 for c in (cases or []) if c.get("status") == "passed"),
        "failed": sum(1 for c in (cases or []) if c.get("status") == "failed"),
        "skipped": sum(1 for c in (cases or []) if c.get("status") == "skipped"),
        "cases": cases or [],
        "unmapped_tests": [],
    }
    (batch_dir / f"{target}-result.json").write_text(json.dumps(result_data), encoding="utf-8")


def _make_case(case_id: str, status: str, message: str = "") -> dict:
    return {
        "case_id": case_id,
        "status": status,
        "file": f"tests/api/test_{case_id}.py",
        "test_name": f"test_{case_id}",
        "duration_ms": 100,
        "message": message,
        "raw_log_ref": "",
        "trace": "",
        "screenshot": "",
        "video": "",
    }


# ---------------------------------------------------------------------------
# Test: registered in default_operations
# ---------------------------------------------------------------------------


def test_collect_observations_is_registered() -> None:
    ops = default_operations()
    assert "operation:collect-observations" in ops


# ---------------------------------------------------------------------------
# Test: writes all 4 required files
# ---------------------------------------------------------------------------


def test_writes_four_required_files(tmp_path: Path) -> None:
    change_id = "CH-ops-001"
    change_dir = tmp_path / change_id
    batch_id = "20260725-110000"
    _setup_execution(change_dir, batch_id, cases=[
        _make_case("API-001", "failed", "HTTP 500"),
    ])

    task = _make_task()
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    assert (change_dir / "inspect" / "observations.json").is_file()
    assert (change_dir / "inspect" / "issue-evidence-manifest.json").is_file()
    assert (change_dir / "issues" / "events.jsonl").is_file()
    assert (change_dir / "issues" / "snapshot.json").is_file()


# ---------------------------------------------------------------------------
# Test: returned value contains required fields
# ---------------------------------------------------------------------------


def test_returns_batch_id_digest_and_abnormal_count(tmp_path: Path) -> None:
    change_id = "CH-ops-002"
    change_dir = tmp_path / change_id
    batch_id = "20260725-110001"
    _setup_execution(change_dir, batch_id, cases=[
        _make_case("API-001", "failed"),
        _make_case("API-002", "passed"),
    ])

    task = _make_task()
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    assert value["batch_id"] == batch_id
    assert str(value["evidence_bundle_digest"]).startswith("sha256:")
    assert value["abnormal_count"] == 1  # one failed case


# ---------------------------------------------------------------------------
# Test: idempotent replay
# ---------------------------------------------------------------------------


def test_idempotent_replay_produces_same_output(tmp_path: Path) -> None:
    change_id = "CH-idempotent"
    change_dir = tmp_path / change_id
    batch_id = "20260725-110002"
    _setup_execution(change_dir, batch_id, cases=[
        _make_case("API-001", "failed"),
    ])

    task = _make_task()
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    # First call
    result1 = collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]
    obs_json_1 = (change_dir / "inspect" / "observations.json").read_bytes()
    manifest_json_1 = (change_dir / "inspect" / "issue-evidence-manifest.json").read_bytes()
    snapshot_1 = (change_dir / "issues" / "snapshot.json").read_bytes()

    # Second call (replay)
    result2 = collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]
    obs_json_2 = (change_dir / "inspect" / "observations.json").read_bytes()
    manifest_json_2 = (change_dir / "inspect" / "issue-evidence-manifest.json").read_bytes()
    snapshot_2 = (change_dir / "issues" / "snapshot.json").read_bytes()

    assert result1.status == "succeeded"
    assert result2.status == "succeeded"
    assert result1.value == result2.value
    assert obs_json_1 == obs_json_2
    assert manifest_json_1 == manifest_json_2
    # Snapshot may differ slightly in timestamp but observations count should match
    snap1 = json.loads(snapshot_1)
    snap2 = json.loads(snapshot_2)
    assert len(snap1.get("observations", [])) == len(snap2.get("observations", []))


# ---------------------------------------------------------------------------
# Test: hard failure on missing execution manifest
# ---------------------------------------------------------------------------


def test_hard_failure_on_missing_execution_manifest(tmp_path: Path) -> None:
    change_id = "CH-no-manifest"
    change_dir = tmp_path / change_id
    change_dir.mkdir()

    task = _make_task()
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert "execution-manifest.yaml" in (result.error or "")


# ---------------------------------------------------------------------------
# Test: hard failure on corrupt execution manifest
# ---------------------------------------------------------------------------


def test_hard_failure_on_corrupt_execution_manifest(tmp_path: Path) -> None:
    change_id = "CH-corrupt"
    change_dir = tmp_path / change_id
    execution_dir = change_dir / "execution"
    execution_dir.mkdir(parents=True)
    (execution_dir / "execution-manifest.yaml").write_text(
        "this: is: invalid: yaml: [[[", encoding="utf-8"
    )

    task = _make_task()
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"


# ---------------------------------------------------------------------------
# Test: clean batch writes empty observations and clean snapshot
# ---------------------------------------------------------------------------


def test_clean_batch_writes_empty_observations_and_snapshot(tmp_path: Path) -> None:
    change_id = "CH-clean-ops"
    change_dir = tmp_path / change_id
    batch_id = "20260725-110003"
    _setup_execution(change_dir, batch_id, cases=[
        _make_case("API-001", "passed"),
    ])

    task = _make_task()
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    assert value["abnormal_count"] == 0

    # observations.json should have empty list
    obs_data = json.loads((change_dir / "inspect" / "observations.json").read_text(encoding="utf-8"))
    assert obs_data.get("observations") == []

    # snapshot.json should exist and have empty observations
    snapshot = json.loads((change_dir / "issues" / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot.get("observations") == []
    assert snapshot.get("change_id") == change_id

    # events.jsonl should NOT be created for clean batch (no events to append)
    assert not (change_dir / "issues" / "events.jsonl").is_file()


# ---------------------------------------------------------------------------
# Test: abnormal_count matches real observation count
# ---------------------------------------------------------------------------


def test_abnormal_count_matches_observation_count(tmp_path: Path) -> None:
    change_id = "CH-count"
    change_dir = tmp_path / change_id
    batch_id = "20260725-110004"
    _setup_execution(change_dir, batch_id, cases=[
        _make_case("API-001", "failed"),
        _make_case("API-002", "failed"),
        _make_case("API-003", "passed"),
        _make_case("API-004", "skipped", "anomaly case"),
    ])

    task = _make_task()
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    # 2 failures + 1 skip (anomaly) = 3
    assert value["abnormal_count"] == 3

    # observations.json should match
    obs_data = json.loads((change_dir / "inspect" / "observations.json").read_text(encoding="utf-8"))
    assert len(obs_data["observations"]) == 3


# ---------------------------------------------------------------------------
# Test: events.jsonl contains observation_recorded events
# ---------------------------------------------------------------------------


def test_events_jsonl_contains_observation_recorded(tmp_path: Path) -> None:
    change_id = "CH-events"
    change_dir = tmp_path / change_id
    batch_id = "20260725-110005"
    _setup_execution(change_dir, batch_id, cases=[
        _make_case("API-001", "failed"),
    ])

    task = _make_task()
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]

    events_file = change_dir / "issues" / "events.jsonl"
    assert events_file.is_file()
    lines = [l for l in events_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "observation_recorded"
    assert event["change_id"] == change_id
    assert event["batch_id"] == batch_id
    assert event["seq"] == 1


# ---------------------------------------------------------------------------
# Test: idempotent replay does not duplicate events in events.jsonl
# ---------------------------------------------------------------------------


def test_idempotent_replay_does_not_duplicate_events(tmp_path: Path) -> None:
    change_id = "CH-no-dup"
    change_dir = tmp_path / change_id
    batch_id = "20260725-110006"
    _setup_execution(change_dir, batch_id, cases=[
        _make_case("API-001", "failed"),
    ])

    task = _make_task()
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]
    collect_observations_operation(task, workspace, context)  # type: ignore[arg-type]

    events_file = change_dir / "issues" / "events.jsonl"
    lines = [l for l in events_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    # Should still be exactly 1 event (idempotency_key deduplication)
    assert len(lines) == 1
