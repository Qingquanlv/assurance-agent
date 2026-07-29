"""fold_trace execution-phase semantics (Task 3 Step 1)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.evidence.trace import (
    ExecutionFoldInput,
    canonical_json_bytes,
    fold_trace,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-1"
EXECUTED_AT = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


def _setup_project(tmp_path: Path) -> Path:
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    return change_dir


def _write_case(change_dir: Path, rel: str, body: str) -> None:
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _write_api_case(change_dir: Path, case_id: str = "TC_DEPT_API_001", *, required: bool = True) -> None:
    _write_case(
        change_dir,
        "cases/dept/case.yaml",
        f"""
schema_version: "1.0"
added:
  - case_id: {case_id}
    module: system.dept
    type: API
    title: create
    status: active
    priority: P0
    severity: blocker
    automation:
      required: {"true" if required else "false"}
modified: []
removed: []
""",
    )


def _write_fuzz_case(change_dir: Path, case_id: str = "TC_DEPT_FUZZ_001") -> None:
    _write_case(
        change_dir,
        "cases/fuzz/case.yaml",
        f"""
schema_version: "1.0"
added:
  - case_id: {case_id}
    module: system.dept
    type: Fuzz
    title: fuzz
    status: active
    priority: P1
    severity: major
    automation:
      required: true
modified: []
removed: []
""",
    )


def _write_perf_case(change_dir: Path, capability: str = "dept_list") -> None:
    _write_case(
        change_dir,
        "cases/perf/case.yaml",
        f"""
schema_version: "1.0"
added:
  - case_id: TC_DEPT_PERF_001
    module: system.dept
    type: Performance
    title: p95
    status: active
    priority: P1
    severity: major
    automation:
      required: true
      performance:
        scenario:
          capability: {capability}
