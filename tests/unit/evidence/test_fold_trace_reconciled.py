"""fold_trace reconciled-phase enrichment (Task 10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.artifacts.models.trace import TraceFailure
from assurance_agent.evidence.trace import fold_trace
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-1"
BATCH_ID = "20260729-120000"
EXECUTED_AT = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
CASE_ID = "TC_DEPT_API_001"

_SHARED_ROW_FIELDS = (
    "case_id",
    "module",
    "case_type",
    "automation_required",
    "assertions",
    "covering_tests",
    "coverage_state",
    "latest_execution",
    "freshest_pass",
    "presence_in_current_batch",
    "atemporal_kinds_present",
)

_CLOSED_STATUSES = ("resolved", "not_an_issue", "accepted_risk")
_OPEN_STATUSES = ("detected", "triaged", "in_progress", "verification_pending")
_CLASSIFICATIONS = (
    "product_bug",
    "test_bug",
    "test_data_issue",
    "environment_issue",
    "coverage_gap",
    "performance_issue",
    "workflow_issue",
    "unknown",
)


def _setup_project(tmp_path: Path) -> Path:
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    return change_dir


def _write_api_case(change_dir: Path, case_id: str = CASE_ID) -> None:
    path = change_dir / "cases" / "dept" / "case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
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
      required: true
modified: []
removed: []
""",
        encoding="utf-8",
    )


def _write_manifest(change_dir: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "executed_at": EXECUTED_AT.isoformat(),
        "selected_targets": SelectedTargets(api=True, e2e=False, fuzz=False, performance=False).model_dump(),
        "result_files": {},
    }
    manifest_path = change_dir / "execution" / "execution-manifest.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _failure_evidence() -> dict[str, str]:
    return {
        "result_file": "execution/runs/x/api-result.json",
        "test_file": "tests/api/test_dept.py",
        "trace": "",
        "screenshot": "",
        "video": "",
        "raw_log": "",
        "log_excerpt": "",
    }


def _failure_entry(
    *,
    case_id: str = CASE_ID,
    category: str = "assertion_failure",
    severity: str = "high",
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "target": "api",
        "category": category,
        "fix_proposal_eligible": False,
        "severity": severity,
        "evidence": _failure_evidence(),
        "diagnosis": "diag",
        "recommended_action": "fix",
    }


def _write_failure_analysis(change_dir: Path, failures: list[dict[str, object]]) -> None:
    inspect_dir = change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "source_manifest": "execution/execution-manifest.yaml",
        "inspection_status": "completed",
        "batch_id": BATCH_ID,
        "source_batch_id": BATCH_ID,
        "final_status": "FAIL",
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "analyzed",
        "failures": failures,
        "hard_fails": [],
        "needs_review": [],
        "known_product_issues": [],
    }
    (inspect_dir / "failure-analysis.json").write_text(json.dumps(payload), encoding="utf-8")


def _observation(*, case_id: str, observation_id: str) -> dict[str, object]:
    return {
        "observation_id": observation_id,
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "kind": "test_failure",
        "target": "api",
        "case_id": case_id,
        "source": {
            "artifact": "inspect/failure-analysis.json",
            "json_pointer": "/failures/0",
        },
        "evidence_refs": ["evidence:1"],
        "signature": f"sig-{observation_id}",
        "observed_at": "2026-07-29T12:00:00Z",
    }


def _occurrence(*, occurrence_id: str, observation_id: str, problem_id: str) -> dict[str, object]:
    return {
        "occurrence_id": occurrence_id,
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "observation_ids": [observation_id],
        "problem_id": problem_id,
        "provisional_assessment": {
            "classification": "product_bug",
            "severity": "high",
            "authority": "llm_provisional",
            "root_cause_hypothesis": "hypothesis",
        },
        "analysis": {
            "evidence_bundle_digest": "sha256:evidence",
            "analyzer": "test-analyzer",
            "prompt_version": "1.0",
            "candidate_digest": "sha256:candidate",
        },
    }


def _write_issues_snapshot(
    change_dir: Path,
    *,
    observations: list[dict[str, object]],
    occurrences: list[dict[str, object]],
) -> None:
    issues_dir = change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "authoritative_batch_id": BATCH_ID,
        "observations": observations,
        "occurrences": occurrences,
        "project_sync_status": "completed",
        "batches": [BATCH_ID],
    }
    (issues_dir / "snapshot.json").write_text(json.dumps(payload), encoding="utf-8")


def _problem(
    problem_id: str,
    *,
    status: str = "detected",
    classification: str = "product_bug",
    fingerprint_digest: str | None = None,
    resolution: dict[str, object] | None = None,
) -> dict[str, object]:
    digest = fingerprint_digest or f"sha256:{problem_id}"
    return {
        "problem_id": problem_id,
        "fingerprint": {"version": "1", "digest": digest},
        "title": f"Problem {problem_id}",
        "assessment": {
            "classification": classification,
            "severity": "high",
            "authority": "llm_provisional",
            "root_cause_hypothesis": "hypothesis",
        },
        "status": status,
        "first_seen": {"change_id": CHANGE_ID, "occurrence_id": "OCC-1"},
        "last_seen": {"change_id": CHANGE_ID, "occurrence_id": "OCC-1"},
        "occurrences": ["OCC-1"],
        "verification_request": None,
        "resolution": resolution,
        "version": 1,
    }


