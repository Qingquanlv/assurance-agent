"""Task 10: reconciled-phase `fold_trace` enrichment.

Covers the failure merge (D5: every ``FailureEntry`` for a case, in document
order), the two-hop Observation → Occurrence → Problem join with iterative
``merged_into`` alias following (A7), the open allowlist (D4), fingerprint
dedupe, the three reconciled sources, and the dual-temporal invariant — the
reconciled phase may only *add* facts, never change an execution-phase one.

Documents are written through the authoritative artifact models
(``FailureAnalysis``, ``ChangeIssueSnapshot``, ``ProblemProjection``) wherever
the case under test is a well-formed input, so a field rename upstream breaks
these tests instead of silently teaching the fold a stale schema. Raw JSON is
written only where the point of the test is a document the models would reject.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.inspect import (
    FailureAnalysis,
    FailureCategory,
    FailureEntry,
    FailureEvidence,
    FailureSeverity,
)
from assurance_agent.artifacts.models.issues import (
    ChangeIssueSnapshot,
    IssueAnalysisStatus,
    IssueClassification,
    IssueOccurrence,
    Observation,
    ObservationSource,
    OccurrenceAnalysis,
    Problem,
    ProblemAssessment,
    ProblemFingerprint,
    ProblemProjection,
    ProblemResolution,
    ProblemSeenRef,
    ProblemStatus,
    ProvisionalAssessment,
)
from assurance_agent.artifacts.models.trace import (
    TraceProblemFact,
    TraceProjection,
    TraceRow,
    TraceSource,
)
from assurance_agent.evidence.trace import (
    FAILURE_ANALYSIS_SOURCE,
    GAP_DETAIL_INVALID,
    GAP_DETAIL_MISSING,
    ISSUES_SNAPSHOT_SOURCE,
    PROJECT_PROBLEMS_SOURCE,
    fold_trace,
)

# The writer's own serializer, so the clean-batch fixture is byte-identical to
# what `collect_observations_operation` publishes.
from assurance_agent.workflow.issues.projection import dump_projection
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-TRACE-010"
BATCH_ID = "20260702-111111"
CASE_ID = "TC_API_001"
OTHER_CASE_ID = "TC_API_002"
DIGEST = "sha256:" + "a" * 64


# --------------------------------------------------------------------------- #
# execution-phase scaffolding (the facts the reconciled phase enriches)
# --------------------------------------------------------------------------- #


def _change_dir(project_root: Path) -> Path:
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    return change_dir


def _write_cases(change_dir: Path, case_ids: list[str]) -> None:
    document = {
        "schema_version": "1.0",
        "added": [
            {
                "case_id": case_id,
                "module": "system.api",
                "type": "API",
                "assertions": ["an assertion"],
                "automation": {"required": True},
            }
            for case_id in case_ids
        ],
        "modified": [],
        "removed": [],
    }
    path = change_dir / "cases" / "system" / "api" / "case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_mapped_tests(project_root: Path, case_ids: list[str]) -> dict[str, str]:
    source = "".join(
        f"def test_{case_id.lower()}__scenario() -> None:\n    assert True\n\n" for case_id in case_ids
    )
    path = project_root / "tests" / "api" / "test_x.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return {"tests/api/test_x.py": hashlib.sha256(source.encode("utf-8")).hexdigest()}


def _write_manifest(change_dir: Path, test_files: dict[str, str]) -> None:
    document = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "result_files": {"api": f"runs/{BATCH_ID}/api-result.json"},
        "test_files_sha256": test_files,
        "final_status": "FAIL",
    }
    path = change_dir / "execution" / "execution-manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_api_result(change_dir: Path, case_ids: list[str]) -> None:
    batch_dir = change_dir / "execution" / "runs" / BATCH_ID
    batch_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "target": "api",
        "cases": [
            {
                "case_id": case_id,
                "status": "failed",
                "file": "tests/api/test_x.py",
                "test_name": f"test_{case_id.lower()}__scenario",
            }
            for case_id in case_ids
        ],
        "unmapped_tests": [],
    }
    (batch_dir / "api-result.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


def _change(project_root: Path, case_ids: list[str] | None = None) -> Path:
    """A gap-free execution-phase change: cases, mapped tests, manifest, result."""
    cases = case_ids or [CASE_ID]
    change_dir = _change_dir(project_root)
    _write_cases(change_dir, cases)
    _write_manifest(change_dir, _write_mapped_tests(project_root, cases))
    _write_api_result(change_dir, cases)
    return change_dir


# --------------------------------------------------------------------------- #
# reconciled-phase input builders
# --------------------------------------------------------------------------- #


def _failure_entry(
    case_id: str,
    *,
    category: FailureCategory = "assertion_failure",
    severity: FailureSeverity = "high",
    target: str = "api",
) -> FailureEntry:
    return FailureEntry(
        case_id=case_id,
        target=target,  # type: ignore[arg-type]
        category=category,
        fix_proposal_eligible=True,
        severity=severity,
        evidence=FailureEvidence(
            result_file=f"execution/runs/{BATCH_ID}/api-result.json",
            test_file="tests/api/test_x.py",
            trace="",
            screenshot="",
            video="",
            raw_log="",
            log_excerpt="expected 200, got 500",
        ),
        diagnosis="assertion failed",
        recommended_action="fix the product",
    )


def _write_failure_analysis(
    change_dir: Path,
    entries: list[FailureEntry],
    *,
    change_id: str = CHANGE_ID,
    batch_id: str = BATCH_ID,
    source_batch_id: str | None = None,
) -> Path:
    analysis = FailureAnalysis(
        schema_version="1.0",
        change_id=change_id,
        source_manifest="execution/execution-manifest.yaml",
        inspection_status="completed",
        batch_id=batch_id,
        source_batch_id=source_batch_id if source_batch_id is not None else batch_id,
        final_status="FAIL",
        inspect_mode="primary",
        classification_performed=True,
        status="analyzed" if entries else "no_failures",
        failures=entries,
        hard_fails=[entry for entry in entries if not entry.fix_proposal_eligible],
        needs_review=[],
        known_product_issues=[entry for entry in entries if entry.category == "known_product_issue"],
    )
    return _write_json(change_dir / "inspect" / "failure-analysis.json", analysis.model_dump(mode="json"))


def _observation(observation_id: str, case_id: str | None, *, batch_id: str = BATCH_ID) -> Observation:
    return Observation(
        observation_id=observation_id,
        change_id=CHANGE_ID,
        batch_id=batch_id,
        kind="test_failure",
        target="api",
        case_id=case_id,
        source=ObservationSource(
            artifact=f"execution/runs/{BATCH_ID}/api-result.json",
            json_pointer="/cases/0",
        ),
        evidence_refs=[f"execution/runs/{BATCH_ID}/api-result.json"],
        signature="HTTP 500 from endpoint",
        observed_at="2026-07-02T11:11:11Z",
    )


def _occurrence(occurrence_id: str, observation_ids: list[str], problem_id: str) -> IssueOccurrence:
    return IssueOccurrence(
        occurrence_id=occurrence_id,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        observation_ids=observation_ids,
        problem_id=problem_id,
        provisional_assessment=ProvisionalAssessment(
            classification="product_bug",
            severity="high",
            authority="llm_provisional",
            root_cause_hypothesis="the endpoint returns 500",
        ),
        analysis=OccurrenceAnalysis(
            evidence_bundle_digest=DIGEST,
            analyzer="aa-inspector",
            prompt_version="1",
            candidate_digest=DIGEST,
        ),
    )


def _analysis_status(status: str = "completed", *, batch_id: str = BATCH_ID) -> IssueAnalysisStatus:
    return IssueAnalysisStatus(
        schema_version="1.0",
        change_id=CHANGE_ID,
        batch_id=batch_id,
        status=status,  # type: ignore[arg-type]
        evidence_bundle_digest=DIGEST,
        candidate_count=1,
        candidate_digest=DIGEST,
    )


def _write_snapshot(
    change_dir: Path,
    observations: list[Observation],
    occurrences: list[IssueOccurrence],
    *,
    change_id: str = CHANGE_ID,
    analysis_status: IssueAnalysisStatus | None = None,
    project_sync_status: str = "completed",
    batches: list[str] | None = None,
) -> Path:
    """A usable snapshot: analysis completed and the project ledger synced.

    Both are what makes the two-hop join answerable — an analysis that never
    completed has not produced the occurrences, and a pending project sync means
    the ledger this fold reads does not yet know about them.
    """
    snapshot = ChangeIssueSnapshot(
        schema_version="1.0",
        change_id=change_id,
        authoritative_batch_id=BATCH_ID,
        observations=observations,
        occurrences=occurrences,
        analysis_status=analysis_status if analysis_status is not None else _analysis_status(),
        project_sync_status=project_sync_status,  # type: ignore[arg-type]
        batches=batches if batches is not None else [BATCH_ID],
    )
    return _write_json(change_dir / "issues" / "snapshot.json", snapshot.model_dump(mode="json"))


def _problem(
    problem_id: str,
    *,
    status: ProblemStatus = "detected",
    classification: IssueClassification = "product_bug",
    fingerprint_digest: str | None = None,
    disposition: str | None = None,
) -> Problem:
    resolution = (
        ProblemResolution(
            resolved_at="2026-07-02T12:00:00Z",
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            disposition=disposition,
            verification_scope=[CASE_ID],
            evidence_digest=DIGEST,
        )
        if disposition is not None
        else None
    )
    return Problem(
        problem_id=problem_id,
        fingerprint=ProblemFingerprint(
            version="1",
            digest=fingerprint_digest or ("sha256:" + hashlib.sha256(problem_id.encode()).hexdigest()),
        ),
        title=f"{problem_id} title",
        assessment=ProblemAssessment(
            classification=classification,
            severity="high",
            authority="llm_provisional",
        ),
        status=status,
        first_seen=ProblemSeenRef(change_id=CHANGE_ID, occurrence_id="OCC-1"),
        last_seen=ProblemSeenRef(change_id=CHANGE_ID, occurrence_id="OCC-1"),
        occurrences=["OCC-1"],
        resolution=resolution,
        version=1,
    )


def _write_problems(project_root: Path, problems: list[Problem]) -> Path:
    projection = ProblemProjection(
        schema_version="1.0",
        generated_at="2026-07-02T12:00:00Z",
        problems=problems,
    )
    return _write_json(project_root / "qa" / "issues" / "problems.json", projection.model_dump(mode="json"))


def _write_json(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _mutate_json(path: Path, **overrides: object) -> Path:
    """Rewrite a model-built document with top-level keys replaced.

    Used for the documents the authoritative models refuse to build — an unknown
    ``project_sync_status``, a future ``schema_version`` — so the fixture stays
    a real document that differs only in the field under test.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(overrides)
    return _write_json(path, document)


