"""Tests for assurance_agent.workflow.issues.reconciler.

Coverage:
- plan_reconciliation:
  * Valid single candidate: new problem detected (occurrence_detected + problem_detected)
  * Valid candidate: exact fingerprint match → occurrence_linked + problem_occurrence_linked
  * Valid candidate: exact fingerprint match, resolved problem → regression
  * Valid candidate: possible_problem_ids with no exact match → merge_suggested
  * Multiple candidates, same fingerprint → first detected, second linked
  * Multiple candidates, two distinct fingerprints → two problems detected
  * issue_analysis_completed is the FIRST change event
  * All-or-nothing semantic validation:
    - Unknown observation_id → ReconciliationValidationError (no events)
    - Duplicate candidate_id → ReconciliationValidationError (no events)
    - Duplicate occurrence_id (identical candidate content) → ReconciliationValidationError
    - Incomplete fingerprint inputs (empty symptom) → ReconciliationValidationError
    - Unknown possible_problem_id → ReconciliationValidationError
    - Valid candidate before invalid one: still all-or-nothing rejection
  * Empty candidate list → only issue_analysis_completed change event, no problem events
  * Idempotency: same candidate batch → same event IDs and idempotency keys
"""

from __future__ import annotations

import pytest

from assurance_agent.artifacts.models.issues import (
    AffectedSurface,
    ChangeIssueSnapshot,
    FingerprintInputs,
    IssueCandidate,
    IssueCandidateDocument,
    IssueCandidateProposed,
    IssueClassification,
    IssueEvidenceManifest,
    IssueEvidenceManifestEntry,
    IssueSeverity,
    Observation,
    ObservationDocument,
    ObservationSource,
    Problem,
    ProblemAssessment,
    ProblemProjection,
    ProblemResolution,
    ProblemSeenRef,
)
from assurance_agent.workflow.issues.identity import (
    problem_fingerprint,
    problem_id,
)
from assurance_agent.workflow.issues.reconciler import (
    ReconciliationPlan,
    ReconciliationValidationError,
    plan_reconciliation,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_observation(obs_id: str, change_id: str = "CH-001", batch_id: str = "BATCH-001") -> Observation:
    return Observation(
        observation_id=obs_id,
        change_id=change_id,
        batch_id=batch_id,
        kind="test_failure",
        target="api",
        case_id="test-case-1",
        source=ObservationSource(
            artifact="execution/runs/BATCH-001/api-result.json",
            json_pointer="/cases/0",
        ),
        evidence_refs=["execution/runs/BATCH-001/api-result.json"],
        signature="GET /api/v1/users returned HTTP 500",
        observed_at="2026-07-25T10:00:00Z",
    )


def _make_observations(
    *obs_ids: str,
    change_id: str = "CH-001",
    batch_id: str = "BATCH-001",
) -> ObservationDocument:
    return ObservationDocument(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        observations=[_make_observation(oid, change_id=change_id, batch_id=batch_id) for oid in obs_ids],
    )


def _make_candidate(
    candidate_id: str,
    obs_ids: list[str],
    surface_value: str = "GET /api/v1/users",
    symptom: str = "returns http 500",
    possible_problem_ids: list[str] | None = None,
    title: str = "API endpoint returns 500",
    classification: IssueClassification = "product_bug",
    severity: IssueSeverity = "high",
) -> IssueCandidate:
    return IssueCandidate(
        candidate_id=candidate_id,
        observation_ids=obs_ids,
        proposed=IssueCandidateProposed(
            title=title,
            classification=classification,
            severity=severity,
            root_cause_hypothesis="Unhandled exception in endpoint handler",
        ),
        affected_surface=AffectedSurface(kind="endpoint", value=surface_value),
        fingerprint_inputs=FingerprintInputs(
            surface="endpoint",
            symptom=symptom,
            qualifiers=None,
        ),
        possible_problem_ids=possible_problem_ids or [],
        confidence=0.85,
        recommended_action="investigate and fix",
    )


def _make_candidate_doc(
    candidates: list[IssueCandidate],
    change_id: str = "CH-001",
    batch_id: str = "BATCH-001",
    evidence_bundle_digest: str = "sha256:" + "a" * 64,
) -> IssueCandidateDocument:
    return IssueCandidateDocument(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        evidence_bundle_digest=evidence_bundle_digest,
        candidates=candidates,
    )


def _empty_problems() -> ProblemProjection:
    return ProblemProjection(
        schema_version="1.0",
        generated_at="1970-01-01T00:00:00Z",
        problems=[],
    )


def _empty_snapshot(change_id: str = "CH-001", batch_id: str = "BATCH-001") -> ChangeIssueSnapshot:
    return ChangeIssueSnapshot(
        schema_version="1.0",
        change_id=change_id,
        authoritative_batch_id=batch_id,
        observations=[],
        occurrences=[],
        analysis_status=None,
        project_sync_status="completed",
        batches=[batch_id],
    )


def _make_manifest(
    change_id: str = "CH-001",
    batch_id: str = "BATCH-001",
    digest: str = "sha256:" + "a" * 64,
) -> IssueEvidenceManifest:
    return IssueEvidenceManifest(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        digest=digest,
        entries=[
            IssueEvidenceManifestEntry(
                path="execution/manifest.json",
                digest="sha256:" + "b" * 64,
            )
        ],
    )


def _plan(
    candidates_doc: IssueCandidateDocument,
    obs: ObservationDocument,
    snapshot: ChangeIssueSnapshot | None = None,
    problems: ProblemProjection | None = None,
    *,
    manifest: IssueEvidenceManifest | None = None,
    expected_change_id: str | None = None,
) -> ReconciliationPlan:
    change_id = expected_change_id or candidates_doc.change_id
    batch_id = candidates_doc.batch_id
    # Align fixture observations with the candidate batch unless the caller is
    # deliberately exercising the trusted-input boundary (custom manifest /
    # expected_change_id).
    if (
        manifest is None
        and expected_change_id is None
        and (obs.change_id != change_id or obs.batch_id != batch_id)
    ):
        obs = ObservationDocument(
            schema_version="1.0",
            change_id=change_id,
            batch_id=batch_id,
            observations=[
                item.model_copy(update={"change_id": change_id, "batch_id": batch_id})
                for item in obs.observations
            ],
        )
    return plan_reconciliation(
        candidates_doc,
        obs,
        snapshot if snapshot is not None else _empty_snapshot(change_id, batch_id),
        problems if problems is not None else _empty_problems(),
        manifest=manifest or _make_manifest(change_id, batch_id, candidates_doc.evidence_bundle_digest),
        expected_change_id=change_id,
    )


def _make_existing_problem(
    *,
    surface_value: str = "GET /api/v1/users",
    symptom: str = "returns http 500",
    status: str = "detected",
    version: int = 1,
) -> Problem:
    """Build a Problem that matches the default candidate surface/symptom."""
    fp = problem_fingerprint(
        affected_surface=AffectedSurface(kind="endpoint", value=surface_value),
        fingerprint_inputs=FingerprintInputs(surface="endpoint", symptom=symptom),
    )
    pid = problem_id(fp)
    ref = ProblemSeenRef(change_id="CH-prev", occurrence_id="OCC-prev")
    resolution = None
    if status == "resolved":
        resolution = ProblemResolution(
            resolved_at="2026-07-20T00:00:00Z",
            change_id="CH-fix",
            batch_id="BATCH-fix",
            disposition="fixed",
            verification_scope=["test-case-1"],
            evidence_digest="sha256:" + "b" * 64,
        )
    return Problem(
        problem_id=pid,
        fingerprint=fp,
        title="API endpoint returns 500",
        assessment=ProblemAssessment(
            classification="product_bug",
            severity="high",
            authority="llm_provisional",
            root_cause_hypothesis="Unhandled exception",
        ),
        status=status,  # type: ignore[arg-type]
        first_seen=ref,
        last_seen=ref,
        occurrences=["OCC-prev"],
        resolution=resolution,
        version=version,
    )


def _problems_with(*problems: Problem) -> ProblemProjection:
    return ProblemProjection(
        schema_version="1.0",
        generated_at="2026-07-25T00:00:00Z",
        problems=list(problems),
    )


# ---------------------------------------------------------------------------
# Tests: basic reconcile scenarios
# ---------------------------------------------------------------------------


def test_new_candidate_creates_problem_detected() -> None:
    """A candidate with no matching fingerprint → problem_detected + occurrence_detected."""
    obs = _make_observations("OBS-001")
    cand = _make_candidate("CAND-001", ["OBS-001"])
    candidates_doc = _make_candidate_doc([cand])

    plan = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    assert isinstance(plan, ReconciliationPlan)
    assert plan.occurrence_count == 1

    # Change events: analysis_completed + occurrence_detected
    change_types = [e.type for e in plan.change_events]
    assert change_types[0] == "issue_analysis_completed", "First event must be issue_analysis_completed"
    assert "occurrence_detected" in change_types

    # Problem events: problem_detected
    problem_types = [e.type for e in plan.problem_events]
    assert "problem_detected" in problem_types
    assert "problem_occurrence_linked" not in problem_types


def test_issue_analysis_completed_is_first_change_event() -> None:
    """issue_analysis_completed must be the very first Change event."""
    obs = _make_observations("OBS-001")
    cand = _make_candidate("CAND-001", ["OBS-001"])
    candidates_doc = _make_candidate_doc([cand])

    plan = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    assert plan.change_events[0].type == "issue_analysis_completed"


def test_exact_fingerprint_match_links_occurrence() -> None:
    """Candidate matching an existing problem fingerprint → occurrence_linked + problem_occurrence_linked."""
    obs = _make_observations("OBS-002")
    cand = _make_candidate("CAND-002", ["OBS-002"])
    candidates_doc = _make_candidate_doc([cand], change_id="CH-002", batch_id="BATCH-002")

    existing = _make_existing_problem(status="detected", version=1)
    problems = _problems_with(existing)

    plan = _plan(
        candidates_doc, obs, _empty_snapshot(change_id="CH-002", batch_id="BATCH-002"), problems
    )

    change_types = [e.type for e in plan.change_events]
    problem_types = [e.type for e in plan.problem_events]

    assert "occurrence_linked" in change_types
    assert "occurrence_detected" not in change_types
    assert "problem_occurrence_linked" in problem_types
    assert "problem_detected" not in problem_types


def test_exact_fingerprint_resolved_triggers_regression() -> None:
    """Candidate matching a resolved problem → occurrence_detected + problem_regressed."""
    obs = _make_observations("OBS-003")
    cand = _make_candidate("CAND-003", ["OBS-003"])
    candidates_doc = _make_candidate_doc([cand], change_id="CH-003", batch_id="BATCH-003")

    existing = _make_existing_problem(status="resolved", version=2)
    problems = _problems_with(existing)

    plan = _plan(
        candidates_doc, obs, _empty_snapshot(change_id="CH-003", batch_id="BATCH-003"), problems
    )

    change_types = [e.type for e in plan.change_events]
    problem_types = [e.type for e in plan.problem_events]

    assert "occurrence_detected" in change_types
    assert "problem_regressed" in problem_types
    assert "problem_occurrence_linked" not in problem_types
    assert "problem_detected" not in problem_types


def test_possible_problem_ids_with_no_exact_match_emits_merge_suggested() -> None:
    """Possible matches from LLM (but no exact fingerprint) → problem_merge_suggested."""
    obs = _make_observations("OBS-004")
    existing = _make_existing_problem(
        surface_value="POST /api/v1/orders",
        symptom="server error",
        status="detected",
        version=1,
    )
    problems = _problems_with(existing)
    existing_pid = existing.problem_id

    # Candidate uses a DIFFERENT surface (different fingerprint) but names existing problem as possible
    cand = _make_candidate(
        "CAND-004",
        ["OBS-004"],
        surface_value="GET /api/v1/products",
        symptom="http 500 error",
        possible_problem_ids=[existing_pid],
    )
    candidates_doc = _make_candidate_doc([cand], change_id="CH-004", batch_id="BATCH-004")

    plan = _plan(
        candidates_doc, obs, _empty_snapshot(change_id="CH-004", batch_id="BATCH-004"), problems
    )

    problem_types = [e.type for e in plan.problem_events]
    assert "problem_merge_suggested" in problem_types
    assert "problem_detected" in problem_types  # new problem still created


def test_two_candidates_same_fingerprint_first_detected_second_linked() -> None:
    """Two candidates with the same fingerprint: first → detected, second → linked."""
    obs = _make_observations("OBS-A", "OBS-B")
    cand_a = _make_candidate("CAND-A", ["OBS-A"])
    cand_b = _make_candidate("CAND-B", ["OBS-B"])  # same surface+symptom = same fingerprint
    candidates_doc = _make_candidate_doc([cand_a, cand_b], change_id="CH-005", batch_id="BATCH-005")

    plan = _plan(
        candidates_doc, obs, _empty_snapshot(change_id="CH-005", batch_id="BATCH-005"), _empty_problems()
    )

    change_types = [e.type for e in plan.change_events]
    problem_types = [e.type for e in plan.problem_events]

    assert change_types.count("occurrence_detected") == 1
    assert change_types.count("occurrence_linked") == 1
    assert problem_types.count("problem_detected") == 1
    assert problem_types.count("problem_occurrence_linked") == 1

    # Verify version sequencing in linked event
    linked_events = [e for e in plan.problem_events if e.type == "problem_occurrence_linked"]
    assert len(linked_events) == 1
    assert linked_events[0].expected_problem_version == 1  # after detection at version 1


def test_two_candidates_different_fingerprints_creates_two_problems() -> None:
    """Two candidates with distinct fingerprints → two distinct problems."""
    obs = _make_observations("OBS-X", "OBS-Y")
    cand_x = _make_candidate("CAND-X", ["OBS-X"], surface_value="GET /api/v1/users", symptom="http 500")
    cand_y = _make_candidate("CAND-Y", ["OBS-Y"], surface_value="POST /api/v1/orders", symptom="server error")
    candidates_doc = _make_candidate_doc([cand_x, cand_y], change_id="CH-006", batch_id="BATCH-006")

    plan = _plan(
        candidates_doc, obs, _empty_snapshot(change_id="CH-006", batch_id="BATCH-006"), _empty_problems()
    )

    problem_types = [e.type for e in plan.problem_events]
    assert problem_types.count("problem_detected") == 2

    # Distinct problem IDs
    detected_events = [e for e in plan.problem_events if e.type == "problem_detected"]
    pids = [e.problem_id for e in detected_events]
    assert pids[0] != pids[1]


def test_empty_candidate_list_emits_only_analysis_completed() -> None:
    """Zero candidates → only issue_analysis_completed change event, no problem events."""
    obs = _make_observations("OBS-CLEAN")
    candidates_doc = _make_candidate_doc([])

    plan = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    assert plan.occurrence_count == 0
    assert len(plan.change_events) == 1
    assert plan.change_events[0].type == "issue_analysis_completed"
    assert len(plan.problem_events) == 0


def test_candidate_digest_is_stable() -> None:
    """Same candidates document → same candidate_digest on every call."""
    obs = _make_observations("OBS-001")
    cand = _make_candidate("CAND-001", ["OBS-001"])
    candidates_doc = _make_candidate_doc([cand])

    plan1 = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())
    plan2 = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    assert plan1.candidate_digest == plan2.candidate_digest
    assert plan1.candidate_digest.startswith("sha256:")


