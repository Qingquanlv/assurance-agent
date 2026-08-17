"""fold_trace reconciled-phase enrichment (Task 10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.artifacts.models.trace import TraceFailure
from assurance_agent.evidence.layer_summary import (
    summarize_projection_by_layer,
    validate_trace_phase_pair,
)
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
    manifest_path = change_dir / "execution" / "execution-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")


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
        "source_manifest": "execution/execution-manifest.json",
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


@pytest.mark.parametrize("status", _CLOSED_STATUSES)
@pytest.mark.parametrize("classification", _CLASSIFICATIONS)
def test_closed_or_non_product_bug_never_open_without_authority(
    tmp_path: Path,
    status: str,
    classification: str,
) -> None:
    """Legacy snapshot-only seeds cannot mint problem links after authority activation."""
    _seed_reconciled_inputs(
        tmp_path,
        observations=[_observation(case_id=CASE_ID, observation_id="OBS-1")],
        occurrences=[_occurrence(occurrence_id="OCC-1", observation_id="OBS-1", problem_id="PROB-1")],
        problems=[_problem("PROB-1", status=status, classification=classification)],
    )
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert projection.schema_version == "2"
    assert projection.rows[0].open_problem_ids == ()


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
    assert execution.schema_version == "2"
    assert reconciled.schema_version == "2"
    assert len(execution.rows) == len(reconciled.rows)
    for exec_row, rec_row in zip(execution.rows, reconciled.rows, strict=True):
        assert _shared_row_view(exec_row) == _shared_row_view(rec_row)
        assert exec_row.failures == ()
        assert exec_row.open_problem_ids == ()
    summarize_projection_by_layer(execution)
    summarize_projection_by_layer(reconciled)
    validate_trace_phase_pair(execution, reconciled)


def test_reconciled_sources_include_authority_inputs(tmp_path: Path) -> None:
    _seed_reconciled_inputs(tmp_path)
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    paths = {src.path for src in projection.sources}
    assert "inspect/failure-analysis.json" in paths
    assert "inspect/issue-evidence-manifest.json" in paths
    assert "qa/issues/problems.json" in paths


# ---------------------------------------------------------------------------
# Task 12 — V2 fold activation, current freshness, authority enrichment
# ---------------------------------------------------------------------------


def _ensure_case_for_authority(project_root: Path) -> Path:
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    _write_api_case(change_dir)
    return change_dir


def _write_selected_api_result(change_dir: Path, batch_id: str) -> None:
    """Satisfy selected api target so authority tests are not masked by result_missing."""
    path = change_dir / "execution" / "runs" / batch_id / "api-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "change_id": CHANGE_ID,
                "batch_id": batch_id,
                "target": "api",
                "cases": [],
                "unmapped_tests": [],
            }
        ),
        encoding="utf-8",
    )


def _authority_project_root(change_dir: Path) -> Path:
    return change_dir.parents[2]


@pytest.fixture
def authority_tree(tmp_path: Path) -> Path:
    from tests.unit.evidence import test_issue_replay_authority as auth

    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    auth._write_json(change_dir / auth.FAILURE_SOURCE, auth._failure_payload())
    digest, candidates, observation = auth._seed_manifest_tree(change_dir)
    c_digest = auth.candidate_document_digest(candidates)
    events = [
        auth._obs_recorded(observation, seq=1),
        auth._analysis_failed_event(
            evidence_digest=digest,
            candidate_digest=c_digest,
            candidate_count=0,
            seq=2,
        ),
    ]
    auth._write_ledger(change_dir, events)
    auth._write_snapshot_from_events(change_dir, events)
    _write_selected_api_result(change_dir, auth.BATCH_ID)
    return change_dir


@pytest.fixture
def completed_tree(tmp_path: Path) -> Path:
    from tests.unit.evidence import test_issue_replay_authority as auth

    root = auth._write_completed_with_occurrence(tmp_path)
    _write_selected_api_result(auth.change_dir(root), auth.BATCH_ID)
    return root


def preseed_b0_projection_and_write_b1_pending(authority_tree: Path) -> Path:
    """Replace analysis-failed B0 seed with B1 pending recovery and a stale B0 projection."""
    from tests.unit.evidence import test_issue_replay_authority as auth

    project_root = _authority_project_root(authority_tree)
    _ensure_case_for_authority(project_root)
    # Persist a B0-shaped reconciled projection before advancing to B1 pending.
    b0 = fold_trace(project_root, CHANGE_ID, phase="reconciled")
    out = authority_tree / "inspect" / "trace-projection.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(b0.model_dump_json(indent=2), encoding="utf-8")

    # Advance the execution anchor + issue authority inputs to literal batch B1 pending.
    for path in (
        authority_tree / auth.MANIFEST_SOURCE,
        authority_tree / auth.CANDIDATES_SOURCE,
        authority_tree / auth.OBSERVATIONS_SOURCE,
        authority_tree / auth.LEDGER_SOURCE,
        authority_tree / auth.SNAPSHOT_SOURCE,
        authority_tree / auth.RECONCILE_SOURCE,
        authority_tree / auth.FAILURE_SOURCE,
        authority_tree / auth.EXECUTION_ANCHOR,
        authority_tree / "execution/runs/secret.json",
        authority_tree / "execution/runs/visible.txt",
        authority_tree / auth.EXTRA_ENTRY,
    ):
        path.unlink(missing_ok=True)

    failure = auth._failure_payload()
    failure["batch_id"] = "B1"
    failure["source_batch_id"] = "B1"
    auth._write_json(authority_tree / auth.FAILURE_SOURCE, failure)

    original_batch = auth.BATCH_ID
    auth.BATCH_ID = "B1"
    try:
        auth.write_recovery_state(authority_tree, "project_sync_pending")
        anchor = yaml.safe_load((authority_tree / auth.EXECUTION_ANCHOR).read_text(encoding="utf-8"))
        assert anchor["batch_id"] == "B1"
    finally:
        auth.BATCH_ID = original_batch
    _write_selected_api_result(authority_tree, "B1")
    return project_root


def materialize_reconciled_v2(project_root: Path) -> Path:
    from assurance_agent.workflow.improvements.ledger import atomic_write_json

    _ensure_case_for_authority(project_root)
    projection = fold_trace(project_root, CHANGE_ID, phase="reconciled")
    assert projection.schema_version == "2"
    path = project_root / "qa" / "changes" / CHANGE_ID / "inspect" / "trace-projection.json"
    atomic_write_json(path, projection.model_dump(mode="json"))
    return path


def mutate_project_problem_through_valid_review(project_root: Path) -> None:
    """Append a valid review event that changes live problem authority digests."""
    from assurance_agent.artifacts.models.issue_events import (
        ProblemAssessmentConfirmedEvent,
        ProblemDetectedEvent,
    )
    from assurance_agent.evidence.issue_identity import event_id
    from assurance_agent.evidence.issue_replay import (
        dump_projection,
        project_problems,
        read_problem_events_from_bytes,
    )
    from tests.unit.evidence import test_issue_replay_authority as auth

    ledger = project_root / auth.PROJECT_LEDGER_SOURCE
    events = list(read_problem_events_from_bytes(ledger.read_bytes()))
    detected = next(event for event in events if isinstance(event, ProblemDetectedEvent))
    evidence_refs = [detected.occurrence_id]
    evidence_digest = auth._evidence_refs_digest(evidence_refs)
    key = f"review:confirm_assessment:{detected.problem_id}:1:{evidence_digest}"
    confirmed = ProblemAssessmentConfirmedEvent(
        schema_version="1.0",
        seq=max(event.seq for event in events) + 1,
        event_id=event_id(key),
        idempotency_key=key,
        ts="2026-07-30T12:00:00Z",
        evidence_digest=evidence_digest,
        problem_id=detected.problem_id,
        expected_problem_version=1,
        type="problem_assessment_confirmed",
        classification="product_bug",
        severity="high",
        root_cause_hypothesis="reviewed",
        reason="confirmed",
        evidence_refs=evidence_refs,
    )
    auth._append_problem_event(ledger, confirmed)
    (project_root / auth.PROJECT_PROBLEMS_SOURCE).write_bytes(
        dump_projection(project_problems(tuple([*events, confirmed])))
    )


def test_recovery_fold_is_current_incomplete_without_old_problem_links(
    authority_tree: Path,
) -> None:
    project_root = preseed_b0_projection_and_write_b1_pending(authority_tree)
    projection = fold_trace(project_root, CHANGE_ID, phase="reconciled")
    assert projection.schema_version == "2"
    assert projection.authoritative_batch_id == "B1"
    assert projection.integrity == "incomplete"
    assert {gap.code for gap in projection.gaps} == {"project_sync_pending"}
    assert all(row.open_problem_ids == () for row in projection.rows)


def test_valid_completed_fold_emits_v2_with_problem_links(completed_tree: Path) -> None:
    _ensure_case_for_authority(completed_tree)
    projection = fold_trace(completed_tree, CHANGE_ID, phase="reconciled")
    assert projection.schema_version == "2"
    assert projection.integrity == "complete"
    assert projection.rows[0].open_problem_ids
    assert projection.rows[0].failures


def test_failure_identity_mismatch_keeps_independent_problem_links(completed_tree: Path) -> None:
    from tests.unit.evidence import test_issue_replay_authority as auth

    _ensure_case_for_authority(completed_tree)
    change_dir = auth.change_dir(completed_tree)
    auth.mutate_failure(change_dir, "wrong_change")
    projection = fold_trace(completed_tree, CHANGE_ID, phase="reconciled")
    assert any(gap.code == "failure_analysis_identity_mismatch" for gap in projection.gaps)
    assert projection.rows[0].failures == ()
    assert projection.rows[0].open_problem_ids


@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("analysis_failed", "issue_analysis_failed"),
        ("reconcile_failed", "issue_reconcile_failed"),
        ("project_sync_pending", "project_sync_pending"),
    ],
)
def test_recovery_states_fold_incomplete_without_problem_links(
    tmp_path: Path,
    state: str,
    code: str,
) -> None:
    from tests.unit.evidence import test_issue_replay_authority as auth

    change_dir = _ensure_case_for_authority(tmp_path)
    auth._write_json(change_dir / auth.FAILURE_SOURCE, auth._failure_payload())
    auth.write_recovery_state(change_dir, state)
    _write_selected_api_result(change_dir, auth.BATCH_ID)
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    assert projection.schema_version == "2"
    assert projection.integrity == "incomplete"
    assert {gap.code for gap in projection.gaps if gap.code == code} == {code}
    assert all(row.open_problem_ids == () for row in projection.rows)


def test_unavailable_prefix_clears_problem_links(authority_tree: Path) -> None:
    from tests.unit.evidence import test_issue_replay_authority as auth

    project_root = _authority_project_root(authority_tree)
    _ensure_case_for_authority(project_root)
    auth.mutate_prefix(authority_tree, "manifest_missing")
    projection = fold_trace(project_root, CHANGE_ID, phase="reconciled")
    assert any(gap.code == "issue_reconciliation_unavailable" for gap in projection.gaps)
    assert all(row.open_problem_ids == () for row in projection.rows)


def test_recovery_plus_independent_project_gap(tmp_path: Path) -> None:
    from tests.unit.evidence import test_issue_replay_authority as auth

    change_dir = _ensure_case_for_authority(tmp_path)
    auth._write_json(change_dir / auth.FAILURE_SOURCE, auth._failure_payload())
    auth.write_recovery_state(change_dir, "analysis_failed")
    _write_selected_api_result(change_dir, auth.BATCH_ID)
    # Independent project projection gap: present malformed problems.json.
    problems = tmp_path / auth.PROJECT_PROBLEMS_SOURCE
    problems.parent.mkdir(parents=True, exist_ok=True)
    problems.write_text("{not-json", encoding="utf-8")
    projection = fold_trace(tmp_path, CHANGE_ID, phase="reconciled")
    codes = {gap.code for gap in projection.gaps}
    assert "issue_analysis_failed" in codes
    assert "problems_snapshot_missing" in codes
    assert all(row.open_problem_ids == () for row in projection.rows)


def test_authority_sources_deduplicate_with_execution_sources(completed_tree: Path) -> None:
    _ensure_case_for_authority(completed_tree)
    projection = fold_trace(completed_tree, CHANGE_ID, phase="reconciled")
    paths = [source.path for source in projection.sources]
    assert len(paths) == len(set(paths))


def test_malformed_summary_prevents_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from assurance_agent.evidence import trace as trace_mod
    from assurance_agent.evidence.layer_summary import TraceLayerSummaryError

    _seed_reconciled_inputs(tmp_path)

    def boom(projection):  # noqa: ANN001
        raise TraceLayerSummaryError("forced summary failure")

    monkeypatch.setattr(trace_mod, "summarize_projection_by_layer", boom)
    with pytest.raises(TraceLayerSummaryError):
        fold_trace(tmp_path, CHANGE_ID, phase="execution")


def test_current_loader_rejects_projection_after_problem_review(completed_tree: Path) -> None:
    from assurance_agent.evidence.current_projection import (
        CurrentProjectionStaleError,
        load_current_reconciled_projection,
    )

    materialize_reconciled_v2(completed_tree)
    mutate_project_problem_through_valid_review(completed_tree)
    with pytest.raises(CurrentProjectionStaleError) as raised:
        load_current_reconciled_projection(completed_tree, CHANGE_ID)
    assert raised.value.reason == "digest_mismatch"


def test_current_loader_success_and_closed_stale_reasons(completed_tree: Path) -> None:
    from assurance_agent.evidence.current_projection import (
        CurrentProjectionInvalidError,
        CurrentProjectionMissingError,
        CurrentProjectionStaleError,
        load_current_reconciled_projection,
    )
    from assurance_agent.evidence.digests import projection_digest

    path = materialize_reconciled_v2(completed_tree)
    loaded = load_current_reconciled_projection(completed_tree, CHANGE_ID)
    assert loaded.schema_version == "2"
    assert projection_digest(loaded) == projection_digest(
        fold_trace(completed_tree, CHANGE_ID, phase="reconciled")
    )

    path.unlink()
    with pytest.raises(CurrentProjectionMissingError):
        load_current_reconciled_projection(completed_tree, CHANGE_ID)

    materialize_reconciled_v2(completed_tree)
    path.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(CurrentProjectionInvalidError):
        load_current_reconciled_projection(completed_tree, CHANGE_ID)

    path.write_text("null", encoding="utf-8")
    with pytest.raises(CurrentProjectionInvalidError):
        load_current_reconciled_projection(completed_tree, CHANGE_ID)

    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CurrentProjectionInvalidError):
        load_current_reconciled_projection(completed_tree, CHANGE_ID)

    live = fold_trace(completed_tree, CHANGE_ID, phase="reconciled")
    v1 = live.model_dump(mode="json")
    v1["schema_version"] = "1"
    # Drop V2-only fields that V1 rejects if any; V1 accepts same core shape.
    path.write_text(json.dumps(v1), encoding="utf-8")
    with pytest.raises(CurrentProjectionStaleError) as legacy:
        load_current_reconciled_projection(completed_tree, CHANGE_ID)
    assert legacy.value.reason == "legacy_version"

    for field, reason in (
        ("phase", "phase_mismatch"),
        ("change_id", "change_id_mismatch"),
        ("authoritative_batch_id", "batch_id_mismatch"),
    ):
        payload = live.model_dump(mode="json")
        payload[field] = "execution" if field == "phase" else "WRONG"
        # Also inject an unrelated model defect that must lose to identity preflight.
        payload["integrity"] = "not-a-valid-integrity"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(CurrentProjectionStaleError) as raised:
            load_current_reconciled_projection(completed_tree, CHANGE_ID)
        assert raised.value.reason == reason


def test_current_loader_secret_only_edit_is_digest_stale(completed_tree: Path) -> None:
    from assurance_agent.evidence.current_projection import (
        CurrentProjectionStaleError,
        load_current_reconciled_projection,
    )
    from assurance_agent.evidence.digests import evidence_entry_digest_v1, projection_digest, raw_sha256
    from tests.unit.evidence import test_issue_replay_authority as auth

    path = materialize_reconciled_v2(completed_tree)
    before = fold_trace(completed_tree, CHANGE_ID, phase="reconciled")
    secret_path = auth.change_dir(completed_tree) / "execution/runs/secret.json"
    original = secret_path.read_bytes()
    altered = b'{"access_token":"ALTEREDTOKEN12"}'
    assert evidence_entry_digest_v1(original) == evidence_entry_digest_v1(altered)
    assert raw_sha256(original) != raw_sha256(altered)
    secret_path.write_bytes(altered)
    after = fold_trace(completed_tree, CHANGE_ID, phase="reconciled")
    assert projection_digest(before) != projection_digest(after)
    with pytest.raises(CurrentProjectionStaleError) as raised:
        load_current_reconciled_projection(completed_tree, CHANGE_ID)
    assert raised.value.reason == "digest_mismatch"
    assert path.is_file()