def _joined(
    project_root: Path,
    change_dir: Path,
    *,
    observations: list[Observation] | None = None,
    occurrences: list[IssueOccurrence] | None = None,
    problems: list[Problem] | None = None,
) -> None:
    """Write a snapshot + ledger pair, defaulting to one case → one problem."""
    _write_snapshot(
        change_dir,
        observations if observations is not None else [_observation("OBS-1", CASE_ID)],
        occurrences if occurrences is not None else [_occurrence("OCC-1", ["OBS-1"], "PB-001")],
    )
    _write_problems(project_root, problems if problems is not None else [_problem("PB-001")])


# --------------------------------------------------------------------------- #
# assertions helpers
# --------------------------------------------------------------------------- #


def _reconciled(project_root: Path) -> TraceProjection:
    return fold_trace(project_root, CHANGE_ID, phase="reconciled")


def _row(projection: TraceProjection, case_id: str = CASE_ID) -> TraceRow:
    rows = [row for row in projection.rows if row.case_id == case_id]
    assert rows, f"no row for {case_id}"
    return rows[0]


def _gap_codes(projection: TraceProjection) -> list[str]:
    return [gap.code for gap in projection.gaps]


def _gaps(projection: TraceProjection, code: str) -> list[str]:
    return [gap.detail for gap in projection.gaps if gap.code == code]


def _source_paths(projection: TraceProjection) -> list[str]:
    return [source.path for source in projection.sources]


def _source(projection: TraceProjection, path: str) -> TraceSource:
    return next(source for source in projection.sources if source.path == path)


def _fact(projection: TraceProjection, problem_id: str, case_id: str = CASE_ID) -> TraceProblemFact:
    facts = [fact for fact in _row(projection, case_id).problem_facts if fact.problem_id == problem_id]
    assert facts, f"no problem fact for {problem_id} on {case_id}"
    return facts[0]


# --------------------------------------------------------------------------- #
# failure merge (D5)
# --------------------------------------------------------------------------- #


def test_every_failure_entry_for_a_case_is_kept_in_document_order(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _joined(tmp_path, change_dir)
    _write_failure_analysis(
        change_dir,
        [
            _failure_entry(CASE_ID, category="assertion_failure", severity="low"),
            _failure_entry(CASE_ID, category="locator_failure", severity="critical"),
            _failure_entry(CASE_ID, category="environment_failure", severity="medium"),
        ],
    )

    row = _row(_reconciled(tmp_path))

    assert [(failure.category, failure.severity) for failure in row.failures] == [
        ("assertion_failure", "low"),
        ("locator_failure", "critical"),
        ("environment_failure", "medium"),
    ]


def test_failures_are_scoped_to_their_own_case(tmp_path: Path) -> None:
    change_dir = _change(tmp_path, [CASE_ID, OTHER_CASE_ID])
    _joined(tmp_path, change_dir)
    _write_failure_analysis(
        change_dir,
        [
            _failure_entry(OTHER_CASE_ID, category="business_logic_failure"),
            _failure_entry(CASE_ID, category="assertion_failure"),
            _failure_entry("test_orphan_function", category="test_code_error"),
        ],
    )

    projection = _reconciled(tmp_path)

    assert [failure.category for failure in _row(projection).failures] == ["assertion_failure"]
    assert [failure.category for failure in _row(projection, OTHER_CASE_ID).failures] == [
        "business_logic_failure"
    ]


def test_a_case_without_failures_keeps_an_empty_tuple(tmp_path: Path) -> None:
    change_dir = _change(tmp_path, [CASE_ID, OTHER_CASE_ID])
    _joined(tmp_path, change_dir)
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])

    assert _row(_reconciled(tmp_path), OTHER_CASE_ID).failures == ()


def test_failure_entries_from_any_target_are_preserved(tmp_path: Path) -> None:
    """The row carries the failure facts, not a target-filtered view of them."""
    change_dir = _change(tmp_path)
    _joined(tmp_path, change_dir)
    _write_failure_analysis(
        change_dir,
        [
            _failure_entry(CASE_ID, target="coverage", category="coverage_gap"),
            _failure_entry(CASE_ID, target="api", category="assertion_failure"),
        ],
    )

    assert [failure.category for failure in _row(_reconciled(tmp_path)).failures] == [
        "coverage_gap",
        "assertion_failure",
    ]


