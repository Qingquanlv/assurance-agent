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

import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.workflow.execution.evidence import EvidenceError
from assurance_agent.workflow.issues.collector import collect_observations


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

    result = collect_observations(change_dir, change_id, clock=lambda: "2026-07-25T10:00:00Z")

    assert len(result.observations) == 1
    obs = result.observations[0]
    assert any("traces" in r for r in obs.evidence_refs)
    assert any("screenshots" in r for r in obs.evidence_refs)
    assert any("videos" in r for r in obs.evidence_refs)


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
            {"name": "dept_list_p99", "verdict": "FAIL"},
            {"name": "user_login_p99", "verdict": "PASS"},
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
