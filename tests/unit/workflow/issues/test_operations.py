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
- record_empty_issue_analysis_operation: writes completed zero-candidate artifacts
- record_issue_analysis_failure_operation: writes failed status + appends ledger event
- record_project_sync_pending_operation: appends project_sync_pending event
- Error-kind → analysis-reason mapping for failure operation
- Recovery context vs input fallback for failure operation
- Idempotency of all three recovery operations
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RecoveryContext, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.issues.operations import (
    collect_observations_operation,
    record_empty_issue_analysis_operation,
    record_issue_analysis_failure_operation,
    record_project_sync_pending_operation,
)


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


# ===========================================================================
# Helpers for recovery operation tests
# ===========================================================================


def _write_evidence_manifest(change_dir: Path, batch_id: str, evidence_digest: str) -> None:
    """Write a minimal inspect/issue-evidence-manifest.json for recovery op tests."""
    inspect_dir = change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "change_id": change_dir.name,
        "batch_id": batch_id,
        "digest": evidence_digest,
        "entries": [
            {"path": "execution/runs/" + batch_id + "/api-result.json", "digest": evidence_digest},
        ],
    }
    (inspect_dir / "issue-evidence-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _write_candidate_doc(change_dir: Path, batch_id: str, evidence_digest: str) -> None:
    """Write a minimal inspect/issue-candidates.json for project-sync-pending tests."""
    inspect_dir = change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": "1.0",
        "change_id": change_dir.name,
        "batch_id": batch_id,
        "evidence_bundle_digest": evidence_digest,
        "candidates": [],
    }
    (inspect_dir / "issue-candidates.json").write_text(
        json.dumps(doc, indent=2), encoding="utf-8"
    )


def _make_recovery_task(
    target: str,
    error_kind: str = "transport",
    message: str = "analysis failed",
    *,
    with_recovery: bool = False,
) -> ExecutableTask:
    """Build an ExecutableTask for recovery operation tests."""
    from assurance_agent.workflow.graph.contracts import ResourceClaims
    from assurance_agent.workflow.graph.schema_v2 import RetryPolicyDef, TimeoutPolicyDef

    base = ExecutableTask(
        task_id="task-rec-001",
        invocation_id="inv-001",
        checkpoint_ns="ns-001",
        graph_id="g-001",
        node_id="record-failure",
        structural_path="record-failure",
        input={"error_kind": error_kind, "message": message},
        input_sha256="0" * 64,
        contract_digest="0" * 64,
        retryable_errors=(),
        retry_policy=RetryPolicyDef(max_attempts=1),
        timeout_policy=TimeoutPolicyDef(run_seconds=60, heartbeat_seconds=30),
        target=target,
        resources=ResourceClaims(),
    )
    if with_recovery:
        from assurance_agent.workflow.core.graph_events import TaskRecoveryRoutedEvent
        from assurance_agent.workflow.graph.models import RecoveryContext

        rec_event = TaskRecoveryRoutedEvent(
            type="task_recovery_routed",
            invocation_id="inv-001",
            checkpoint_ns="ns-001",
            graph_id="g-001",
            node_id="analyze-issues",
            generation_ordinal=0,
            task_id="task-ana-001",
            error_kind=error_kind,  # type: ignore[arg-type]
            message=message,
            via="record-analysis-failure",
            continue_to="inspect-complete",
        )
        recovery = RecoveryContext(
            source_task_id="task-ana-001",
            source_node_id="analyze-issues",
            generation_ordinal=0,
            error_kind=error_kind,  # type: ignore[arg-type]
            message=message,
            attempts_used=3,
            recovery_event=rec_event,
        )
        return base.model_copy(update={"recovery": recovery})
    return base


# ===========================================================================
# Tests: record_empty_issue_analysis_operation
# ===========================================================================


def test_record_empty_writes_completed_candidates_and_status(tmp_path: Path) -> None:
    change_id = "CH-empty-001"
    change_dir = tmp_path / change_id
    batch_id = "20260725-120000"
    evidence_digest = "sha256:" + "a" * 64

    _write_evidence_manifest(change_dir, batch_id, evidence_digest)

    task = _make_recovery_task("operation:record-empty-issue-analysis")
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = record_empty_issue_analysis_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    assert value["batch_id"] == batch_id
    assert value["candidate_count"] == 0
    assert str(value["candidate_digest"]).startswith("sha256:")

    candidates_path = change_dir / "inspect" / "issue-candidates.json"
    assert candidates_path.is_file()
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    assert candidates["candidates"] == []
    assert candidates["batch_id"] == batch_id

    status_path = change_dir / "inspect" / "issue-analysis-status.json"
    assert status_path.is_file()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "completed"
    assert status["candidate_count"] == 0
    assert status["evidence_bundle_digest"] == evidence_digest