def test_an_unknown_failure_category_is_carried_verbatim(tmp_path: Path) -> None:
    """``TraceFailure.category`` is a free string, so a new upstream category is
    a fact to record, not a corrupt document."""
    change_dir = _change(tmp_path)
    _joined(tmp_path, change_dir)
    _write_json(
        change_dir / "inspect" / "failure-analysis.json",
        {
            "schema_version": "1.0",
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "source_batch_id": BATCH_ID,
            "failures": [
                {"case_id": CASE_ID, "target": "api", "category": "brand_new_category", "severity": "spicy"}
            ],
        },
    )

    projection = _reconciled(tmp_path)

    assert "failure_analysis_missing" not in _gap_codes(projection)
    assert [(f.category, f.severity) for f in _row(projection).failures] == [("brand_new_category", "spicy")]


def test_missing_failure_analysis_produces_a_typed_gap_and_an_absent_source(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _joined(tmp_path, change_dir)

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["failure_analysis_missing"]
    source = next(s for s in projection.sources if s.path == FAILURE_ANALYSIS_SOURCE)
    assert (source.exists, source.sha256) == (False, None)
    assert _row(projection).failures == ()


def test_corrupt_failure_analysis_is_a_typed_gap_not_an_exception(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _joined(tmp_path, change_dir)
    path = change_dir / "inspect" / "failure-analysis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["failure_analysis_missing"]
    source = next(s for s in projection.sources if s.path == FAILURE_ANALYSIS_SOURCE)
    assert source.exists is True
    assert source.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert _row(projection).failures == ()


def test_failure_analysis_row_missing_a_case_id_is_a_typed_gap(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _joined(tmp_path, change_dir)
    _write_json(
        change_dir / "inspect" / "failure-analysis.json",
        {
            "schema_version": "1.0",
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "source_batch_id": BATCH_ID,
            "failures": [{"target": "api", "category": "assertion_failure", "severity": "high"}],
        },
    )

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["failure_analysis_missing"]
    assert "case_id" in _gaps(projection, "failure_analysis_missing")[0]


def test_failure_analysis_for_another_change_is_rejected_fail_closed(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _joined(tmp_path, change_dir)
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)], change_id="CH-OTHER-999")

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["failure_analysis_missing"]
    assert "CH-OTHER-999" in _gaps(projection, "failure_analysis_missing")[0]
    assert _row(projection).failures == ()


def test_failure_analysis_from_another_batch_is_rejected_fail_closed(tmp_path: Path) -> None:
    """The archive gate already refuses a stale analysis
    (``failure_analysis.source_batch_id == execution.batch_id``); folding one as
    the current batch's failures would contradict it."""
    change_dir = _change(tmp_path)
    _joined(tmp_path, change_dir)
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)], batch_id="20260601-090000")

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["failure_analysis_missing"]
    detail = _gaps(projection, "failure_analysis_missing")[0]
    assert detail.startswith(GAP_DETAIL_INVALID)
    assert "20260601-090000" in detail and BATCH_ID in detail
    assert _row(projection).failures == ()


def test_failure_analysis_inspecting_another_batch_is_rejected_fail_closed(tmp_path: Path) -> None:
    """``source_batch_id`` names the batch whose results were classified, so a
    document that inspected an older batch is stale even when its own
    ``batch_id`` is current."""
    change_dir = _change(tmp_path)
    _joined(tmp_path, change_dir)
    _write_failure_analysis(
        change_dir, [_failure_entry(CASE_ID)], batch_id=BATCH_ID, source_batch_id="20260601-090000"
    )

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["failure_analysis_missing"]
    detail = _gaps(projection, "failure_analysis_missing")[0]
    assert detail.startswith(GAP_DETAIL_INVALID)
    assert "source_batch_id" in detail
    assert _row(projection).failures == ()


def test_failure_analysis_cannot_be_attributed_without_an_authoritative_batch(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    (change_dir / "execution" / "execution-manifest.yaml").unlink()
    _joined(tmp_path, change_dir)
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["manifest_missing", "failure_analysis_missing"]
    detail = _gaps(projection, "failure_analysis_missing")[0]
    assert detail.startswith(GAP_DETAIL_INVALID)
    assert "authoritative batch" in detail
    assert _row(projection).failures == ()


# --------------------------------------------------------------------------- #
# degraded issue snapshots fail closed
# --------------------------------------------------------------------------- #


def test_the_clean_batch_snapshot_is_valid_empty_evidence(tmp_path: Path) -> None:
    """The shape ``collect_observations_operation`` writes when nothing went wrong.

    A clean batch has no abnormal observations, so no Issue analysis runs and
    ``analysis_status`` stays null (``workflow/issues/operations.py``). That is
    not a degraded snapshot — it is the answer "this change has no problems" —
    and folding it as unusable would deny every green change a complete
    reconciled projection.
    """
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    clean = ChangeIssueSnapshot(
        schema_version="1.0",
        change_id=CHANGE_ID,
        authoritative_batch_id=BATCH_ID,
        observations=[],
        occurrences=[],
        analysis_status=None,
        project_sync_status="completed",
        batches=[BATCH_ID],
    )
    snapshot_path = change_dir / "issues" / "snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(dump_projection(clean))
    _write_problems(tmp_path, [])

    projection = _reconciled(tmp_path)

    assert projection.gaps == ()
    assert projection.integrity == "complete"
    row = _row(projection)
    assert row.problem_facts == ()
    assert row.open_problem_ids == ()
    assert _source(projection, ISSUES_SNAPSHOT_SOURCE).exists is True


def test_absent_analysis_status_with_collected_observations_fails_closed(tmp_path: Path) -> None:
    """The other half of the rule: observations exist, so an analysis was owed.

    A null ``analysis_status`` here means the analysis never ran or never
    finished, so the fold cannot tell whether the problems those observations
    point at are open.
    """
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(tmp_path, change_dir)
    _mutate_json(change_dir / "issues" / "snapshot.json", analysis_status=None)

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["issues_snapshot_missing"]
    detail = _gaps(projection, "issues_snapshot_missing")[0]
    assert detail.startswith(GAP_DETAIL_INVALID)
    assert "analysis_status" in detail
    row = _row(projection)
    assert row.open_problem_ids == ()
    assert row.problem_facts == ()


@pytest.mark.parametrize(
    ("overrides", "expected_field"),
    [
        ({"project_sync_status": "pending"}, "project_sync_status"),
        ({"project_sync_status": "in_flight"}, "project_sync_status"),
    ],
    ids=["sync_pending", "sync_unknown"],
)
def test_a_degraded_snapshot_is_unusable_and_suppresses_the_join(
    tmp_path: Path, overrides: dict[str, object], expected_field: str
) -> None:
    """A snapshot that says its own analysis or project sync is incomplete cannot
    answer "is this case's problem still open" (the semantics
    ``report_builder`` already reads as ``unknown`` risk)."""
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])
    _joined(tmp_path, change_dir)
    snapshot_path = _mutate_json(change_dir / "issues" / "snapshot.json", **overrides)

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["issues_snapshot_missing"]
    detail = _gaps(projection, "issues_snapshot_missing")[0]
    assert detail.startswith(GAP_DETAIL_INVALID)
    assert expected_field in detail
    row = _row(projection)
    assert row.open_problem_ids == ()
    assert row.problem_facts == ()
    assert len(row.failures) == 1, "an unusable snapshot must not cost the failure facts"
    source = _source(projection, ISSUES_SNAPSHOT_SOURCE)
    assert source.exists is True
    assert source.sha256 == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_an_uncompleted_analysis_status_makes_the_snapshot_unusable(tmp_path: Path, status: str) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(
        change_dir,
        [_observation("OBS-1", CASE_ID)],
        [_occurrence("OCC-1", ["OBS-1"], "PB-001")],
        analysis_status=_analysis_status(status),
    )
    _write_problems(tmp_path, [_problem("PB-001")])

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["issues_snapshot_missing"]
    detail = _gaps(projection, "issues_snapshot_missing")[0]
    assert detail.startswith(GAP_DETAIL_INVALID)
    assert status in detail
    assert _row(projection).open_problem_ids == ()


