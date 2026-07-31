"""Tests for assurance_agent.workflow.issues.collector.

Coverage:
- test_failure observations from failed API/E2E/Fuzz cases
- anomaly observations from skipped cases (no workaround marker)
- workaround observations from skipped cases with workaround markers
- runner-level anomaly (target failed with no case failures)
- coverage_gap observations from coverage-result.json
- performance_signal observations from performance-result.json
- trace/screenshot/video refs included in evidence_refs
- review_finding observations from review/*.json
- workaround observations from healing apply summaries
- clean batch (no abnormal signals) → empty observations
- evidence manifest always has at least one entry (execution manifest)
- replay produces identical OBS- IDs
- missing execution manifest → EvidenceError (hard failure)
- corrupt execution manifest → EvidenceError (hard failure)
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from assurance_agent.evidence.digests import (
    evidence_bundle_digest_v1,
    evidence_entry_digest_v1,
    raw_sha256,
    read_evidence_entry_v1,
)
from assurance_agent.workflow.execution.evidence import EvidenceError
from assurance_agent.workflow.issues.collector import collect_observations

# Pinned from pre-extraction collector on fixed UTF-8 fixture bytes (not via new helpers).
_CLEAN_MANIFEST_YAML = (
    "batch_id: '20260725-100000'\n"
    "change_id: CH-GOLDEN\n"
    "result_files:\n"
    "  api: runs/20260725-100000/api-result.json\n"
    "schema_version: '1.0'\n"
    "selected_targets:\n"
    "  api: true\n"
    "  e2e: false\n"
    "  fuzz: false\n"
    "  performance: false\n"
)
_CLEAN_API_RESULT_JSON = (
    '{"schema_version":"1.0","change_id":"CH-GOLDEN","batch_id":"20260725-100000",'
    '"target":"api","status":"passed","command":"pytest tests/api",'
    '"source":{"framework":"pytest","raw_log":""},"total":0,"passed":0,"failed":0,'
    '"skipped":0,"cases":[],"unmapped_tests":[]}'
)
_CLEAN_BUNDLE_DIGEST_GOLDEN = "sha256:9da76743d2f43bbd1e7c4f06fe9efad23cc884ad2ff77b56b47a869dd3c59957"
_CLEAN_ANCHOR_DIGEST_GOLDEN = "sha256:41ff3f27309c753c0ff67537a3b528d1ea788ec9319d9deae4c581ec683c461f"


# ---------------------------------------------------------------------------
# Test fixture builders
# ---------------------------------------------------------------------------


def _write_manifest(change_dir: Path, *, batch_id: str, targets: dict | None = None) -> None:
    """Write a minimal execution-manifest.yaml."""
    if targets is None:
        targets = {"api": True, "e2e": False, "fuzz": False, "performance": False}
    execution_dir = change_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    batch_dir = execution_dir / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": "1.0",
        "change_id": change_dir.name,
        "batch_id": batch_id,
        "selected_targets": targets,
        "result_files": {},
    }

    result_files: dict[str, str] = {}
    if targets.get("api"):
        result_files["api"] = f"runs/{batch_id}/api-result.json"
    if targets.get("e2e"):
        result_files["e2e"] = f"runs/{batch_id}/e2e-result.json"
    if targets.get("fuzz"):
        result_files["fuzz"] = f"runs/{batch_id}/fuzz-result.json"
    if targets.get("coverage"):
        result_files["coverage"] = f"runs/{batch_id}/coverage-result.json"
    if targets.get("performance"):
        result_files["performance"] = f"runs/{batch_id}/performance-result.json"
    manifest["result_files"] = result_files

    (execution_dir / "execution-manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


def _write_api_result(
    change_dir: Path,
    batch_id: str,
    *,
    cases: list[dict] | None = None,
    unmapped_tests: list[dict] | None = None,
    status: str = "passed",
) -> None:
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": "1.0",
        "change_id": change_dir.name,
        "batch_id": batch_id,
        "target": "api",
        "status": status,
        "command": "pytest tests/api",
        "source": {"framework": "pytest", "raw_log": ""},
        "total": len(cases or []),
        "passed": 0,
        "failed": sum(1 for c in (cases or []) if c.get("status") == "failed"),
        "skipped": 0,
        "cases": cases or [],
        "unmapped_tests": unmapped_tests or [],
    }
    (batch_dir / "api-result.json").write_text(json.dumps(result), encoding="utf-8")


def _write_e2e_result(
    change_dir: Path,
    batch_id: str,
    *,
    cases: list[dict] | None = None,
    status: str = "passed",
) -> None:
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": "1.0",
        "change_id": change_dir.name,
        "batch_id": batch_id,
        "target": "e2e",
        "status": status,
        "command": "pytest tests/e2e",
        "source": {"framework": "playwright", "raw_log": ""},
        "total": len(cases or []),
        "passed": 0,
        "failed": sum(1 for c in (cases or []) if c.get("status") == "failed"),
        "skipped": 0,
        "cases": cases or [],
        "unmapped_tests": [],
    }
    (batch_dir / "e2e-result.json").write_text(json.dumps(result), encoding="utf-8")


def _make_case(
    case_id: str,
    status: str,
    message: str = "",
    raw_log_ref: str = "",
    trace: str = "",
    screenshot: str = "",
    video: str = "",
) -> dict:
    return {
        "case_id": case_id,
        "status": status,
        "file": f"tests/api/test_{case_id}.py",
        "test_name": f"test_{case_id}",
        "duration_ms": 100,
        "message": message,
        "raw_log_ref": raw_log_ref,
        "trace": trace,
        "screenshot": screenshot,
        "video": video,
    }


# ---------------------------------------------------------------------------
# Test: missing / corrupt execution manifest
# ---------------------------------------------------------------------------


def test_missing_execution_manifest_raises(tmp_path: Path) -> None:
    change_dir = tmp_path / "CH-001"
    change_dir.mkdir()
    with pytest.raises(EvidenceError, match="execution-manifest.yaml not found"):
        collect_observations(change_dir, "CH-001")


def test_corrupt_execution_manifest_raises(tmp_path: Path) -> None:
    change_dir = tmp_path / "CH-001"
    execution_dir = change_dir / "execution"
    execution_dir.mkdir(parents=True)
    (execution_dir / "execution-manifest.yaml").write_text("not: valid: yaml: [[[", encoding="utf-8")
    with pytest.raises(EvidenceError):
        collect_observations(change_dir, "CH-001")


def test_selected_target_missing_result_is_a_hard_evidence_failure(
    tmp_path: Path,
) -> None:
    change_id = "CH-missing-result"
    change_dir = tmp_path / change_id
    batch_id = "20260725-095959"
    _write_manifest(change_dir, batch_id=batch_id)

    with pytest.raises(EvidenceError, match="result file missing"):
        collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")


def test_selected_target_missing_manifest_result_path_is_a_hard_evidence_failure(
    tmp_path: Path,
) -> None:
    change_id = "CH-missing-result-path"
    change_dir = tmp_path / change_id
    batch_id = "20260725-095959"
    _write_manifest(change_dir, batch_id=batch_id)
    manifest_path = change_dir / "execution" / "execution-manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["result_files"].pop("api")
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(EvidenceError, match="result path missing"):
        collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")


def test_parseable_but_malformed_target_result_is_a_hard_evidence_failure(tmp_path: Path) -> None:
    change_id = "CH-malformed-result"
    change_dir = tmp_path / change_id
    batch_id = "20260725-095959"
    _write_manifest(change_dir, batch_id=batch_id)
    result_path = change_dir / "execution" / "runs" / batch_id / "api-result.json"
    result_path.write_text("{}", encoding="utf-8")

    with pytest.raises(EvidenceError, match="invalid selected api result"):
        collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")


@pytest.mark.parametrize("payload", [None, "not-json", "{}"])
def test_selected_performance_missing_corrupt_or_malformed_is_a_hard_evidence_failure(
    tmp_path: Path,
    payload: str | None,
) -> None:
    change_id = "CH-bad-performance"
    change_dir = tmp_path / change_id
    batch_id = "20260725-095959"
    targets = {"api": False, "e2e": False, "fuzz": False, "performance": True}
    _write_manifest(change_dir, batch_id=batch_id, targets=targets)
    perf_path = change_dir / "execution" / "runs" / batch_id / "performance-result.json"
    if payload is not None:
        perf_path.write_text(payload, encoding="utf-8")

    with pytest.raises(EvidenceError, match="performance result"):
        collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")


def test_performance_result_with_malformed_scenario_is_a_hard_evidence_failure(
    tmp_path: Path,
) -> None:
    change_id = "CH-malformed-performance-scenario"
    change_dir = tmp_path / change_id
    batch_id = "20260725-095959"
    targets = {"api": False, "e2e": False, "fuzz": False, "performance": True}
    _write_manifest(change_dir, batch_id=batch_id, targets=targets)
    perf_path = change_dir / "execution" / "runs" / batch_id / "performance-result.json"
    perf_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": change_id,
                "batch_id": batch_id,
                "kind": "performance",
                "available": True,
                "status": "PASS",
                "scenarios": [{}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceError, match="invalid selected performance result"):
        collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")


@pytest.mark.parametrize("payload", [None, "not-json", "{}"])
def test_declared_coverage_missing_corrupt_or_malformed_is_a_hard_evidence_failure(
    tmp_path: Path,
    payload: str | None,
) -> None:
    change_id = "CH-bad-coverage"
    change_dir = tmp_path / change_id
    batch_id = "20260725-095959"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(change_dir, batch_id)
    manifest_path = change_dir / "execution" / "execution-manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["result_files"]["coverage"] = f"runs/{batch_id}/coverage-result.json"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    coverage_path = change_dir / "execution" / "runs" / batch_id / "coverage-result.json"
    if payload is not None:
        coverage_path.write_text(payload, encoding="utf-8")

    with pytest.raises(EvidenceError, match="coverage result"):
        collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")


# ---------------------------------------------------------------------------
# Test: clean batch (no abnormal signals)
# ---------------------------------------------------------------------------


def test_clean_batch_returns_empty_observations(tmp_path: Path) -> None:
    change_id = "CH-clean"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100000"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("API-001", "passed"),
            _make_case("API-002", "passed"),
        ],
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert result.batch_id == batch_id
    assert result.observations == ()
    assert result.evidence_bundle_digest.startswith("sha256:")
    # Manifest always has at least the execution manifest entry
    assert len(result.manifest.entries) >= 1
    entry_paths = {e.path for e in result.manifest.entries}
    assert "execution/execution-manifest.yaml" in entry_paths


# ---------------------------------------------------------------------------
# Test: test_failure observations from failed API cases
# ---------------------------------------------------------------------------


def test_failed_api_case_produces_test_failure_observation(tmp_path: Path) -> None:
    change_id = "CH-api-fail"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100001"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("API-NEG-001", "failed", message="HTTP 500 on empty name"),
            _make_case("API-POS-001", "passed"),
        ],
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.kind == "test_failure"
    assert obs.target == "api"
    assert obs.case_id == "API-NEG-001"
    assert obs.observation_id.startswith("OBS-")
    assert obs.change_id == change_id
    assert obs.batch_id == batch_id
    assert any("api-result.json" in ref for ref in obs.evidence_refs)


def test_failed_e2e_case_produces_test_failure_observation(tmp_path: Path) -> None:
    change_id = "CH-e2e-fail"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100002"
    _write_manifest(
        change_dir,
        batch_id=batch_id,
        targets={
            "api": False,
            "e2e": True,
            "fuzz": False,
            "performance": False,
        },
    )
    _write_e2e_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("E2E-LOGIN-001", "failed", message="Locator not found"),
        ],
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.kind == "test_failure"
    assert obs.target == "e2e"


# ---------------------------------------------------------------------------
# Test: skipped cases produce anomaly or workaround
# ---------------------------------------------------------------------------


def test_skipped_case_without_marker_produces_anomaly(tmp_path: Path) -> None:
    change_id = "CH-skip-anomaly"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100003"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("API-001", "skipped", message="Test not implemented"),
        ],
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.kind == "anomaly"
    assert obs.target == "api"


def test_skipped_case_with_workaround_marker_produces_workaround(tmp_path: Path) -> None:
    change_id = "CH-skip-workaround"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100004"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("API-001", "skipped", message="workaround: known issue with dept mgmt"),
        ],
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.kind == "workaround"


def test_xfail_skipped_case_produces_workaround(tmp_path: Path) -> None:
    change_id = "CH-xfail"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100005"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("API-001", "skipped", message="xfail: flaky assertion"),
        ],
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert result.observations[0].kind == "workaround"


# ---------------------------------------------------------------------------
# Test: runner-level anomaly
# ---------------------------------------------------------------------------


def test_runner_anomaly_when_target_failed_no_case_failures(tmp_path: Path) -> None:
    change_id = "CH-runner-anomaly"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100006"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("API-001", "passed"),
        ],
        status="failed",
    )  # Target-level status says failed but no case failures

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    kinds = [obs.kind for obs in result.observations]
    assert "anomaly" in kinds
    # The anomaly should come from the runner-level signal
    anomaly_obs = next(o for o in result.observations if o.kind == "anomaly")
    assert anomaly_obs.source.json_pointer == "/status"


# ---------------------------------------------------------------------------
# Test: traces/screenshots/videos included in evidence_refs
# ---------------------------------------------------------------------------


def test_traces_screenshots_videos_in_evidence_refs(tmp_path: Path) -> None:
    change_id = "CH-media-refs"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100007"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case(
                "API-001",
                "failed",
                trace="execution/runs/20260725-100007/traces/api.zip",
                screenshot="execution/runs/20260725-100007/screenshots/api.png",
                video="execution/runs/20260725-100007/videos/api.mp4",
            ),
        ],
    )
    for rel, content in (
        ("execution/runs/20260725-100007/traces/api.zip", b"trace"),
        ("execution/runs/20260725-100007/screenshots/api.png", b"screenshot"),
        ("execution/runs/20260725-100007/videos/api.mp4", b"video"),
    ):
        artifact = change_dir / rel
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(content)

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert len(result.observations) == 1
    obs = result.observations[0]
    assert any("traces" in r for r in obs.evidence_refs)
    assert any("screenshots" in r for r in obs.evidence_refs)
    assert any("videos" in r for r in obs.evidence_refs)


def test_task_workspace_evidence_ref_is_normalized_to_canonical_change_path(
    tmp_path: Path,
) -> None:
    """Moving execution out of a task workspace must not leave dead evidence links."""
    change_id = "CH-normalize-task-ref"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100007"
    canonical_rel = f"execution/runs/{batch_id}/raw/api.log"
    canonical_log = change_dir / canonical_rel
    canonical_log.parent.mkdir(parents=True, exist_ok=True)
    canonical_log.write_text("canonical api evidence\n", encoding="utf-8")
    stale_task_ref = change_dir / ".graph-runtime/tasks/task-123/qa/changes" / change_id / canonical_rel
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case(
                "API-001",
                "failed",
                message="AssertionError: expected 4xx, received 500",
                raw_log_ref=str(stale_task_ref),
            )
        ],
        status="failed",
    )

    result = collect_observations(
        change_dir,
        change_id,
        clock=lambda: "2026-07-25T10:00:00Z",
    )

    assert result.observations[0].evidence_refs == [
        f"execution/runs/{batch_id}/api-result.json",
        canonical_rel,
    ]
    manifest_entry = next(entry for entry in result.manifest.entries if entry.path == canonical_rel)
    expected_digest = hashlib.sha256(b"canonical api evidence\n").hexdigest()
    assert manifest_entry.digest == f"sha256:{expected_digest}"


def test_missing_declared_case_evidence_is_a_hard_failure(tmp_path: Path) -> None:
    """A missing evidence file must not be frozen as the empty-file digest."""
    change_id = "CH-missing-case-evidence"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100007"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case(
                "API-001",
                "failed",
                message="AssertionError: expected 4xx, received 500",
                raw_log_ref="execution/runs/20260725-100007/raw/missing.log",
            )
        ],
        status="failed",
    )

    with pytest.raises(EvidenceError, match="referenced evidence file missing"):
        collect_observations(change_dir, change_id)


def test_symlinked_evidence_cannot_escape_change_workspace(tmp_path: Path) -> None:
    """A Change-relative ref must not hash a symlink target outside the Change."""
    change_id = "CH-symlink-escape"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100007"
    outside_log = tmp_path / "outside-api.log"
    outside_log.write_text("outside evidence\n", encoding="utf-8")
    escaped_rel = f"execution/runs/{batch_id}/raw/api.log"
    escaped_log = change_dir / escaped_rel
    escaped_log.parent.mkdir(parents=True, exist_ok=True)
    escaped_log.symlink_to(outside_log)
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case(
                "API-001",
                "failed",
                message="AssertionError: expected 4xx, received 500",
                raw_log_ref=escaped_rel,
            )
        ],
        status="failed",
    )

    with pytest.raises(EvidenceError, match="outside Change workspace"):
        collect_observations(change_dir, change_id)


# ---------------------------------------------------------------------------
# Test: coverage_gap observations
# ---------------------------------------------------------------------------


def test_coverage_gap_observation_produced(tmp_path: Path) -> None:
    change_id = "CH-coverage"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100008"
    _write_manifest(
        change_dir,
        batch_id=batch_id,
        targets={
            "api": True,
            "e2e": False,
            "fuzz": False,
            "performance": False,
            "coverage": True,
        },
    )
    # Write the coverage result file
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    coverage_data = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": batch_id,
        "kind": "coverage",
        "available": True,
        "line_coverage": 65.0,
        "branch_coverage": 60.0,
        "threshold": {"line": 80.0, "branch": 70.0},
        "status": "FAIL",
        "uncovered_critical_files": [
            {"file": "app/api/dept.py", "line_coverage": 40.0},
        ],
    }
    (batch_dir / "coverage-result.json").write_text(json.dumps(coverage_data), encoding="utf-8")
    # Update manifest to include coverage
    manifest_data = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": batch_id,
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "result_files": {
            "api": f"runs/{batch_id}/api-result.json",
            "coverage": f"runs/{batch_id}/coverage-result.json",
        },
    }
    (change_dir / "execution" / "execution-manifest.yaml").write_text(
        yaml.safe_dump(manifest_data), encoding="utf-8"
    )
    _write_api_result(change_dir, batch_id)

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    coverage_obs = [o for o in result.observations if o.kind == "coverage_gap"]
    assert len(coverage_obs) >= 1
    obs = coverage_obs[0]
    assert obs.target == "coverage"
    assert "coverage-result.json" in obs.evidence_refs[0]


# ---------------------------------------------------------------------------
# Test: performance_signal observations
# ---------------------------------------------------------------------------


def test_performance_signal_observation_produced(tmp_path: Path) -> None:
    change_id = "CH-perf"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100009"
    targets = {"api": False, "e2e": False, "fuzz": False, "performance": True}
    manifest_data = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": batch_id,
        "selected_targets": targets,
        "result_files": {
            "performance": f"runs/{batch_id}/performance-result.json",
        },
    }
    execution_dir = change_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    batch_dir = execution_dir / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    (execution_dir / "execution-manifest.yaml").write_text(yaml.safe_dump(manifest_data), encoding="utf-8")
    perf_data = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": batch_id,
        "kind": "performance",
        "available": True,
        "status": "FAIL",
        "scenarios": [
            {
                "capability": "dept_list_p99",
                "endpoint": "GET /departments",
                "measured_p95_ms": 800.0,
                "threshold_p95_ms": 500.0,
                "measured_error_rate": 0.0,
                "threshold_error_rate_max": 0.01,
                "verdict": "FAIL",
            },
            {
                "capability": "user_login_p99",
                "endpoint": "POST /login",
                "measured_p95_ms": 100.0,
                "threshold_p95_ms": 500.0,
                "measured_error_rate": 0.0,
                "threshold_error_rate_max": 0.01,
                "verdict": "PASS",
            },
        ],
        "command": "locust",
        "source": {},
    }
    (batch_dir / "performance-result.json").write_text(json.dumps(perf_data), encoding="utf-8")

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    perf_obs = [o for o in result.observations if o.kind == "performance_signal"]
    assert len(perf_obs) == 1  # Only the FAIL scenario
    assert perf_obs[0].target == "performance"
    assert "dept_list_p99" in perf_obs[0].signature


# ---------------------------------------------------------------------------
# Test: review_finding observations from review/*.json
# ---------------------------------------------------------------------------


def test_review_warning_produces_review_finding(tmp_path: Path) -> None:
    change_id = "CH-review"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100010"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(change_dir, batch_id)

    review_dir = change_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "api-plan-review.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "decision": "needs_fix",
                "findings": [
                    {"type": "missing_negative_test", "severity": "high", "message": "Add 400 case"},
                ],
                "risk_level": "high",
            }
        ),
        encoding="utf-8",
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    review_obs = [o for o in result.observations if o.kind == "review_finding"]
    assert len(review_obs) >= 1
    assert review_obs[0].target == "api"
    assert "review/api-plan-review.json" in review_obs[0].evidence_refs[0]


# ---------------------------------------------------------------------------
# Test: workaround observations from healing apply summaries
# ---------------------------------------------------------------------------


def test_healing_apply_produces_workaround_observation(tmp_path: Path) -> None:
    change_id = "CH-healing"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100011"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(change_dir, batch_id)

    healing_dir = change_dir / "healing"
    healing_dir.mkdir(parents=True, exist_ok=True)
    (healing_dir / "api-apply-summary.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "target": "api",
                "applied": True,
            }
        ),
        encoding="utf-8",
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    workaround_obs = [o for o in result.observations if o.kind == "workaround"]
    assert len(workaround_obs) >= 1
    assert workaround_obs[0].target == "api"


def test_healing_skip_xfail_added_produces_workaround(tmp_path: Path) -> None:
    change_id = "CH-heal-xfail"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100012"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(change_dir, batch_id)

    healing_dir = change_dir / "healing"
    healing_dir.mkdir(parents=True, exist_ok=True)
    (healing_dir / "fixer-safety-check.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "passed": True,
                "needs_review": False,
                "product_code_modified": False,
                "skip_or_xfail_added": True,
                "unrelated_tests_modified": False,
                "assertion_expected_value_changes_detected": False,
                "high_risk_proposal_applied": False,
            }
        ),
        encoding="utf-8",
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    workaround_obs = [o for o in result.observations if o.kind == "workaround"]
    assert any("skip_or_xfail_added" in o.source.json_pointer for o in workaround_obs)


# ---------------------------------------------------------------------------
# Test: OBS- ID determinism (replay produces the same ID)
# ---------------------------------------------------------------------------


def test_observation_id_is_deterministic_on_replay(tmp_path: Path) -> None:
    change_id = "CH-replay"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100013"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("API-001", "failed", message="HTTP 500"),
        ],
    )

    result1 = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")
    result2 = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert len(result1.observations) == 1
    assert result1.observations[0].observation_id == result2.observations[0].observation_id
    assert result1.evidence_bundle_digest == result2.evidence_bundle_digest


# ---------------------------------------------------------------------------
# Test: evidence manifest entries cover all observation evidence_refs
# ---------------------------------------------------------------------------


def test_every_observation_has_evidence_in_manifest(tmp_path: Path) -> None:
    change_id = "CH-manifest-check"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100014"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("API-001", "failed", message="Assertion error"),
            _make_case("API-002", "skipped", message="workaround: known issue"),
        ],
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    manifest_paths = {e.path for e in result.manifest.entries}
    for obs in result.observations:
        for ref in obs.evidence_refs:
            # Strip anchors for path matching
            clean = ref.split("#")[0]
            assert clean in manifest_paths, (
                f"Observation {obs.observation_id} references {ref!r} but it is not in the evidence manifest"
            )


# ---------------------------------------------------------------------------
# Test: multiple targets combined
# ---------------------------------------------------------------------------


def test_multiple_targets_api_and_e2e(tmp_path: Path) -> None:
    change_id = "CH-multi"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100015"
    _write_manifest(
        change_dir,
        batch_id=batch_id,
        targets={
            "api": True,
            "e2e": True,
            "fuzz": False,
            "performance": False,
        },
    )
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("API-001", "failed"),
        ],
    )
    _write_e2e_result(
        change_dir,
        batch_id,
        cases=[
            _make_case("E2E-001", "failed"),
        ],
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    targets = {obs.target for obs in result.observations}
    assert "api" in targets
    assert "e2e" in targets
    assert len(result.observations) == 2


# ---------------------------------------------------------------------------
# Test: secret redaction does not break manifest digest
# ---------------------------------------------------------------------------


def test_secret_in_result_file_is_redacted_before_hashing(tmp_path: Path) -> None:
    change_id = "CH-secret"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100016"
    _write_manifest(change_dir, batch_id=batch_id)
    # Write a result file with a secret in it
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    result_with_secret = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": batch_id,
        "target": "api",
        "status": "failed",
        "command": "pytest",
        "source": {"framework": "pytest", "raw_log": "Authorization: Bearer sk-abc123456789012345678"},
        "total": 1,
        "passed": 0,
        "failed": 1,
        "skipped": 0,
        "cases": [_make_case("API-001", "failed", message="HTTP 500 Bearer sk-abc123456789012345678")],
        "unmapped_tests": [],
    }
    (batch_dir / "api-result.json").write_text(json.dumps(result_with_secret), encoding="utf-8")

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    # Should succeed and produce observation (secret in message doesn't block collection)
    assert len(result.observations) >= 1
    # Digest should be deterministic
    result2 = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")
    assert result.evidence_bundle_digest == result2.evidence_bundle_digest


# ---------------------------------------------------------------------------
# Test: fuzz target results collected
# ---------------------------------------------------------------------------


def test_fuzz_failed_case_produces_test_failure(tmp_path: Path) -> None:
    change_id = "CH-fuzz"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100017"
    _write_manifest(
        change_dir,
        batch_id=batch_id,
        targets={
            "api": False,
            "e2e": False,
            "fuzz": True,
            "performance": False,
        },
    )
    batch_dir = change_dir / "execution" / "runs" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest_data = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": batch_id,
        "selected_targets": {"api": False, "e2e": False, "fuzz": True, "performance": False},
        "result_files": {"fuzz": f"runs/{batch_id}/fuzz-result.json"},
    }
    (change_dir / "execution" / "execution-manifest.yaml").write_text(
        yaml.safe_dump(manifest_data), encoding="utf-8"
    )
    fuzz_result = {
        "schema_version": "1.0",
        "change_id": change_id,
        "batch_id": batch_id,
        "target": "fuzz",
        "status": "failed",
        "command": "atheris",
        "source": {"framework": "atheris", "raw_log": ""},
        "total": 1,
        "passed": 0,
        "failed": 1,
        "skipped": 0,
        "cases": [_make_case("FUZZ-001", "failed", message="fuzz stateful failure")],
        "unmapped_tests": [],
    }
    (batch_dir / "fuzz-result.json").write_text(json.dumps(fuzz_result), encoding="utf-8")

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.kind == "test_failure"
    assert obs.target == "fuzz"


# ---------------------------------------------------------------------------
# Safe evidence-entry digests (Task 6)
# ---------------------------------------------------------------------------


def test_clean_batch_manifest_digest_bytes_remain_pinned(tmp_path: Path) -> None:
    change_id = "CH-GOLDEN"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100000"
    execution_dir = change_dir / "execution"
    batch_dir = execution_dir / "runs" / batch_id
    batch_dir.mkdir(parents=True)
    (execution_dir / "execution-manifest.yaml").write_text(_CLEAN_MANIFEST_YAML, encoding="utf-8")
    (batch_dir / "api-result.json").write_text(_CLEAN_API_RESULT_JSON, encoding="utf-8")

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert result.evidence_bundle_digest == _CLEAN_BUNDLE_DIGEST_GOLDEN
    assert result.manifest.digest == _CLEAN_BUNDLE_DIGEST_GOLDEN
    assert len(result.manifest.entries) == 1
    assert result.manifest.entries[0].path == "execution/execution-manifest.yaml"
    assert result.manifest.entries[0].digest == _CLEAN_ANCHOR_DIGEST_GOLDEN
    assert result.manifest.digest == evidence_bundle_digest_v1(result.manifest.entries)
    anchor_bytes = _CLEAN_MANIFEST_YAML.encode("utf-8")
    assert result.manifest.entries[0].digest == evidence_entry_digest_v1(anchor_bytes)


def test_collector_manifest_entry_matches_one_read_validated_entry(tmp_path: Path) -> None:
    change_id = "CH-one-read"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100018"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[_make_case("API-001", "failed", message="boom")],
        status="failed",
    )

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")
    for entry in result.manifest.entries:
        validated = read_evidence_entry_v1(change_dir, entry.path)
        assert validated.entry_digest == entry.digest
        assert validated.raw_sha256 == raw_sha256(validated.data)
        assert validated.entry_digest == evidence_entry_digest_v1(validated.data)


def test_collector_rejects_symlinked_evidence_file_under_allowlist(tmp_path: Path) -> None:
    """Even an in-tree symlink target must not be hashed (O_NOFOLLOW)."""
    change_id = "CH-symlink-file"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100019"
    real = change_dir / "execution" / "runs" / batch_id / "raw" / "real.log"
    real.parent.mkdir(parents=True)
    real.write_text("real\n", encoding="utf-8")
    link = change_dir / "execution" / "runs" / batch_id / "raw" / "api.log"
    os.symlink(real.name, link)  # relative symlink within same directory
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[
            _make_case(
                "API-001",
                "failed",
                message="fail",
                raw_log_ref=f"execution/runs/{batch_id}/raw/api.log",
            )
        ],
        status="failed",
    )

    with pytest.raises(EvidenceError):
        collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")


def test_collector_manifest_paths_are_unique_and_non_aliasing(tmp_path: Path) -> None:
    """Alias forms are rejected; accepted manifest entry paths stay unique."""
    from assurance_agent.evidence.digests import EvidenceEntryPathError, normalize_evidence_entry_path

    with pytest.raises(EvidenceEntryPathError):
        normalize_evidence_entry_path("execution/./api-result.json")

    change_id = "CH-dup-path"
    change_dir = tmp_path / change_id
    batch_id = "20260725-100020"
    _write_manifest(change_dir, batch_id=batch_id)
    _write_api_result(
        change_dir,
        batch_id,
        cases=[_make_case("API-001", "failed", message="fail")],
        status="failed",
    )
    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")
    paths = [e.path for e in result.manifest.entries]
    assert len(paths) == len(set(paths))
    assert all(normalize_evidence_entry_path(p) == p for p in paths)


def test_build_evidence_manifest_rejects_duplicate_normalized_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct manifest refs that normalize to the same path must fail closed."""
    from assurance_agent.artifacts.models.issues import Observation, ObservationSource
    from assurance_agent.evidence.digests import ValidatedEvidenceEntry
    from assurance_agent.workflow.issues.collector import _build_evidence_manifest

    change_id = "CH-dup-normalized"
    batch_id = "20260725-100021"
    change_dir = tmp_path / change_id
    change_dir.mkdir()
    normalized_path = "execution/runs/b/result.json"

    def fake_read(_change_dir: Path, _path: str) -> ValidatedEvidenceEntry:
        return ValidatedEvidenceEntry(
            path=normalized_path,
            data=b"payload",
            entry_digest="sha256:abc",
            raw_sha256="abc",
        )

    monkeypatch.setattr(
        "assurance_agent.workflow.issues.collector.read_evidence_entry_v1",
        fake_read,
    )

    observation = Observation(
        observation_id="OBS-dup-path",
        change_id=change_id,
        batch_id=batch_id,
        kind="test_failure",
        target="api",
        source=ObservationSource(
            artifact="execution/runs/b/api-result.json",
            json_pointer="/cases/0",
        ),
        evidence_refs=[
            "execution/runs/b/alias-result.json",
            normalized_path,
        ],
        signature="sig-dup-path",
        observed_at="2026-07-25T10:00:00Z",
    )

    with pytest.raises(EvidenceError, match=f"duplicate evidence path: {normalized_path}"):
        _build_evidence_manifest(
            change_id=change_id,
            batch_id=batch_id,
            anchor_path="execution/execution-manifest.yaml",
            observations=[observation],
            change_dir=change_dir,
        )