def test_idempotency_same_idempotency_keys() -> None:
    """Two calls with the same inputs produce identical idempotency_keys."""
    obs = _make_observations("OBS-001")
    cand = _make_candidate("CAND-001", ["OBS-001"])
    candidates_doc = _make_candidate_doc([cand])

    plan1 = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())
    plan2 = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    idem1 = {e.idempotency_key for e in plan1.change_events} | {
        e.idempotency_key for e in plan1.problem_events
    }
    idem2 = {e.idempotency_key for e in plan2.change_events} | {
        e.idempotency_key for e in plan2.problem_events
    }
    assert idem1 == idem2


def test_occurrence_id_is_stable_and_unique() -> None:
    """Occurrence IDs are deterministic and unique across different candidates."""
    obs = _make_observations("OBS-001", "OBS-002")
    cand_a = _make_candidate("CAND-A", ["OBS-001"])
    cand_b = _make_candidate("CAND-B", ["OBS-002"], surface_value="POST /api/v1/orders", symptom="500 error")
    candidates_doc = _make_candidate_doc([cand_a, cand_b])

    plan = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    from assurance_agent.workflow.issues.events import (
        OccurrenceDetectedEvent,
        OccurrenceLinkedEvent,
    )

    occ_events = [
        e for e in plan.change_events if isinstance(e, (OccurrenceDetectedEvent, OccurrenceLinkedEvent))
    ]
    occ_ids = [e.occurrence.occurrence_id for e in occ_events]
    assert len(occ_ids) == 2
    assert occ_ids[0] != occ_ids[1], "Different candidates must produce different occurrence IDs"
    assert all(oid.startswith("OCC-") for oid in occ_ids)