def test_one_degraded_snapshot_produces_exactly_one_gap(tmp_path: Path) -> None:
    """Two cases, several occurrences: still one gap about the input."""
    change_dir = _change(tmp_path, [CASE_ID, OTHER_CASE_ID])
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", CASE_ID), _observation("OBS-2", OTHER_CASE_ID)],
        occurrences=[
            _occurrence("OCC-1", ["OBS-1"], "PB-001"),
            _occurrence("OCC-2", ["OBS-2"], "PB-002"),
        ],
        problems=[_problem("PB-001"), _problem("PB-002")],
    )
    _mutate_json(change_dir / "issues" / "snapshot.json", project_sync_status="pending")

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["issues_snapshot_missing"]
    assert all(row.open_problem_ids == () for row in projection.rows)


@pytest.mark.parametrize(
    "overrides",
    [
        {"analysis_status": {"schema_version": "1.0", "status": "failed"}},
        {"project_sync_status": "pending"},
    ],
    ids=["explicit_failed_analysis", "sync_pending"],
)
def test_an_empty_snapshot_that_declares_itself_degraded_is_still_unusable(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    """Emptiness excuses only a *null* analysis_status.

    A snapshot that explicitly reports a failed analysis, or a project sync that
    has not landed, is describing a broken pipeline — the empty observation list
    is then a symptom, not a clean batch.
    """
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(change_dir, [], [])
    _write_problems(tmp_path, [])
    _mutate_json(change_dir / "issues" / "snapshot.json", **overrides)

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["issues_snapshot_missing"]
    assert _gaps(projection, "issues_snapshot_missing")[0].startswith(GAP_DETAIL_INVALID)


# --------------------------------------------------------------------------- #
# an analysis that predates the observations it is supposed to cover
#
# `ChangeIssueSnapshot` is folded from the change's issue event log, so
# observations accumulate across batches while `analysis_status` describes only
# the most recent analysis (`workflow/issues/projection.py`). An observation
# collected *after* that analysis therefore has no occurrence and no Problem —
# the analysis ran before it existed — and a snapshot reporting `completed` says
# nothing about it. Folding that as "this case has no open problem" is a
# fail-open the reconciled join must refuse.
# --------------------------------------------------------------------------- #

NEWER_BATCH_ID = "20260702-222222"
OLDER_BATCH_ID = "20260701-090909"


def test_an_observation_newer_than_the_completed_analysis_is_unanalyzed(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])
    _write_snapshot(
        change_dir,
        [_observation("OBS-1", CASE_ID, batch_id=NEWER_BATCH_ID)],
        [],
        analysis_status=_analysis_status(batch_id=BATCH_ID),
        batches=[BATCH_ID, NEWER_BATCH_ID],
    )
    _write_problems(tmp_path, [])

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["issues_snapshot_missing"]
    detail = _gaps(projection, "issues_snapshot_missing")[0]
    assert detail.startswith(GAP_DETAIL_INVALID)
    assert NEWER_BATCH_ID in detail
    assert BATCH_ID in detail
    row = _row(projection)
    assert row.open_problem_ids == ()
    assert row.problem_facts == ()
    assert len(row.failures) == 1, "a stale analysis must not cost the failure facts"


def test_a_stale_analysis_suppresses_the_join_even_when_occurrences_exist(tmp_path: Path) -> None:
    """The occurrences on record belong to the analysed batch, not the newer one.

    Believing them would report the *old* batch's open problems as this batch's
    verdict, which is precisely the mismatch the batch facts expose.
    """
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(
        change_dir,
        [
            _observation("OBS-1", CASE_ID, batch_id=BATCH_ID),
            _observation("OBS-2", CASE_ID, batch_id=NEWER_BATCH_ID),
        ],
        [_occurrence("OCC-1", ["OBS-1"], "PB-001")],
        analysis_status=_analysis_status(batch_id=BATCH_ID),
        batches=[BATCH_ID, NEWER_BATCH_ID],
    )
    _write_problems(tmp_path, [_problem("PB-001")])

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["issues_snapshot_missing"]
    assert _row(projection).open_problem_ids == ()


def test_an_observation_from_an_earlier_analysed_batch_stays_usable(tmp_path: Path) -> None:
    """Older observations were analysed in their own batch, and their occurrences
    are in the snapshot — only observations the analysis could not have seen are
    unanalyzed."""
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(
        change_dir,
        [_observation("OBS-1", CASE_ID, batch_id=OLDER_BATCH_ID)],
        [_occurrence("OCC-1", ["OBS-1"], "PB-001")],
        analysis_status=_analysis_status(batch_id=BATCH_ID),
        batches=[OLDER_BATCH_ID, BATCH_ID],
    )
    _write_problems(tmp_path, [_problem("PB-001")])

    projection = _reconciled(tmp_path)

    assert projection.gaps == ()
    assert _row(projection).open_problem_ids == ("PB-001",)


def test_an_observation_from_the_analysed_batch_stays_usable(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(tmp_path, change_dir)

    projection = _reconciled(tmp_path)

    assert projection.gaps == ()
    assert _row(projection).open_problem_ids == ("PB-001",)


@pytest.mark.parametrize(
    ("observation_batch", "analysis_batch"),
    [
        ("not-a-batch-id", BATCH_ID),
        (BATCH_ID, "not-a-batch-id"),
    ],
    ids=["unorderable_observation", "unorderable_analysis"],
)
def test_batch_ids_that_cannot_be_ordered_fail_closed(
    tmp_path: Path, observation_batch: str, analysis_batch: str
) -> None:
    """Fail closed when "newer" cannot be decided.

    Ordering is the batch-id timestamp format; two ids that do not both parse
    cannot be compared, so any difference between them is treated as unanalyzed
    rather than guessed in the direction that passes.
    """
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(
        change_dir,
        [_observation("OBS-1", CASE_ID, batch_id=observation_batch)],
        [_occurrence("OCC-1", ["OBS-1"], "PB-001")],
        analysis_status=_analysis_status(batch_id=analysis_batch),
        batches=[observation_batch, analysis_batch],
    )
    _write_problems(tmp_path, [_problem("PB-001")])

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["issues_snapshot_missing"]
    assert _row(projection).open_problem_ids == ()


def test_a_clean_empty_snapshot_is_unaffected_by_the_staleness_rule(tmp_path: Path) -> None:
    """No observations means nothing can be newer than an analysis that never ran."""
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(change_dir, [], [], analysis_status=None)
    _write_problems(tmp_path, [])

    projection = _reconciled(tmp_path)

    assert projection.gaps == ()
    assert projection.integrity == "complete"


def test_a_completed_and_synced_snapshot_still_joins(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(tmp_path, change_dir)

    projection = _reconciled(tmp_path)

    assert projection.gaps == ()
    assert _row(projection).open_problem_ids == ("PB-001",)


# --------------------------------------------------------------------------- #
# missing vs present-but-invalid, and pinned schema versions
# --------------------------------------------------------------------------- #


def _all_reconciled_inputs(project_root: Path, change_dir: Path) -> dict[str, Path]:
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])
    _joined(project_root, change_dir)
    return {
        FAILURE_ANALYSIS_SOURCE: change_dir / "inspect" / "failure-analysis.json",
        ISSUES_SNAPSHOT_SOURCE: change_dir / "issues" / "snapshot.json",
        PROJECT_PROBLEMS_SOURCE: project_root / "qa" / "issues" / "problems.json",
    }