def test_record_empty_fails_without_manifest(tmp_path: Path) -> None:
    change_id = "CH-empty-noman"
    change_dir = tmp_path / change_id
    change_dir.mkdir()

    task = _make_recovery_task("operation:record-empty-issue-analysis")
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = record_empty_issue_analysis_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"


def test_record_empty_is_idempotent(tmp_path: Path) -> None:
    change_id = "CH-empty-idm"
    change_dir = tmp_path / change_id
    batch_id = "20260725-120001"
    evidence_digest = "sha256:" + "b" * 64

    _write_evidence_manifest(change_dir, batch_id, evidence_digest)

    task = _make_recovery_task("operation:record-empty-issue-analysis")
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result1 = record_empty_issue_analysis_operation(task, workspace, context)  # type: ignore[arg-type]
    candidates1 = (change_dir / "inspect" / "issue-candidates.json").read_bytes()
    status1 = (change_dir / "inspect" / "issue-analysis-status.json").read_bytes()

    result2 = record_empty_issue_analysis_operation(task, workspace, context)  # type: ignore[arg-type]
    candidates2 = (change_dir / "inspect" / "issue-candidates.json").read_bytes()
    status2 = (change_dir / "inspect" / "issue-analysis-status.json").read_bytes()

    assert result1.status == "succeeded"
    assert result2.status == "succeeded"
    assert result1.value == result2.value
    assert candidates1 == candidates2
    assert status1 == status2


def test_record_empty_is_registered(tmp_path: Path) -> None:
    ops = default_operations()
    assert "operation:record-empty-issue-analysis" in ops


# ===========================================================================
# Tests: record_issue_analysis_failure_operation
# ===========================================================================