def _write_problems(project_root: Path, problems: list[dict[str, object]]) -> None:
    problems_dir = project_root / "qa" / "issues"
    problems_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "generated_at": "2026-07-29T12:00:00Z",
        "problems": problems,
    }
    (problems_dir / "problems.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_reconciled_inputs(
    tmp_path: Path,
    *,
    failures: list[dict[str, object]] | None = None,
    observations: list[dict[str, object]] | None = None,
    occurrences: list[dict[str, object]] | None = None,
    problems: list[dict[str, object]] | None = None,
    write_failure_analysis: bool = True,
    write_issues: bool = True,
    write_problems: bool = True,
) -> Path:
    change_dir = _setup_project(tmp_path)
    _write_api_case(change_dir)
    _write_manifest(change_dir)
    if write_failure_analysis:
        _write_failure_analysis(change_dir, failures or [])
    if write_issues:
        _write_issues_snapshot(
            change_dir,
            observations=observations or [],
            occurrences=occurrences or [],
        )
    if write_problems:
        _write_problems(tmp_path, problems or [])
    return change_dir


def _shared_row_view(row: object) -> dict[str, object]:
    return {field: getattr(row, field) for field in _SHARED_ROW_FIELDS}


def test_failure_analysis_missing_emits_gap(tmp_path: Path) -> None:
    change_dir = _seed_reconciled_inputs(tmp_path, write_failure_analysis=False)
    _write_issues_snapshot(change_dir, observations=[], occurrences=[])
    _write_problems(tmp_path, [])

    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert any(gap.code == "failure_analysis_missing" for gap in projection.gaps)
    assert projection.phase == "reconciled"


def test_failures_preserved_in_document_order(tmp_path: Path) -> None:
    _seed_reconciled_inputs(
        tmp_path,
        failures=[
            _failure_entry(category="assertion_failure", severity="high"),
            _failure_entry(category="environment_failure", severity="low"),
        ],
    )
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    row = projection.rows[0]
    assert row.failures == (
        TraceFailure(category="assertion_failure", severity="high"),
        TraceFailure(category="environment_failure", severity="low"),
    )


def test_merge_alias_follows_resolved_source_to_open_canonical(tmp_path: Path) -> None:
    _seed_reconciled_inputs(
        tmp_path,
        observations=[_observation(case_id=CASE_ID, observation_id="OBS-1")],
        occurrences=[_occurrence(occurrence_id="OCC-1", observation_id="OBS-1", problem_id="PROB-alias")],
        problems=[
            _problem(
                "PROB-alias",
                status="resolved",
                resolution={
                    "resolved_at": "2026-07-29T12:00:00Z",
                    "change_id": CHANGE_ID,
                    "batch_id": BATCH_ID,
                    "disposition": "merged_into:PROB-canonical",
                    "verification_scope": ["merged"],
                    "evidence_digest": "sha256:merge",
                },
            ),
            _problem("PROB-canonical", status="detected", classification="product_bug"),
        ],
    )
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert projection.rows[0].open_problem_ids == ("PROB-canonical",)


def test_merge_alias_cycle_emits_problem_alias_invalid(tmp_path: Path) -> None:
    merge_resolution = {
        "resolved_at": "2026-07-29T12:00:00Z",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "disposition": "merged_into:PROB-b",
        "verification_scope": ["merged"],
        "evidence_digest": "sha256:merge",
    }
    _seed_reconciled_inputs(
        tmp_path,
        observations=[_observation(case_id=CASE_ID, observation_id="OBS-1")],
        occurrences=[_occurrence(occurrence_id="OCC-1", observation_id="OBS-1", problem_id="PROB-a")],
        problems=[
            _problem(
                "PROB-a",
                status="resolved",
                resolution={
                    **merge_resolution,
                    "disposition": "merged_into:PROB-b",
                },
            ),
            _problem(
                "PROB-b",
                status="resolved",
                resolution={
                    **merge_resolution,
                    "disposition": "merged_into:PROB-a",
                },
            ),
        ],
    )
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert any(gap.code == "problem_alias_invalid" for gap in projection.gaps)
    assert projection.rows[0].open_problem_ids == ()


def test_merge_alias_missing_target_emits_problem_alias_invalid(tmp_path: Path) -> None:
    _seed_reconciled_inputs(
        tmp_path,
        observations=[_observation(case_id=CASE_ID, observation_id="OBS-1")],
        occurrences=[_occurrence(occurrence_id="OCC-1", observation_id="OBS-1", problem_id="PROB-alias")],
        problems=[
            _problem(
                "PROB-alias",
                status="resolved",
                resolution={
                    "resolved_at": "2026-07-29T12:00:00Z",
                    "change_id": CHANGE_ID,
                    "batch_id": BATCH_ID,
                    "disposition": "merged_into:PROB-missing",
                    "verification_scope": ["merged"],
                    "evidence_digest": "sha256:merge",
                },
            ),
        ],
    )
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert any(gap.code == "problem_alias_invalid" for gap in projection.gaps)
    assert projection.rows[0].open_problem_ids == ()