_INPUT_CODES = {
    FAILURE_ANALYSIS_SOURCE: "failure_analysis_missing",
    ISSUES_SNAPSHOT_SOURCE: "issues_snapshot_missing",
    PROJECT_PROBLEMS_SOURCE: "problems_snapshot_missing",
}


@pytest.mark.parametrize("logical", list(_INPUT_CODES))
def test_an_absent_input_is_detailed_as_missing(tmp_path: Path, logical: str) -> None:
    change_dir = _change(tmp_path)
    paths = _all_reconciled_inputs(tmp_path, change_dir)
    paths[logical].unlink()

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == [_INPUT_CODES[logical]]
    detail = _gaps(projection, _INPUT_CODES[logical])[0]
    assert detail.startswith(GAP_DETAIL_MISSING)
    assert not detail.startswith(GAP_DETAIL_INVALID)
    source = _source(projection, logical)
    assert (source.exists, source.sha256) == (False, None)


@pytest.mark.parametrize("logical", list(_INPUT_CODES))
def test_a_present_but_unreadable_input_is_detailed_as_invalid(tmp_path: Path, logical: str) -> None:
    change_dir = _change(tmp_path)
    paths = _all_reconciled_inputs(tmp_path, change_dir)
    paths[logical].write_text("{not json", encoding="utf-8")

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == [_INPUT_CODES[logical]]
    assert _gaps(projection, _INPUT_CODES[logical])[0].startswith(GAP_DETAIL_INVALID)
    source = _source(projection, logical)
    assert source.exists is True
    assert source.sha256 == hashlib.sha256(paths[logical].read_bytes()).hexdigest()


@pytest.mark.parametrize("logical", list(_INPUT_CODES))
@pytest.mark.parametrize("schema_version", ["2.0", "1", None])
def test_an_unpinned_schema_version_is_an_invalid_input(
    tmp_path: Path, logical: str, schema_version: str | None
) -> None:
    """All three documents pin ``schema_version: "1.0"`` in their own models, so
    a future or absent version is a document this fold must not guess at."""
    change_dir = _change(tmp_path)
    paths = _all_reconciled_inputs(tmp_path, change_dir)
    _mutate_json(paths[logical], schema_version=schema_version)

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == [_INPUT_CODES[logical]]
    assert _gaps(projection, _INPUT_CODES[logical])[0].startswith(GAP_DETAIL_INVALID)
    assert _source(projection, logical).exists is True
    row = _row(projection)
    if logical == FAILURE_ANALYSIS_SOURCE:
        assert row.failures == ()
    else:
        assert row.open_problem_ids == ()


@pytest.mark.parametrize(
    "disposition",
    ["merged_into:PB-404", "merged_into:PB-002"],
    ids=["absent_target", "cycle"],
)
def test_alias_gap_details_do_not_claim_the_ledger_itself_is_unusable(
    tmp_path: Path, disposition: str
) -> None:
    """The source prefixes describe the *document*; alias gaps describe a
    reference inside a document this fold read successfully, so they carry
    neither prefix and must not be mistaken for an absent or refused ledger."""
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[
            _problem("PB-001", status="resolved", disposition=disposition),
            _problem("PB-002", status="resolved", disposition="merged_into:PB-001"),
        ],
    )

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["problem_alias_invalid"]
    detail = _gaps(projection, "problem_alias_invalid")[0]
    assert not detail.startswith(GAP_DETAIL_MISSING)
    assert not detail.startswith(GAP_DETAIL_INVALID)
    assert _source(projection, PROJECT_PROBLEMS_SOURCE).exists is True


def test_the_expected_schema_version_of_every_input_is_accepted(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _all_reconciled_inputs(tmp_path, change_dir)

    projection = _reconciled(tmp_path)

    assert projection.gaps == ()
    assert projection.integrity == "complete"


# --------------------------------------------------------------------------- #
# two-hop problem join (§9) and the open allowlist (D4)
# --------------------------------------------------------------------------- #


def test_two_hop_join_reaches_the_problem_through_the_occurrence(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(tmp_path, change_dir)

    projection = _reconciled(tmp_path)

    assert _row(projection).open_problem_ids == ("PB-001",)
    assert projection.gaps == ()


@pytest.mark.parametrize(
    "status",
    [
        "detected",
        "triaged",
        "in_progress",
        "verification_pending",
        "resolved",
        "not_an_issue",
        "accepted_risk",
    ],
)
@pytest.mark.parametrize("classification", ["product_bug", "test_bug", "environment_issue", "unknown"])
def test_open_requires_a_live_status_and_the_product_bug_classification(
    tmp_path: Path, status: str, classification: str
) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[
            _problem(
                "PB-001",
                status=status,  # type: ignore[arg-type]
                classification=classification,  # type: ignore[arg-type]
                disposition="fixed" if status == "resolved" else None,
            )
        ],
    )

    open_expected = status not in {"resolved", "not_an_issue", "accepted_risk"} and (
        classification == "product_bug"
    )
    assert (_row(_reconciled(tmp_path)).open_problem_ids == ("PB-001",)) is open_expected


def test_an_observation_without_a_case_id_is_never_joined(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", None)],
        occurrences=[_occurrence("OCC-1", ["OBS-1"], "PB-001")],
    )

    assert _row(_reconciled(tmp_path)).open_problem_ids == ()


def test_an_occurrence_observation_that_the_snapshot_does_not_list_is_not_joined(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", CASE_ID)],
        occurrences=[_occurrence("OCC-1", ["OBS-DANGLING"], "PB-001")],
    )

    assert _row(_reconciled(tmp_path)).open_problem_ids == ()


def test_several_occurrences_for_one_case_collect_every_open_problem_sorted(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", CASE_ID), _observation("OBS-2", CASE_ID)],
        occurrences=[
            _occurrence("OCC-2", ["OBS-2"], "PB-009"),
            _occurrence("OCC-1", ["OBS-1"], "PB-002"),
        ],
        problems=[_problem("PB-009"), _problem("PB-002")],
    )

    assert _row(_reconciled(tmp_path)).open_problem_ids == ("PB-002", "PB-009")


def test_the_same_problem_reached_twice_is_reported_once(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", CASE_ID), _observation("OBS-2", CASE_ID)],
        occurrences=[
            _occurrence("OCC-1", ["OBS-1"], "PB-001"),
            _occurrence("OCC-2", ["OBS-2"], "PB-001"),
        ],
    )

    assert _row(_reconciled(tmp_path)).open_problem_ids == ("PB-001",)


def test_one_occurrence_spanning_two_cases_joins_both(tmp_path: Path) -> None:
    change_dir = _change(tmp_path, [CASE_ID, OTHER_CASE_ID])
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", CASE_ID), _observation("OBS-2", OTHER_CASE_ID)],
        occurrences=[_occurrence("OCC-1", ["OBS-1", "OBS-2"], "PB-001")],
    )

    projection = _reconciled(tmp_path)

    assert _row(projection).open_problem_ids == ("PB-001",)
    assert _row(projection, OTHER_CASE_ID).open_problem_ids == ("PB-001",)