def test_record_failure_writes_failed_status_and_event(tmp_path: Path) -> None:
    change_id = "CH-fail-001"
    change_dir = tmp_path / change_id
    batch_id = "20260725-130000"
    evidence_digest = "sha256:" + "c" * 64

    _write_evidence_manifest(change_dir, batch_id, evidence_digest)

    task = _make_recovery_task(
        "operation:record-issue-analysis-failure", error_kind="timeout"
    )
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = record_issue_analysis_failure_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    assert value["analysis_reason"] == "timeout"

    # Candidates file must exist and be empty.
    candidates = json.loads(
        (change_dir / "inspect" / "issue-candidates.json").read_text(encoding="utf-8")
    )
    assert candidates["candidates"] == []

    # Status file must show failed.
    status = json.loads(
        (change_dir / "inspect" / "issue-analysis-status.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "failed"
    assert status["reason"] == "timeout"

    # Event must be appended to issues/events.jsonl.
    events_file = change_dir / "issues" / "events.jsonl"
    assert events_file.is_file()
    lines = [l for l in events_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "issue_analysis_failed"
    assert event["change_id"] == change_id


@pytest.mark.parametrize(
    ("error_kind", "expected_reason"),
    [
        ("transport", "unavailable"),
        ("timeout", "timeout"),
        ("rate_limit", "transport"),
        ("invalid_output", "invalid_output"),
    ],
)
def test_record_failure_error_kind_to_reason_mapping(
    tmp_path: Path, error_kind: str, expected_reason: str
) -> None:
    change_id = f"CH-fail-map-{error_kind}"
    change_dir = tmp_path / change_id
    batch_id = "20260725-130001"
    evidence_digest = "sha256:" + "d" * 64

    _write_evidence_manifest(change_dir, batch_id, evidence_digest)

    task = _make_recovery_task(
        "operation:record-issue-analysis-failure", error_kind=error_kind
    )
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = record_issue_analysis_failure_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    assert value["analysis_reason"] == expected_reason, (
        f"error_kind={error_kind!r} should map to reason={expected_reason!r}"
    )

    status = json.loads(
        (change_dir / "inspect" / "issue-analysis-status.json").read_text(encoding="utf-8")
    )
    assert status["reason"] == expected_reason


def test_record_failure_transport_maps_to_unavailable_retains_graph_error_kind(tmp_path: Path) -> None:
    """transport error_kind → analysis reason 'unavailable'; graph error_kind stays 'transport'."""
    change_id = "CH-fail-transport"
    change_dir = tmp_path / change_id
    batch_id = "20260725-130002"
    evidence_digest = "sha256:" + "e" * 64

    _write_evidence_manifest(change_dir, batch_id, evidence_digest)

    task = _make_recovery_task(
        "operation:record-issue-analysis-failure",
        error_kind="transport",
        message="provider unavailable",
    )
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = record_issue_analysis_failure_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    assert value["error_kind"] == "transport"        # graph error_kind preserved
    assert value["analysis_reason"] == "unavailable"  # analysis reason mapped


def test_record_failure_uses_recovery_context_when_present(tmp_path: Path) -> None:
    """Production path: error_kind/message read from task.recovery, not task.input."""
    change_id = "CH-fail-rec"
    change_dir = tmp_path / change_id
    batch_id = "20260725-130003"
    evidence_digest = "sha256:" + "f" * 64

    _write_evidence_manifest(change_dir, batch_id, evidence_digest)

    # task.input has "transport", task.recovery has "rate_limit".
    task = _make_recovery_task(
        "operation:record-issue-analysis-failure",
        error_kind="rate_limit",
        message="from recovery ctx",
        with_recovery=True,
    )
    # Override task.input to have a different error_kind to prove recovery takes precedence.
    task = task.model_copy(
        update={"input": {"error_kind": "transport", "message": "from input"}}
    )

    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = record_issue_analysis_failure_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    # rate_limit -> transport (analysis reason)
    assert value["analysis_reason"] == "transport"
    # error_kind from recovery context (rate_limit), not from input (transport).
    assert value["error_kind"] == "rate_limit"


def test_record_failure_is_idempotent(tmp_path: Path) -> None:
    change_id = "CH-fail-idm"
    change_dir = tmp_path / change_id
    batch_id = "20260725-130004"
    evidence_digest = "sha256:" + "1" * 64

    _write_evidence_manifest(change_dir, batch_id, evidence_digest)

    task = _make_recovery_task(
        "operation:record-issue-analysis-failure", error_kind="timeout"
    )
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    record_issue_analysis_failure_operation(task, workspace, context)  # type: ignore[arg-type]
    record_issue_analysis_failure_operation(task, workspace, context)  # type: ignore[arg-type]

    events_file = change_dir / "issues" / "events.jsonl"
    lines = [l for l in events_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, "Idempotent replay must not duplicate ledger events"


def test_record_failure_is_registered(tmp_path: Path) -> None:
    ops = default_operations()
    assert "operation:record-issue-analysis-failure" in ops


# ===========================================================================
# Tests: record_project_sync_pending_operation
# ===========================================================================


def test_record_sync_pending_appends_event(tmp_path: Path) -> None:
    change_id = "CH-sync-001"
    change_dir = tmp_path / change_id
    batch_id = "20260725-140000"
    evidence_digest = "sha256:" + "2" * 64

    _write_evidence_manifest(change_dir, batch_id, evidence_digest)
    _write_candidate_doc(change_dir, batch_id, evidence_digest)

    task = _make_recovery_task("operation:record-project-sync-pending")
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = record_project_sync_pending_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    assert value["batch_id"] == batch_id

    events_file = change_dir / "issues" / "events.jsonl"
    assert events_file.is_file()
    lines = [l for l in events_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "project_sync_pending"
    assert event["change_id"] == change_id
    assert "candidate_digest" in event
    assert event["candidate_digest"].startswith("sha256:")


def test_record_sync_pending_fails_without_manifest(tmp_path: Path) -> None:
    change_id = "CH-sync-noman"
    change_dir = tmp_path / change_id
    change_dir.mkdir()

    task = _make_recovery_task("operation:record-project-sync-pending")
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = record_project_sync_pending_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "failed"
    assert result.error_kind == "invalid_input"


def test_record_sync_pending_is_idempotent(tmp_path: Path) -> None:
    change_id = "CH-sync-idm"
    change_dir = tmp_path / change_id
    batch_id = "20260725-140001"
    evidence_digest = "sha256:" + "3" * 64

    _write_evidence_manifest(change_dir, batch_id, evidence_digest)
    _write_candidate_doc(change_dir, batch_id, evidence_digest)

    task = _make_recovery_task("operation:record-project-sync-pending")
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    record_project_sync_pending_operation(task, workspace, context)  # type: ignore[arg-type]
    record_project_sync_pending_operation(task, workspace, context)  # type: ignore[arg-type]

    events_file = change_dir / "issues" / "events.jsonl"
    lines = [l for l in events_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, "Idempotent replay must not duplicate ledger events"


def test_record_sync_pending_fallback_to_evidence_digest_when_no_candidates(tmp_path: Path) -> None:
    """When no candidates file exists, candidate_digest falls back to evidence_bundle_digest."""
    change_id = "CH-sync-nocand"
    change_dir = tmp_path / change_id
    batch_id = "20260725-140002"
    evidence_digest = "sha256:" + "4" * 64

    _write_evidence_manifest(change_dir, batch_id, evidence_digest)
    # No candidates file written.

    task = _make_recovery_task("operation:record-project-sync-pending")
    workspace = _FakeTaskWorkspace(change_dir)
    context = _make_context(change_dir)

    result = record_project_sync_pending_operation(task, workspace, context)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    value = result.value
    assert isinstance(value, dict)
    # Candidate digest should fall back to evidence digest.
    assert value["candidate_digest"] == evidence_digest


def test_record_sync_pending_is_registered(tmp_path: Path) -> None:
    ops = default_operations()
    assert "operation:record-project-sync-pending" in ops