def test_problem_id_prefix_is_prob() -> None:
    """All problem IDs produced start with 'PROB-'."""
    obs = _make_observations("OBS-001")
    cand = _make_candidate("CAND-001", ["OBS-001"])
    candidates_doc = _make_candidate_doc([cand])

    plan = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    detected = [e for e in plan.problem_events if e.type == "problem_detected"]
    assert all(e.problem_id.startswith("PROB-") for e in detected)


def test_occurrence_links_correct_problem_id() -> None:
    """The occurrence_id in problem events matches the occurrence in change events."""
    obs = _make_observations("OBS-001")
    cand = _make_candidate("CAND-001", ["OBS-001"])
    candidates_doc = _make_candidate_doc([cand])

    plan = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    detected_problem = next(e for e in plan.problem_events if e.type == "problem_detected")
    detected_change = next(e for e in plan.change_events if e.type == "occurrence_detected")

    assert detected_problem.occurrence_id == detected_change.occurrence.occurrence_id
    assert detected_problem.problem_id == detected_change.occurrence.problem_id


def test_regression_resets_problem_version() -> None:
    """Regression event has the correct expected_problem_version."""
    obs = _make_observations("OBS-REG")
    cand = _make_candidate("CAND-REG", ["OBS-REG"])
    candidates_doc = _make_candidate_doc([cand], change_id="CH-REG", batch_id="BATCH-REG")

    existing = _make_existing_problem(status="resolved", version=3)
    problems = _problems_with(existing)

    plan = _plan(
        candidates_doc, obs, _empty_snapshot(change_id="CH-REG", batch_id="BATCH-REG"), problems
    )

    regressed = next(e for e in plan.problem_events if e.type == "problem_regressed")
    assert regressed.expected_problem_version == 3  # matches existing.version