@pytest.mark.parametrize(
    ("first_case", "second_case", "winner"),
    [(CASE_ID, OTHER_CASE_ID, CASE_ID), (OTHER_CASE_ID, CASE_ID, OTHER_CASE_ID)],
    ids=["first_declaration_wins", "reversed_declaration_wins"],
)
def test_a_duplicate_observation_id_resolves_to_its_first_declaration(
    tmp_path: Path, first_case: str, second_case: str, winner: str
) -> None:
    """Two rows claiming one observation_id is a contradictory snapshot; the fold
    must not let list order decide silently, so the first declaration wins the
    same way duplicate case declarations do."""
    change_dir = _change(tmp_path, [CASE_ID, OTHER_CASE_ID])
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", first_case), _observation("OBS-1", second_case)],
        occurrences=[_occurrence("OCC-1", ["OBS-1"], "PB-001")],
    )

    projection = _reconciled(tmp_path)

    loser = OTHER_CASE_ID if winner == CASE_ID else CASE_ID
    assert _row(projection, winner).open_problem_ids == ("PB-001",)
    assert _row(projection, loser).open_problem_ids == ()


def test_a_duplicate_observation_id_whose_first_declaration_has_no_case_does_not_join(
    tmp_path: Path,
) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", None), _observation("OBS-1", CASE_ID)],
        occurrences=[_occurrence("OCC-1", ["OBS-1"], "PB-001")],
    )

    assert _row(_reconciled(tmp_path)).open_problem_ids == ()


def test_two_open_problems_sharing_a_fingerprint_are_deduplicated(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    shared = "sha256:" + "b" * 64
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", CASE_ID), _observation("OBS-2", CASE_ID)],
        occurrences=[
            _occurrence("OCC-1", ["OBS-1"], "PB-002"),
            _occurrence("OCC-2", ["OBS-2"], "PB-001"),
        ],
        problems=[
            _problem("PB-001", fingerprint_digest=shared),
            _problem("PB-002", fingerprint_digest=shared),
        ],
    )

    assert _row(_reconciled(tmp_path)).open_problem_ids == ("PB-001",)


# --------------------------------------------------------------------------- #
# merge alias following (A7)
# --------------------------------------------------------------------------- #


def test_a_merged_source_is_replaced_by_its_canonical_target(tmp_path: Path) -> None:
    """The source Problem is ``resolved`` by construction; that must not be read
    as "closed" when the canonical Problem it merged into is still open."""
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[
            _problem("PB-001", status="resolved", disposition="merged_into:PB-100"),
            _problem("PB-100", status="triaged"),
        ],
    )

    projection = _reconciled(tmp_path)

    assert _row(projection).open_problem_ids == ("PB-100",)
    assert projection.gaps == ()


def test_an_alias_chain_is_followed_to_the_end(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[
            _problem("PB-001", status="resolved", disposition="merged_into:PB-002"),
            _problem("PB-002", status="resolved", disposition="merged_into:PB-003"),
            _problem("PB-003", status="in_progress"),
        ],
    )

    assert _row(_reconciled(tmp_path)).open_problem_ids == ("PB-003",)


def test_a_canonical_target_that_is_closed_reports_no_open_problem(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[
            _problem("PB-001", status="resolved", disposition="merged_into:PB-100"),
            _problem("PB-100", status="resolved", disposition="fixed in this change"),
        ],
    )

    projection = _reconciled(tmp_path)

    assert _row(projection).open_problem_ids == ()
    assert projection.gaps == ()


def test_a_canonical_target_classified_as_a_test_bug_reports_no_open_problem(tmp_path: Path) -> None:
    """The allowlist is evaluated on the canonical Problem, not on the source."""
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[
            _problem("PB-001", status="resolved", disposition="merged_into:PB-100"),
            _problem("PB-100", status="triaged", classification="test_bug"),
        ],
    )

    assert _row(_reconciled(tmp_path)).open_problem_ids == ()


@pytest.mark.parametrize(
    "disposition",
    ["fixed by the change", "merged into PB-100", "merged_into PB-100", "MERGED_INTO:PB-100"],
)
def test_only_the_exact_merged_into_disposition_is_an_alias(tmp_path: Path, disposition: str) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[
            _problem("PB-001", status="resolved", disposition=disposition),
            _problem("PB-100", status="triaged"),
        ],
    )

    projection = _reconciled(tmp_path)

    assert _row(projection).open_problem_ids == ()
    assert projection.gaps == ()


def test_an_alias_cycle_produces_one_gap_and_no_open_problem(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[
            _problem("PB-001", status="resolved", disposition="merged_into:PB-002"),
            _problem("PB-002", status="resolved", disposition="merged_into:PB-001"),
        ],
    )

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["problem_alias_invalid"]
    assert projection.gaps[0].source == PROJECT_PROBLEMS_SOURCE
    assert _row(projection).open_problem_ids == ()


def test_an_alias_cycle_reached_from_two_references_still_gaps_once(tmp_path: Path) -> None:
    change_dir = _change(tmp_path, [CASE_ID, OTHER_CASE_ID])
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", CASE_ID), _observation("OBS-2", OTHER_CASE_ID)],
        occurrences=[
            _occurrence("OCC-1", ["OBS-1"], "PB-001"),
            _occurrence("OCC-2", ["OBS-2"], "PB-002"),
        ],
        problems=[
            _problem("PB-001", status="resolved", disposition="merged_into:PB-002"),
            _problem("PB-002", status="resolved", disposition="merged_into:PB-001"),
        ],
    )

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["problem_alias_invalid"]
    assert _row(projection).open_problem_ids == ()
    assert _row(projection, OTHER_CASE_ID).open_problem_ids == ()


def test_a_self_merge_is_a_cycle(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[_problem("PB-001", status="resolved", disposition="merged_into:PB-001")],
    )

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["problem_alias_invalid"]
    assert _row(projection).open_problem_ids == ()


@pytest.mark.parametrize("disposition", ["merged_into:PB-404", "merged_into:"])
def test_an_alias_target_that_does_not_exist_produces_one_gap(tmp_path: Path, disposition: str) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[_problem("PB-001", status="resolved", disposition=disposition)],
    )

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["problem_alias_invalid"]
    assert _row(projection).open_problem_ids == ()


def test_an_occurrence_referencing_an_unknown_problem_gaps_and_reports_nothing_open(
    tmp_path: Path,
) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(tmp_path, change_dir, problems=[_problem("PB-999")])

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["problem_alias_invalid"]
    assert "PB-001" in projection.gaps[0].detail
    assert _row(projection).open_problem_ids == ()


def test_a_broken_reference_does_not_hide_the_other_open_problems(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", CASE_ID), _observation("OBS-2", CASE_ID)],
        occurrences=[
            _occurrence("OCC-1", ["OBS-1"], "PB-404"),
            _occurrence("OCC-2", ["OBS-2"], "PB-001"),
        ],
    )

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["problem_alias_invalid"]
    assert _row(projection).open_problem_ids == ("PB-001",)


# --------------------------------------------------------------------------- #
# problem facts (§9.4 / §9.5 recorded, not filtered)
# --------------------------------------------------------------------------- #