modified: []
removed: []
""",
    )


def _write_manifest(
    change_dir: Path,
    *,
    batch_id: str,
    selected: SelectedTargets | None = None,
    executed_at: datetime | None = None,
    test_files_sha256: dict[str, str] | None = None,
) -> None:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": batch_id,
        "selected_targets": (
            selected or SelectedTargets(api=True, e2e=False, fuzz=False, performance=False)
        ).model_dump(),
        "result_files": {},
    }
    if executed_at is not None:
        payload["executed_at"] = executed_at.isoformat()
    if test_files_sha256 is not None:
        payload["test_files_sha256"] = test_files_sha256
    manifest_path = change_dir / "execution" / "execution-manifest.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_api_result(
    change_dir: Path,
    batch_id: str,
    *,
    change_id: str = CHANGE_ID,
    doc_batch_id: str | None = None,
    target: str = "api",
    file_target: str | None = None,
    cases: list[dict[str, object]] | None = None,
    unmapped: list[dict[str, str]] | None = None,
) -> None:
    on_disk_target = file_target if file_target is not None else target
    payload = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": doc_batch_id if doc_batch_id is not None else batch_id,
        "target": target,
        "status": "passed",
        "command": "cmd",
        "source": {"framework": "pytest", "raw_log": "raw.log"},
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "cases": cases
        or [
            {
                "case_id": "TC_DEPT_API_001",
                "status": "passed",
                "file": "tests/api/test_dept.py",
                "test_name": "test_tc_dept_api_001__ok",
                "duration_ms": 1,
                "message": "",
            }
        ],
        "unmapped_tests": unmapped or [],
    }
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / f"{on_disk_target}-result.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_fuzz_result(
    change_dir: Path,
    batch_id: str,
    *,
    status: str = "passed",
    case_id: str = "TC_DEPT_FUZZ_001",
) -> None:
    _write_api_result(
        change_dir,
        batch_id,
        target="fuzz",
        cases=[
            {
                "case_id": case_id,
                "status": status,
                "file": "tests/fuzz/test_dept.py",
                "test_name": "test_tc_dept_fuzz_001__x",
                "duration_ms": 1,
                "message": "",
            }
        ],
    )


def _write_perf_result(
    change_dir: Path,
    batch_id: str,
    *,
    change_id: str = CHANGE_ID,
    doc_batch_id: str | None = None,
    scenarios: list[dict[str, str | float]] | None = None,
) -> None:
    payload = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": doc_batch_id if doc_batch_id is not None else batch_id,
        "kind": "performance",
        "available": True,
        "status": "PASS",
        "scenarios": scenarios
        or [
            {
                "capability": "dept_list",
                "endpoint": "/dept",
                "measured_p95_ms": 100.0,
                "threshold_p95_ms": 200.0,
                "measured_error_rate": 0.0,
                "threshold_error_rate_max": 0.01,
                "verdict": "PASS",
            }
        ],
        "command": "",
        "source": {},
    }
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "performance-result.json").write_text(json.dumps(payload), encoding="utf-8")


def _fold_view_digest(
    *,
    batch_id: str,
    executed_at: datetime,
    selected: SelectedTargets,
    test_files_sha256: dict[str, str] | None = None,
) -> str:
    payload = {
        "batch_id": batch_id,
        "change_id": CHANGE_ID,
        "executed_at": executed_at,
        "selected_targets": selected,
        "test_files_sha256": dict(sorted((test_files_sha256 or {}).items())),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def test_reconciled_phase_is_accepted(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert projection.phase == "reconciled"


def test_manifest_missing_when_current_is_none(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    projection = fold_trace(tmp_path, CHANGE_ID)
    assert any(gap.code == "manifest_missing" for gap in projection.gaps)
    assert projection.integrity == "incomplete"
    fold_view = next(src for src in projection.sources if src.path.endswith("#fold-view"))
    assert fold_view.exists is False
    assert fold_view.sha256 is None


def test_injected_current_does_not_require_manifest(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    batch_id = "20260729-120000"
    selected = SelectedTargets(api=True, e2e=False, fuzz=False, performance=False)
    current = ExecutionFoldInput(
        batch_id=batch_id,
        executed_at=EXECUTED_AT,
        selected_targets=selected,
        test_files_sha256={"tests/api/test_dept.py": "abc"},
    )
    projection = fold_trace(tmp_path, CHANGE_ID, current=current)
    assert not any(gap.code == "manifest_missing" for gap in projection.gaps)
    fold_view = next(src for src in projection.sources if src.path.endswith("#fold-view"))
    assert fold_view.exists is True
    assert fold_view.sha256 == _fold_view_digest(
        batch_id=batch_id,
        executed_at=EXECUTED_AT,
        selected=selected,
        test_files_sha256={"tests/api/test_dept.py": "abc"},
    )


def test_manifest_and_injected_current_produce_same_fold_view_digest(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    batch_id = "20260729-120000"
    selected = SelectedTargets(api=True, e2e=False, fuzz=False, performance=False)
    test_files = {"tests/api/test_dept.py": "abc"}
    _write_manifest(
        change_dir,
        batch_id=batch_id,
        selected=selected,
        executed_at=EXECUTED_AT,
        test_files_sha256=test_files,
    )
    from_disk = fold_trace(tmp_path, CHANGE_ID)
    injected = fold_trace(
        tmp_path,
        CHANGE_ID,
        current=ExecutionFoldInput(
            batch_id=batch_id,
            executed_at=EXECUTED_AT,
            selected_targets=selected,
            test_files_sha256=test_files,
        ),
    )
    disk_digest = next(src for src in from_disk.sources if src.path.endswith("#fold-view")).sha256
    injected_digest = next(src for src in injected.sources if src.path.endswith("#fold-view")).sha256
    assert (
        disk_digest
        == injected_digest
        == _fold_view_digest(
            batch_id=batch_id,
            executed_at=EXECUTED_AT,
            selected=selected,
            test_files_sha256=test_files,
        )
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("change_id", "OTHER"),
        ("doc_batch_id", "20260729-999999"),
        ("target", "e2e"),
    ],
)
def test_result_identity_mismatch_gaps(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    batch_id = "20260729-120000"
    _write_manifest(change_dir, batch_id=batch_id, executed_at=EXECUTED_AT)
    kwargs: dict[str, object] = {}
    if field == "change_id":
        kwargs["change_id"] = value
    elif field == "doc_batch_id":
        kwargs["doc_batch_id"] = value
    elif field == "target":
        kwargs["target"] = value
        kwargs["file_target"] = "api"
    _write_api_result(change_dir, batch_id, **kwargs)  # type: ignore[arg-type]
    projection = fold_trace(tmp_path, CHANGE_ID)
    mismatches = [gap for gap in projection.gaps if gap.code == "result_identity_mismatch"]
    assert len(mismatches) == 1
    row = projection.rows[0]
    assert row.latest_execution is None


def test_latest_and_freshest_pass_across_batches(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    older = "20260728-120000"
    newer_fail = "20260729-120000"
    newer_pass = "20260729-130000"
    _write_api_result(
        change_dir,
        older,
        cases=[
            {
                "case_id": "TC_DEPT_API_001",
                "status": "passed",
                "file": "f.py",
                "test_name": "t",
                "duration_ms": 1,
                "message": "",
            }
        ],
    )
    _write_api_result(
        change_dir,
        newer_fail,
        cases=[
            {
                "case_id": "TC_DEPT_API_001",
                "status": "failed",
                "file": "f.py",
                "test_name": "t",
                "duration_ms": 1,
                "message": "",
            }
        ],
    )
    _write_api_result(
        change_dir,
        newer_pass,
        cases=[
            {
                "case_id": "TC_DEPT_API_001",
                "status": "passed",
                "file": "f.py",
                "test_name": "t",
                "duration_ms": 1,
                "message": "",
            }
        ],
    )
    _write_manifest(change_dir, batch_id=newer_pass, executed_at=EXECUTED_AT)
    projection = fold_trace(tmp_path, CHANGE_ID)
    row = projection.rows[0]
    assert row.latest_execution is not None
    assert row.latest_execution.batch_id == newer_pass
    assert row.latest_execution.status == "passed"
    assert row.freshest_pass is not None
    assert row.freshest_pass.batch_id == newer_pass


def test_skipped_counts_as_latest_but_not_pass(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    pass_batch = "20260728-120000"
    skip_batch = "20260729-120000"
    _write_api_result(
        change_dir,
        pass_batch,
        cases=[
            {
                "case_id": "TC_DEPT_API_001",
                "status": "passed",
                "file": "f.py",
                "test_name": "t",
                "duration_ms": 1,
                "message": "",
            }
        ],
    )
    _write_api_result(
        change_dir,
        skip_batch,
        cases=[
            {
                "case_id": "TC_DEPT_API_001",
                "status": "skipped",
                "file": "f.py",
                "test_name": "t",
                "duration_ms": 1,
                "message": "",
            }
        ],
    )
    _write_manifest(change_dir, batch_id=skip_batch, executed_at=EXECUTED_AT)
    projection = fold_trace(tmp_path, CHANGE_ID)
    row = projection.rows[0]
    assert row.latest_execution is not None
    assert row.latest_execution.batch_id == skip_batch
    assert row.latest_execution.status == "skipped"
    assert row.freshest_pass is not None
    assert row.freshest_pass.batch_id == pass_batch


@pytest.mark.parametrize(
    ("selected", "in_current", "expected"),
    [
        (SelectedTargets(api=False, e2e=False, fuzz=False, performance=False), False, "target_not_selected"),
        (SelectedTargets(api=True, e2e=False, fuzz=False, performance=False), True, "executed"),
        (SelectedTargets(api=True, e2e=False, fuzz=False, performance=False), False, "not_in_current_batch"),
    ],
)
def test_presence_in_current_batch_states(
    tmp_path: Path,
    selected: SelectedTargets,
    in_current: bool,
    expected: str,
) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    current_batch = "20260729-120000"
    if in_current:
        _write_api_result(change_dir, current_batch)
    else:
        _write_api_result(change_dir, "20260728-120000")
    _write_manifest(change_dir, batch_id=current_batch, selected=selected, executed_at=EXECUTED_AT)
    projection = fold_trace(tmp_path, CHANGE_ID)
    assert projection.rows[0].presence_in_current_batch == expected


def test_executed_at_preferred_over_legacy_batch_id(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    batch_id = "20260729-120000"
    legacy_ts = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
    explicit = datetime(2026, 7, 29, 15, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    _write_api_result(change_dir, batch_id)
    _write_manifest(change_dir, batch_id=batch_id, executed_at=explicit)
    projection = fold_trace(tmp_path, CHANGE_ID)
    row = projection.rows[0]
    assert row.latest_execution is not None
    assert row.latest_execution.ts == explicit
    assert row.latest_execution.ts_source == "executed_at"
    assert row.latest_execution.ts != legacy_ts


def test_legacy_batch_id_parses_as_utc(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    batch_id = "20260729-153045"
    _write_api_result(change_dir, batch_id)
    _write_manifest(change_dir, batch_id=batch_id)
    projection = fold_trace(tmp_path, CHANGE_ID)
    row = projection.rows[0]
    assert row.latest_execution is not None
    assert row.latest_execution.ts == datetime(2026, 7, 29, 15, 30, 45, tzinfo=UTC)
    assert row.latest_execution.ts_source == "batch_id_legacy_utc"


def test_naive_executed_at_in_injection_raises(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    with pytest.raises(TypeError, match="timezone-aware"):
        fold_trace(
            tmp_path,
            CHANGE_ID,
            current=ExecutionFoldInput(
                batch_id="20260729-120000",
                executed_at=datetime(2026, 7, 29, 12, 0, 0),
                selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
                test_files_sha256={},
            ),
        )


def test_fold_is_deterministic(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    batch_id = "20260729-120000"
    _write_api_result(change_dir, batch_id)
    _write_manifest(change_dir, batch_id=batch_id, executed_at=EXECUTED_AT)
    first = fold_trace(tmp_path, CHANGE_ID).model_dump_json()
    second = fold_trace(tmp_path, CHANGE_ID).model_dump_json()
    assert first == second


def test_sources_include_case_manifest_results_and_fold_view(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    batch_id = "20260729-120000"
    _write_api_result(change_dir, batch_id)
    _write_manifest(change_dir, batch_id=batch_id, executed_at=EXECUTED_AT)
    projection = fold_trace(tmp_path, CHANGE_ID)
    paths = {src.path for src in projection.sources}
    assert "cases/dept/case.yaml" in paths
    assert "execution/execution-manifest.yaml" in paths
    assert "execution/runs/20260729-120000/api-result.json" in paths
    assert "execution/execution-manifest.yaml#fold-view" in paths


@pytest.mark.parametrize(
    ("verdict", "expected_status", "perf_run"),
    [
        ("PASS", "passed", True),
        ("FAIL", "failed", True),
        ("SKIPPED", "skipped", False),
    ],
)
def test_performance_capability_join(
    tmp_path: Path,
    verdict: str,
    expected_status: str,
    perf_run: bool,
) -> None:
    change_dir = _setup_project(tmp_path)
    _write_perf_case(change_dir)
    batch_id = "20260729-120000"
    _write_perf_result(
        change_dir,
        batch_id,
        scenarios=[
            {
                "capability": "dept_list",
                "endpoint": "/dept",
                "measured_p95_ms": 100.0,
                "threshold_p95_ms": 200.0,
                "measured_error_rate": 0.0,
                "threshold_error_rate_max": 0.01,
                "verdict": verdict,
            }
        ],
    )
    _write_manifest(
        change_dir,
        batch_id=batch_id,
        selected=SelectedTargets(api=False, e2e=False, fuzz=False, performance=True),
        executed_at=EXECUTED_AT,
    )
    projection = fold_trace(tmp_path, CHANGE_ID)
    row = projection.rows[0]
    assert row.latest_execution is not None
    assert row.latest_execution.status == expected_status
    if perf_run:
        assert row.atemporal_kinds_present == ("perf_run",)
    else:
        assert row.atemporal_kinds_present == ()


def test_performance_identity_mismatch(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_perf_case(change_dir)
    batch_id = "20260729-120000"
    _write_perf_result(change_dir, batch_id, change_id="OTHER")
    _write_manifest(
        change_dir,
        batch_id=batch_id,
        selected=SelectedTargets(api=False, e2e=False, fuzz=False, performance=True),
        executed_at=EXECUTED_AT,
    )
    projection = fold_trace(tmp_path, CHANGE_ID)
    assert any(gap.code == "result_identity_mismatch" for gap in projection.gaps)
    assert projection.rows[0].latest_execution is None


def test_fuzz_run_only_for_non_skipped_latest(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_fuzz_case(change_dir)
    _write_fuzz_result(change_dir, "20260728-120000", status="passed")
    _write_fuzz_result(change_dir, "20260729-120000", status="skipped")
    _write_manifest(
        change_dir,
        batch_id="20260729-120000",
        selected=SelectedTargets(api=False, e2e=False, fuzz=True, performance=False),
        executed_at=EXECUTED_AT,
    )
    projection = fold_trace(tmp_path, CHANGE_ID)
    row = projection.rows[0]
    assert row.latest_execution is not None
    assert row.latest_execution.status == "skipped"
    assert row.atemporal_kinds_present == ()


def test_fuzz_run_present_for_failed_latest(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_fuzz_case(change_dir)
    _write_fuzz_result(change_dir, "20260729-120000", status="failed")
    _write_manifest(
        change_dir,
        batch_id="20260729-120000",
        selected=SelectedTargets(api=False, e2e=False, fuzz=True, performance=False),
        executed_at=EXECUTED_AT,
    )
    projection = fold_trace(tmp_path, CHANGE_ID)
    assert projection.rows[0].atemporal_kinds_present == ("fuzz_run",)


def test_coverage_state_without_tree_scan(tmp_path: Path) -> None:
    change_dir = _setup_project(tmp_path)
    _write_case(
        change_dir,
        "cases/required/case.yaml",
        """
schema_version: "1.0"
added:
  - case_id: TC_REQ_API_001
    module: x
    type: API
    title: t
    status: active
    priority: P1
    severity: major
    automation:
      required: true
modified: []
removed: []
""",
    )
    _write_case(
        change_dir,
        "cases/optional/case.yaml",
        """
schema_version: "1.0"
added:
  - case_id: TC_OPT_API_001
    module: x
    type: API
    title: t
    status: active
    priority: P1
    severity: major
modified: []
removed: []
""",
    )
    batch_id = "20260729-120000"
    _write_manifest(change_dir, batch_id=batch_id, executed_at=EXECUTED_AT)
    projection = fold_trace(tmp_path, CHANGE_ID)
    by_id = {row.case_id: row for row in projection.rows}
    assert by_id["TC_REQ_API_001"].coverage_state == "uncovered"
    assert by_id["TC_OPT_API_001"].coverage_state == "not_required"
    assert by_id["TC_REQ_API_001"].covering_tests == ()