def test_occurrence_linked_has_correct_expected_version() -> None:
    """problem_occurrence_linked uses the current problem version."""
    obs = _make_observations("OBS-LINK")
    cand = _make_candidate("CAND-LINK", ["OBS-LINK"])
    candidates_doc = _make_candidate_doc([cand], change_id="CH-LINK", batch_id="BATCH-LINK")

    existing = _make_existing_problem(status="triaged", version=4)
    problems = _problems_with(existing)

    plan = _plan(
        candidates_doc, obs, _empty_snapshot(change_id="CH-LINK", batch_id="BATCH-LINK"), problems
    )

    linked = next(e for e in plan.problem_events if e.type == "problem_occurrence_linked")
    assert linked.expected_problem_version == 4


# ---------------------------------------------------------------------------
# Tests: all-or-nothing semantic validation
# ---------------------------------------------------------------------------


def test_validation_rejects_unknown_observation_id() -> None:
    """A candidate referencing an unknown observation_id → ReconciliationValidationError."""
    obs = _make_observations("OBS-KNOWN")
    cand = _make_candidate("CAND-BAD", ["OBS-UNKNOWN"])  # not in observations
    candidates_doc = _make_candidate_doc([cand])

    with pytest.raises(ReconciliationValidationError) as exc_info:
        _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    assert any("OBS-UNKNOWN" in e for e in exc_info.value.errors)