def test_a_problem_fact_records_the_canonical_identity_status_and_verdict(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    fingerprint = "sha256:" + "c" * 64
    _joined(
        tmp_path,
        change_dir,
        problems=[_problem("PB-001", status="triaged", fingerprint_digest=fingerprint)],
    )

    fact = _fact(_reconciled(tmp_path), "PB-001")

    assert fact.source_problem_ids == ("PB-001",)
    assert fact.fingerprint == fingerprint
    assert (fact.status, fact.classification) == ("triaged", "product_bug")
    assert fact.open_product_bug is True


@pytest.mark.parametrize("status", ["resolved", "not_an_issue", "accepted_risk"])
def test_a_closed_problem_is_recorded_as_a_fact_but_never_routed(tmp_path: Path, status: str) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[
            _problem(
                "PB-001",
                status=status,  # type: ignore[arg-type]
                disposition="fixed" if status == "resolved" else None,
            )
        ],
    )

    projection = _reconciled(tmp_path)

    fact = _fact(projection, "PB-001")
    assert (fact.status, fact.open_product_bug) == (status, False)
    assert _row(projection).open_problem_ids == ()


@pytest.mark.parametrize("classification", ["test_bug", "environment_issue", "unknown"])
def test_a_non_product_problem_is_recorded_as_a_fact_but_never_routed(
    tmp_path: Path, classification: str
) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[_problem("PB-001", classification=classification)],  # type: ignore[arg-type]
    )

    projection = _reconciled(tmp_path)

    fact = _fact(projection, "PB-001")
    assert (fact.classification, fact.open_product_bug) == (classification, False)
    assert _row(projection).open_problem_ids == ()


def test_a_fact_keeps_every_source_alias_that_resolved_to_it(tmp_path: Path) -> None:
    """The canonical row is one fact, but the ids that reached it are the trail
    back to the occurrences, so none of them may be dropped."""
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[
            _observation("OBS-1", CASE_ID),
            _observation("OBS-2", CASE_ID),
            _observation("OBS-3", CASE_ID),
        ],
        occurrences=[
            _occurrence("OCC-1", ["OBS-1"], "PB-002"),
            _occurrence("OCC-2", ["OBS-2"], "PB-001"),
            _occurrence("OCC-3", ["OBS-3"], "PB-100"),
        ],
        problems=[
            _problem("PB-001", status="resolved", disposition="merged_into:PB-100"),
            _problem("PB-002", status="resolved", disposition="merged_into:PB-001"),
            _problem("PB-100", status="triaged"),
        ],
    )

    projection = _reconciled(tmp_path)
    row = _row(projection)

    assert [fact.problem_id for fact in row.problem_facts] == ["PB-100"]
    assert _fact(projection, "PB-100").source_problem_ids == ("PB-001", "PB-002", "PB-100")
    assert row.open_problem_ids == ("PB-100",)


def test_a_fingerprint_deduped_problem_is_still_recorded_as_a_fact(tmp_path: Path) -> None:
    """Dedupe is a per-row *routing* rule; the second problem and its aliases
    stay visible as facts so nothing is silently lost."""
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    shared = "sha256:" + "b" * 64
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", CASE_ID), _observation("OBS-2", CASE_ID)],
        occurrences=[
            _occurrence("OCC-1", ["OBS-1"], "PB-002"),
            _occurrence("OCC-2", ["OBS-2"], "PB-001"),
        ],
        problems=[
            _problem("PB-001", fingerprint_digest=shared),
            _problem("PB-002", fingerprint_digest=shared),
        ],
    )

    projection = _reconciled(tmp_path)
    row = _row(projection)

    assert [fact.problem_id for fact in row.problem_facts] == ["PB-001", "PB-002"]
    assert all(fact.open_product_bug for fact in row.problem_facts)
    assert all(fact.fingerprint == shared for fact in row.problem_facts)
    assert row.open_problem_ids == ("PB-001",)


def test_problem_facts_are_ordered_by_canonical_problem_id(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        observations=[
            _observation("OBS-1", CASE_ID),
            _observation("OBS-2", CASE_ID),
            _observation("OBS-3", CASE_ID),
        ],
        occurrences=[
            _occurrence("OCC-1", ["OBS-1"], "PB-300"),
            _occurrence("OCC-2", ["OBS-2"], "PB-100"),
            _occurrence("OCC-3", ["OBS-3"], "PB-200"),
        ],
        problems=[
            _problem("PB-300"),
            _problem("PB-100", status="resolved", disposition="fixed"),
            _problem("PB-200", classification="test_bug"),
        ],
    )

    row = _row(_reconciled(tmp_path))

    assert [fact.problem_id for fact in row.problem_facts] == ["PB-100", "PB-200", "PB-300"]
    assert row.open_problem_ids == ("PB-300",)


def test_an_unresolvable_chain_records_no_fact(tmp_path: Path) -> None:
    """There is no canonical problem to describe, so the gap is the only record."""
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(
        tmp_path,
        change_dir,
        problems=[_problem("PB-001", status="resolved", disposition="merged_into:PB-404")],
    )

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["problem_alias_invalid"]
    assert _row(projection).problem_facts == ()


def test_the_execution_phase_records_no_problem_facts(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(tmp_path, change_dir)

    assert _row(fold_trace(tmp_path, CHANGE_ID)).problem_facts == ()


# --------------------------------------------------------------------------- #
# missing / corrupt join inputs
# --------------------------------------------------------------------------- #


def test_missing_issues_snapshot_gaps_and_suppresses_the_join(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])
    _write_problems(tmp_path, [_problem("PB-001")])

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["issues_snapshot_missing"]
    source = next(s for s in projection.sources if s.path == ISSUES_SNAPSHOT_SOURCE)
    assert (source.exists, source.sha256) == (False, None)
    row = _row(projection)
    assert row.open_problem_ids == ()
    assert len(row.failures) == 1