def test_empty_problems_with_occurrence_emits_problem_alias_invalid(tmp_path: Path) -> None:
    _seed_reconciled_inputs(
        tmp_path,
        observations=[_observation(case_id=CASE_ID, observation_id="OBS-1")],
        occurrences=[_occurrence(occurrence_id="OCC-1", observation_id="OBS-1", problem_id="PROB-missing")],
        problems=[],
    )
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert any(gap.code == "problem_alias_invalid" for gap in projection.gaps)
    assert projection.rows[0].open_problem_ids == ()


@pytest.mark.parametrize("status", _CLOSED_STATUSES)
@pytest.mark.parametrize("classification", _CLASSIFICATIONS)
def test_closed_or_non_product_bug_never_open(
    tmp_path: Path,
    status: str,
    classification: str,
) -> None:
    _seed_reconciled_inputs(
        tmp_path,
        observations=[_observation(case_id=CASE_ID, observation_id="OBS-1")],
        occurrences=[_occurrence(occurrence_id="OCC-1", observation_id="OBS-1", problem_id="PROB-1")],
        problems=[_problem("PROB-1", status=status, classification=classification)],
    )
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert projection.rows[0].open_problem_ids == ()


@pytest.mark.parametrize("status", _OPEN_STATUSES)
def test_open_product_bug_is_reported(tmp_path: Path, status: str) -> None:
    _seed_reconciled_inputs(
        tmp_path,
        observations=[_observation(case_id=CASE_ID, observation_id="OBS-1")],
        occurrences=[_occurrence(occurrence_id="OCC-1", observation_id="OBS-1", problem_id="PROB-1")],
        problems=[_problem("PROB-1", status=status, classification="product_bug")],
    )
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert projection.rows[0].open_problem_ids == ("PROB-1",)


@pytest.mark.parametrize("status", _OPEN_STATUSES)
@pytest.mark.parametrize("classification", [c for c in _CLASSIFICATIONS if c != "product_bug"])
def test_open_non_product_bug_is_not_reported(
    tmp_path: Path,
    status: str,
    classification: str,
) -> None:
    _seed_reconciled_inputs(
        tmp_path,
        observations=[_observation(case_id=CASE_ID, observation_id="OBS-1")],
        occurrences=[_occurrence(occurrence_id="OCC-1", observation_id="OBS-1", problem_id="PROB-1")],
        problems=[_problem("PROB-1", status=status, classification=classification)],
    )
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert projection.rows[0].open_problem_ids == ()


def test_fingerprint_dedup_keeps_one_canonical_id(tmp_path: Path) -> None:
    shared_digest = "sha256:shared-fingerprint"
    _seed_reconciled_inputs(
        tmp_path,
        observations=[
            _observation(case_id=CASE_ID, observation_id="OBS-1"),
            _observation(case_id=CASE_ID, observation_id="OBS-2"),
        ],
        occurrences=[
            _occurrence(occurrence_id="OCC-1", observation_id="OBS-1", problem_id="PROB-a"),
            _occurrence(occurrence_id="OCC-2", observation_id="OBS-2", problem_id="PROB-b"),
        ],
        problems=[
            _problem("PROB-a", status="detected", fingerprint_digest=shared_digest),
            _problem("PROB-b", status="detected", fingerprint_digest=shared_digest),
        ],
    )
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert len(projection.rows[0].open_problem_ids) == 1
    assert projection.rows[0].open_problem_ids[0] in {"PROB-a", "PROB-b"}


def test_dual_phase_shared_fields_are_equal(tmp_path: Path) -> None:
    _seed_reconciled_inputs(
        tmp_path,
        failures=[_failure_entry()],
        observations=[_observation(case_id=CASE_ID, observation_id="OBS-1")],
        occurrences=[_occurrence(occurrence_id="OCC-1", observation_id="OBS-1", problem_id="PROB-1")],
        problems=[_problem("PROB-1", status="detected", classification="product_bug")],
    )
    execution = fold_trace(tmp_path, CHANGE_ID, phase="execution")
    reconciled = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert len(execution.rows) == len(reconciled.rows)
    for exec_row, rec_row in zip(execution.rows, reconciled.rows, strict=True):
        assert _shared_row_view(exec_row) == _shared_row_view(rec_row)
        assert exec_row.failures == ()
        assert exec_row.open_problem_ids == ()


def test_reconciled_sources_include_three_inputs(tmp_path: Path) -> None:
    _seed_reconciled_inputs(tmp_path)
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    paths = {src.path for src in projection.sources}
    assert "inspect/failure-analysis.json" in paths
    assert "issues/snapshot.json" in paths
    assert "qa/issues/problems.json" in paths