def test_validation_rejects_duplicate_candidate_id() -> None:
    """Two candidates with the same candidate_id → ReconciliationValidationError."""
    obs = _make_observations("OBS-A", "OBS-B")
    cand_a = _make_candidate("SAME-ID", ["OBS-A"])
    cand_b = _make_candidate("SAME-ID", ["OBS-B"], surface_value="POST /api/v1/orders", symptom="error")
    candidates_doc = _make_candidate_doc([cand_a, cand_b])

    with pytest.raises(ReconciliationValidationError) as exc_info:
        _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    assert any("SAME-ID" in e for e in exc_info.value.errors)


def test_validation_rejects_unknown_possible_problem_id() -> None:
    """A possible_problem_id that doesn't exist in the projection → ReconciliationValidationError."""
    obs = _make_observations("OBS-001")
    cand = _make_candidate("CAND-001", ["OBS-001"], possible_problem_ids=["PROB-nonexistent"])
    candidates_doc = _make_candidate_doc([cand])

    with pytest.raises(ReconciliationValidationError) as exc_info:
        _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    assert any("PROB-nonexistent" in e for e in exc_info.value.errors)


def test_validation_rejects_incomplete_fingerprint_inputs_empty_symptom() -> None:
    """A candidate with an empty symptom (after normalization) → ReconciliationValidationError."""
    obs = _make_observations("OBS-001")
    # Empty symptom after normalization
    cand = IssueCandidate(
        candidate_id="CAND-BAD",
        observation_ids=["OBS-001"],
        proposed=IssueCandidateProposed(
            title="Bug",
            classification="product_bug",
            severity="high",
            root_cause_hypothesis="Unknown",
        ),
        affected_surface=AffectedSurface(kind="endpoint", value="GET /api/v1/users"),
        fingerprint_inputs=FingerprintInputs(
            surface="endpoint",
            symptom="   ",  # only whitespace → empty after normalization
        ),
        possible_problem_ids=[],
        confidence=0.5,
        recommended_action="investigate",
    )
    candidates_doc = _make_candidate_doc([cand])

    with pytest.raises(ReconciliationValidationError) as exc_info:
        _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    assert exc_info.value.errors  # at least one error