def test_missing_problem_ledger_gaps_and_suppresses_the_join(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(change_dir, [_observation("OBS-1", CASE_ID)], [_occurrence("OCC-1", ["OBS-1"], "PB-001")])

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["problems_snapshot_missing"]
    source = next(s for s in projection.sources if s.path == PROJECT_PROBLEMS_SOURCE)
    assert (source.exists, source.sha256) == (False, None)
    assert _row(projection).open_problem_ids == ()


def test_a_missing_ledger_never_reports_every_reference_as_an_invalid_alias(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(change_dir, [_observation("OBS-1", CASE_ID)], [_occurrence("OCC-1", ["OBS-1"], "PB-001")])

    assert "problem_alias_invalid" not in _gap_codes(_reconciled(tmp_path))


@pytest.mark.parametrize(
    ("relative", "code", "logical"),
    [
        ("issues/snapshot.json", "issues_snapshot_missing", ISSUES_SNAPSHOT_SOURCE),
        ("qa/issues/problems.json", "problems_snapshot_missing", PROJECT_PROBLEMS_SOURCE),
    ],
)
def test_corrupt_join_inputs_are_typed_gaps_not_exceptions(
    tmp_path: Path, relative: str, code: str, logical: str
) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(tmp_path, change_dir)
    root = change_dir if relative.startswith("issues/") else tmp_path
    (root / relative).write_text('{"schema_version": ', encoding="utf-8")

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == [code]
    assert next(s for s in projection.sources if s.path == logical).exists is True
    assert _row(projection).open_problem_ids == ()


def test_a_problem_entry_missing_its_fingerprint_is_a_typed_gap(tmp_path: Path) -> None:
    """A fold that defaulted the digest would collapse unrelated Problems in the
    fingerprint dedupe, so the ledger is refused instead."""
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(change_dir, [_observation("OBS-1", CASE_ID)], [_occurrence("OCC-1", ["OBS-1"], "PB-001")])
    _write_json(
        tmp_path / "qa" / "issues" / "problems.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-07-02T12:00:00Z",
            "problems": [
                {
                    "problem_id": "PB-001",
                    "status": "detected",
                    "assessment": {"classification": "product_bug"},
                }
            ],
        },
    )

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["problems_snapshot_missing"]
    assert _row(projection).open_problem_ids == ()


def test_an_issues_snapshot_for_another_change_is_rejected_fail_closed(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(
        change_dir,
        [_observation("OBS-1", CASE_ID)],
        [_occurrence("OCC-1", ["OBS-1"], "PB-001")],
        change_id="CH-OTHER-999",
    )
    _write_problems(tmp_path, [_problem("PB-001")])

    projection = _reconciled(tmp_path)

    assert _gap_codes(projection) == ["issues_snapshot_missing"]
    assert "CH-OTHER-999" in _gaps(projection, "issues_snapshot_missing")[0]
    assert _row(projection).open_problem_ids == ()


def test_an_empty_snapshot_and_ledger_are_valid_empty_evidence(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _write_snapshot(change_dir, [], [])
    _write_problems(tmp_path, [])

    projection = _reconciled(tmp_path)

    assert projection.gaps == ()
    assert projection.integrity == "complete"
    assert _row(projection).open_problem_ids == ()


# --------------------------------------------------------------------------- #
# sources, determinism, integrity, dual-temporal invariant
# --------------------------------------------------------------------------- #


def test_sources_add_the_three_reconciled_inputs_with_digests(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    analysis_path = _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])
    _joined(tmp_path, change_dir)
    snapshot_path = change_dir / "issues" / "snapshot.json"
    problems_path = tmp_path / "qa" / "issues" / "problems.json"

    projection = _reconciled(tmp_path)

    execution_paths = _source_paths(fold_trace(tmp_path, CHANGE_ID))
    assert set(_source_paths(projection)) - set(execution_paths) == {
        FAILURE_ANALYSIS_SOURCE,
        ISSUES_SNAPSHOT_SOURCE,
        PROJECT_PROBLEMS_SOURCE,
    }
    for logical, path in (
        (FAILURE_ANALYSIS_SOURCE, analysis_path),
        (ISSUES_SNAPSHOT_SOURCE, snapshot_path),
        (PROJECT_PROBLEMS_SOURCE, problems_path),
    ):
        source = next(item for item in projection.sources if item.path == logical)
        assert source.exists is True
        assert source.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_sources_are_ordered_deterministically(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])
    _joined(tmp_path, change_dir)

    paths = _source_paths(_reconciled(tmp_path))

    assert paths == sorted(paths)


def test_repeated_reconciled_folds_are_byte_identical(tmp_path: Path) -> None:
    change_dir = _change(tmp_path, [CASE_ID, OTHER_CASE_ID])
    _write_failure_analysis(
        change_dir,
        [_failure_entry(OTHER_CASE_ID), _failure_entry(CASE_ID), _failure_entry(CASE_ID, severity="low")],
    )
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-2", OTHER_CASE_ID), _observation("OBS-1", CASE_ID)],
        occurrences=[
            _occurrence("OCC-2", ["OBS-2"], "PB-009"),
            _occurrence("OCC-1", ["OBS-1"], "PB-002"),
        ],
        problems=[_problem("PB-009"), _problem("PB-002")],
    )

    first = _reconciled(tmp_path)
    second = _reconciled(tmp_path)

    assert first.model_dump_json() == second.model_dump_json()


def test_reconciled_phase_is_recorded_on_the_projection(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [])
    _joined(tmp_path, change_dir)

    assert _reconciled(tmp_path).phase == "reconciled"
    assert fold_trace(tmp_path, CHANGE_ID).phase == "execution"


def test_integrity_is_complete_when_all_reconciled_inputs_are_readable(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])
    _joined(tmp_path, change_dir)

    projection = _reconciled(tmp_path)

    assert projection.gaps == ()
    assert projection.integrity == "complete"


def test_a_missing_reconciled_input_makes_the_projection_incomplete(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _joined(tmp_path, change_dir)

    projection = _reconciled(tmp_path)

    assert projection.integrity == "incomplete"
    assert fold_trace(tmp_path, CHANGE_ID).integrity == "complete"


def test_the_execution_phase_never_populates_the_reconciled_fields(tmp_path: Path) -> None:
    change_dir = _change(tmp_path)
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])
    _joined(tmp_path, change_dir)

    projection = fold_trace(tmp_path, CHANGE_ID)

    assert _row(projection).failures == ()
    assert _row(projection).open_problem_ids == ()
    assert _row(projection).problem_facts == ()
    assert set(_source_paths(projection)).isdisjoint(
        {FAILURE_ANALYSIS_SOURCE, ISSUES_SNAPSHOT_SOURCE, PROJECT_PROBLEMS_SOURCE}
    )


def test_reconciled_enrichment_changes_no_execution_phase_row_fact(tmp_path: Path) -> None:
    """The spec's dual-temporal invariant: enrichment only adds.

    Every field a row carries in the execution phase must survive the reconciled
    fold byte-for-byte; only ``failures``, ``open_problem_ids`` and
    ``problem_facts`` may differ, and this fixture makes all three non-empty so
    the comparison is not vacuous.
    """
    change_dir = _change(tmp_path, [CASE_ID, OTHER_CASE_ID])
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID), _failure_entry(OTHER_CASE_ID)])
    _joined(
        tmp_path,
        change_dir,
        observations=[_observation("OBS-1", CASE_ID), _observation("OBS-2", OTHER_CASE_ID)],
        occurrences=[
            _occurrence("OCC-1", ["OBS-1"], "PB-001"),
            _occurrence("OCC-2", ["OBS-2"], "PB-002"),
        ],
        problems=[_problem("PB-001"), _problem("PB-002")],
    )

    execution = fold_trace(tmp_path, CHANGE_ID)
    reconciled = _reconciled(tmp_path)

    enriched = {"failures", "open_problem_ids", "problem_facts"}
    assert [row.case_id for row in reconciled.rows] == [row.case_id for row in execution.rows]
    for before, after in zip(execution.rows, reconciled.rows, strict=True):
        assert after.model_dump(exclude=enriched) == before.model_dump(exclude=enriched)
        assert after.failures != ()
        assert after.open_problem_ids != ()
        assert after.problem_facts != ()
    assert reconciled.authoritative_batch_id == execution.authoritative_batch_id
    assert reconciled.unmapped_tests == execution.unmapped_tests


def test_reconciled_gaps_extend_the_execution_phase_gaps(tmp_path: Path) -> None:
    """A gap the execution phase found is still reported, in the same order."""
    change_dir = _change(tmp_path)
    (change_dir / "execution" / "execution-manifest.yaml").unlink()
    _write_failure_analysis(change_dir, [_failure_entry(CASE_ID)])
    _joined(tmp_path, change_dir)

    execution = fold_trace(tmp_path, CHANGE_ID)
    reconciled = _reconciled(tmp_path)

    assert _gap_codes(execution) == ["manifest_missing"]
    assert _gap_codes(reconciled)[: len(execution.gaps)] == _gap_codes(execution)
    assert list(reconciled.gaps[: len(execution.gaps)]) == list(execution.gaps)