def test_validation_all_or_nothing_valid_before_invalid() -> None:
    """A valid candidate followed by an invalid one → zero events from both."""
    obs = _make_observations("OBS-GOOD", "OBS-BAD")
    cand_good = _make_candidate("CAND-GOOD", ["OBS-GOOD"])
    cand_bad = _make_candidate("CAND-BAD", ["OBS-UNKNOWN"])  # unknown obs
    candidates_doc = _make_candidate_doc([cand_good, cand_bad])

    with pytest.raises(ReconciliationValidationError):
        _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())


def test_validation_all_or_nothing_invalid_before_valid() -> None:
    """An invalid candidate followed by a valid one → zero events from both."""
    obs = _make_observations("OBS-GOOD")
    cand_bad = _make_candidate("CAND-BAD", ["OBS-UNKNOWN"])  # unknown obs
    cand_good = _make_candidate(
        "CAND-GOOD", ["OBS-GOOD"], surface_value="POST /api/v1/orders", symptom="error"
    )
    candidates_doc = _make_candidate_doc([cand_bad, cand_good])

    with pytest.raises(ReconciliationValidationError):
        _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())


def test_validation_multiple_errors_collected() -> None:
    """Multiple invalid candidates → all errors reported, not just the first."""
    obs = _make_observations()  # no valid observations
    cand_a = _make_candidate("CAND-A", ["OBS-UNKNOWN-A"])
    cand_b = _make_candidate("CAND-B", ["OBS-UNKNOWN-B"])
    candidates_doc = _make_candidate_doc([cand_a, cand_b])

    with pytest.raises(ReconciliationValidationError) as exc_info:
        _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    assert len(exc_info.value.errors) >= 2


def test_duplicate_occurrence_id_from_identical_candidates() -> None:
    """Two candidates with identical content → duplicate occurrence_id → validation error."""
    obs = _make_observations("OBS-DUP")
    # Identical candidates: same surface, symptom, obs → same per-candidate digest → same occ_id
    # Duplicate candidate_id is the primary duplication guard exercised here.
    cand_dup_a = _make_candidate("CAND-DUP", ["OBS-DUP"])
    cand_dup_b = _make_candidate("CAND-DUP", ["OBS-DUP"])  # same id
    candidates_doc = _make_candidate_doc([cand_dup_a, cand_dup_b])

    with pytest.raises(ReconciliationValidationError):
        _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())


# ---------------------------------------------------------------------------
# Tests: merge suggestion only for NEW problems (no exact match)
# ---------------------------------------------------------------------------


def test_no_merge_suggestion_on_exact_fingerprint_link() -> None:
    """When there's an exact fingerprint match, NO merge_suggested events are emitted
    even if possible_problem_ids lists the matched problem."""
    obs = _make_observations("OBS-EXACT")
    existing = _make_existing_problem(status="detected", version=1)
    problems = _problems_with(existing)

    cand = _make_candidate(
        "CAND-EXACT",
        ["OBS-EXACT"],
        possible_problem_ids=[existing.problem_id],  # exact match
    )
    candidates_doc = _make_candidate_doc([cand], change_id="CH-EXACT", batch_id="BATCH-EXACT")

    plan = _plan(
        candidates_doc, obs, _empty_snapshot(change_id="CH-EXACT", batch_id="BATCH-EXACT"), problems
    )

    problem_types = [e.type for e in plan.problem_events]
    assert "problem_merge_suggested" not in problem_types, (
        "Exact fingerprint match must not emit merge suggestions"
    )


def test_analysis_completed_has_correct_candidate_count() -> None:
    """issue_analysis_completed carries the correct candidate count."""
    obs = _make_observations("OBS-001", "OBS-002")
    cands = [
        _make_candidate("CAND-001", ["OBS-001"]),
        _make_candidate("CAND-002", ["OBS-002"], surface_value="POST /api/v1/orders", symptom="error"),
    ]
    candidates_doc = _make_candidate_doc(cands)

    plan = _plan(candidates_doc, obs, _empty_snapshot(), _empty_problems())

    completed = plan.change_events[0]
    assert completed.type == "issue_analysis_completed"
    assert completed.analysis_status.candidate_count == 2
    assert completed.analysis_status.status == "completed"


def test_validation_rejects_forged_change_id() -> None:
    """Candidate document change_id must match the trusted runtime change_id."""
    obs = _make_observations("OBS-001")
    candidates_doc = _make_candidate_doc(
        [_make_candidate("CAND-001", ["OBS-001"])],
        change_id="CH-FORGED",
    )
    with pytest.raises(ReconciliationValidationError) as exc_info:
        _plan(candidates_doc, obs, expected_change_id="CH-001")
    assert any("change_id" in e for e in exc_info.value.errors)


def test_validation_rejects_digest_mismatch_against_manifest() -> None:
    """Candidate evidence_bundle_digest must equal the trusted manifest digest."""
    obs = _make_observations("OBS-001")
    candidates_doc = _make_candidate_doc(
        [_make_candidate("CAND-001", ["OBS-001"])],
        evidence_bundle_digest="sha256:" + "c" * 64,
    )
    manifest = _make_manifest(digest="sha256:" + "a" * 64)
    with pytest.raises(ReconciliationValidationError) as exc_info:
        _plan(candidates_doc, obs, manifest=manifest)
    assert any("evidence_bundle_digest" in e for e in exc_info.value.errors)


def test_validation_rejects_batch_id_mismatch_against_manifest() -> None:
    """Candidate/observation batch_id must match the trusted manifest batch."""
    obs = _make_observations("OBS-001", batch_id="BATCH-OTHER")
    candidates_doc = _make_candidate_doc(
        [_make_candidate("CAND-001", ["OBS-001"])],
        batch_id="BATCH-OTHER",
    )
    manifest = _make_manifest(batch_id="BATCH-001")
    with pytest.raises(ReconciliationValidationError) as exc_info:
        _plan(candidates_doc, obs, manifest=manifest)
    assert any("batch_id" in e for e in exc_info.value.errors)
