"""Tests for pure deterministic projections over Change Issue and Problem events.

Covers:
- Observation / Occurrence retention across multiple batches
- issue_analysis_completed / issue_analysis_failed updating analysis_status
- project_sync_pending / occurrence_detected resetting sync status
- All Problem lifecycle transitions and version tracking
- Merge suggestions populating review queue
- problem_merged leaving source history intact
- problem_regressed reopening a resolved Problem
- Byte-identical dump_projection on double replay
- ProjectionError on unknown problem_id and stale version
"""

from __future__ import annotations

import json

import pytest

from assurance_agent.artifacts.models.issues import (
    IssueAnalysisStatus,
    IssueOccurrence,
    OccurrenceAnalysis,
    Observation,
    ObservationSource,
    ProblemFingerprint,
    ProvisionalAssessment,
)
from assurance_agent.workflow.issues.events import (
    CHANGE_ISSUE_EVENT_ADAPTER,
    PROBLEM_EVENT_ADAPTER,
)
from assurance_agent.workflow.issues.projection import (
    ProjectionError,
    dump_projection,
    project_change_issues,
    project_problems,
    project_review_queue,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

FINGERPRINT = ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)
FINGERPRINT_B = ProblemFingerprint(version="1", digest="sha256:" + "b" * 64)

OBS_1 = Observation(
    observation_id="OBS-1111111111111111",
    change_id="CH-001",
    batch_id="B-001",
    kind="test_failure",
    target="api",
    case_id=None,
    source=ObservationSource(
        artifact="execution/runs/B-001/api-result.json",
        json_pointer="/cases/0",
    ),
    evidence_refs=["execution/runs/B-001/api-result.json"],
    signature="http_500_empty_name",
    observed_at="2026-07-25T10:00:00Z",
)

OBS_2 = Observation(
    observation_id="OBS-2222222222222222",
    change_id="CH-001",
    batch_id="B-002",
    kind="warning",
    target="api",
    case_id="API-001",
    source=ObservationSource(
        artifact="execution/runs/B-002/api-result.json",
        json_pointer="/cases/1",
    ),
    evidence_refs=["execution/runs/B-002/api-result.json"],
    signature="http_warning_slow_response",
    observed_at="2026-07-25T11:00:00Z",
)

ANALYSIS_OK = IssueAnalysisStatus(
    schema_version="1.0",
    change_id="CH-001",
    batch_id="B-001",
    status="completed",
    evidence_bundle_digest="sha256:aabbccdd",
    candidate_count=1,
    candidate_digest="sha256:11223344",
)

ANALYSIS_FAIL = IssueAnalysisStatus(
    schema_version="1.0",
    change_id="CH-001",
    batch_id="B-001",
    status="failed",
    evidence_bundle_digest="sha256:aabbccdd",
    candidate_count=0,
    reason="timeout",
    retryable=True,
)

OCC_1 = IssueOccurrence(
    occurrence_id="OCC-1111111111111111",
    change_id="CH-001",
    batch_id="B-001",
    observation_ids=["OBS-1111111111111111"],
    problem_id="PROB-deadbeef12345678",
    provisional_assessment=ProvisionalAssessment(
        classification="product_bug",
        severity="high",
        authority="llm_provisional",
        root_cause_hypothesis="null pointer",
    ),
    analysis=OccurrenceAnalysis(
        evidence_bundle_digest="sha256:aabbccdd",
        analyzer="aa-issue-analyzer",
        prompt_version="v1",
        candidate_digest="sha256:11223344",
    ),
)

OCC_2 = IssueOccurrence(
    occurrence_id="OCC-2222222222222222",
    change_id="CH-001",
    batch_id="B-002",
    observation_ids=["OBS-2222222222222222"],
    problem_id="PROB-deadbeef12345678",
    provisional_assessment=ProvisionalAssessment(
        classification="product_bug",
        severity="high",
        authority="llm_provisional",
        root_cause_hypothesis="null pointer again",
    ),
    analysis=OccurrenceAnalysis(
        evidence_bundle_digest="sha256:bbccddee",
        analyzer="aa-issue-analyzer",
        prompt_version="v1",
        candidate_digest="sha256:22334455",
    ),
)

PROB_ID = "PROB-deadbeef12345678"
PROB_ID_B = "PROB-beefdeadcafef00d"


def _seq_counter():
    n = 0

    def next_seq():
        nonlocal n
        n += 1
        return n

    return next_seq


def _mk_change_event(seq: int, event_id: str, idem: str, **kwargs) -> object:
    data = {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id,
        "idempotency_key": idem,
        "ts": f"2026-07-25T10:0{seq}:00Z",
        "evidence_digest": "sha256:aabbccdd",
        "change_id": "CH-001",
        "batch_id": "B-001",
        **kwargs,
    }
    return CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)


def _mk_problem_event(seq: int, event_id: str, idem: str, prob_id: str, ver: int, **kwargs) -> object:
    data = {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id,
        "idempotency_key": idem,
        "ts": f"2026-07-25T10:0{seq}:00Z",
        "evidence_digest": "sha256:aabbccdd",
        "problem_id": prob_id,
        "expected_problem_version": ver,
        **kwargs,
    }
    return PROBLEM_EVENT_ADAPTER.validate_python(data)


# ---------------------------------------------------------------------------
# project_change_issues
# ---------------------------------------------------------------------------


class TestProjectChangeIssues:
    def test_empty_events_raises(self) -> None:
        with pytest.raises(ProjectionError, match="no events"):
            project_change_issues([])

    def test_single_observation(self) -> None:
        events = [
            _mk_change_event(
                1,
                "E1",
                "I1",
                type="observation_recorded",
                observation=OBS_1.model_dump(mode="json"),
            )
        ]
        snap = project_change_issues(events)  # type: ignore[arg-type]
        assert snap.schema_version == "1.0"
        assert snap.change_id == "CH-001"
        assert len(snap.observations) == 1
        assert snap.observations[0].observation_id == OBS_1.observation_id
        assert snap.analysis_status is None
        assert snap.project_sync_status == "completed"
        assert snap.batches == ["B-001"]

    def test_observations_retained_across_batches(self) -> None:
        events = [
            _mk_change_event(
                1,
                "E1",
                "I1",
                type="observation_recorded",
                observation=OBS_1.model_dump(mode="json"),
            ),
            _mk_change_event(
                2,
                "E2",
                "I2",
                type="observation_recorded",
                batch_id="B-002",
                observation=OBS_2.model_dump(mode="json"),
            ),
        ]
        snap = project_change_issues(events)  # type: ignore[arg-type]
        assert len(snap.observations) == 2
        assert "B-001" in snap.batches
        assert "B-002" in snap.batches

    def test_analysis_completed_sets_status(self) -> None:
        events = [
            _mk_change_event(
                1,
                "E1",
                "I1",
                type="observation_recorded",
                observation=OBS_1.model_dump(mode="json"),
            ),
            _mk_change_event(
                2,
                "E2",
                "I2",
                type="issue_analysis_completed",
                analysis_status=ANALYSIS_OK.model_dump(mode="json"),
            ),
        ]
        snap = project_change_issues(events)  # type: ignore[arg-type]
        assert snap.analysis_status is not None
        assert snap.analysis_status.status == "completed"
        assert snap.analysis_status.candidate_count == 1
        assert snap.authoritative_batch_id == "B-001"

    def test_analysis_failed_sets_status(self) -> None:
        events = [
            _mk_change_event(
                1,
                "E1",
                "I1",
                type="observation_recorded",
                observation=OBS_1.model_dump(mode="json"),
            ),
            _mk_change_event(
                2,
                "E2",
                "I2",
                type="issue_analysis_failed",
                analysis_status=ANALYSIS_FAIL.model_dump(mode="json"),
            ),
        ]
        snap = project_change_issues(events)  # type: ignore[arg-type]
        assert snap.analysis_status is not None
        assert snap.analysis_status.reason == "timeout"

    def test_occurrence_detected_added(self) -> None:
        events = [
            _mk_change_event(
                1,
                "E1",
                "I1",
                type="observation_recorded",
                observation=OBS_1.model_dump(mode="json"),
            ),
            _mk_change_event(
                2,
                "E2",
                "I2",
                type="occurrence_detected",
                occurrence=OCC_1.model_dump(mode="json"),
            ),
        ]
        snap = project_change_issues(events)  # type: ignore[arg-type]
        assert len(snap.occurrences) == 1
        assert snap.occurrences[0].occurrence_id == OCC_1.occurrence_id

    def test_occurrence_linked_added(self) -> None:
        events = [
            _mk_change_event(
                1,
                "E1",
                "I1",
                type="occurrence_linked",
                occurrence=OCC_1.model_dump(mode="json"),
            ),
        ]
        snap = project_change_issues(events)  # type: ignore[arg-type]
        assert len(snap.occurrences) == 1

    def test_project_sync_pending_sets_status(self) -> None:
        events = [
            _mk_change_event(
                1,
                "E1",
                "I1",
                type="observation_recorded",
                observation=OBS_1.model_dump(mode="json"),
            ),
            _mk_change_event(
                2,
                "E2",
                "I2",
                type="project_sync_pending",
                candidate_digest="sha256:11223344",
            ),
        ]
        snap = project_change_issues(events)  # type: ignore[arg-type]
        assert snap.project_sync_status == "pending"

    def test_occurrence_after_sync_pending_resets_status(self) -> None:
        events = [
            _mk_change_event(
                1,
                "E1",
                "I1",
                type="observation_recorded",
                observation=OBS_1.model_dump(mode="json"),
            ),
            _mk_change_event(
                2,
                "E2",
                "I2",
                type="project_sync_pending",
                candidate_digest="sha256:11223344",
            ),
            _mk_change_event(
                3,
                "E3",
                "I3",
                type="occurrence_detected",
                occurrence=OCC_1.model_dump(mode="json"),
            ),
        ]
        snap = project_change_issues(events)  # type: ignore[arg-type]
        assert snap.project_sync_status == "completed"


# ---------------------------------------------------------------------------
# project_problems
# ---------------------------------------------------------------------------


def _detected_event(seq: int = 1, prob_id: str = PROB_ID, occ_id: str = "OCC-1111111111111111") -> object:
    return _mk_problem_event(
        seq,
        f"E{seq}",
        f"I{seq}",
        prob_id,
        0,
        type="problem_detected",
        occurrence_id=occ_id,
        change_id="CH-001",
        batch_id="B-001",
        fingerprint=FINGERPRINT.model_dump(mode="json"),
        title="HTTP 500 on empty name",
        classification="product_bug",
        severity="high",
    )


class TestProjectProblems:
    def test_empty_events_returns_empty_projection(self) -> None:
        proj = project_problems([])
        assert proj.problems == []
        assert proj.schema_version == "1.0"
        assert proj.generated_at  # non-empty sentinel

    def test_problem_detected_creates_version_1(self) -> None:
        proj = project_problems([_detected_event()])  # type: ignore[list-item]
        assert len(proj.problems) == 1
        p = proj.problems[0]
        assert p.problem_id == PROB_ID
        assert p.version == 1
        assert p.status == "detected"
        assert p.assessment.authority == "llm_provisional"
        assert p.assessment.classification == "product_bug"
        assert p.assessment.severity == "high"
        assert p.occurrences == ["OCC-1111111111111111"]

    def test_problem_occurrence_linked_increments_version(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_occurrence_linked",
                occurrence_id="OCC-2222222222222222",
                change_id="CH-001",
                batch_id="B-002",
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        p = proj.problems[0]
        assert p.version == 2
        assert "OCC-2222222222222222" in p.occurrences
        assert p.last_seen.occurrence_id == "OCC-2222222222222222"

    def test_assessment_confirmed_human_authority(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_assessment_confirmed",
                classification="test_bug",
                severity="low",
                reason="reviewed",
                evidence_refs=["OCC-1111111111111111"],
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        p = proj.problems[0]
        assert p.version == 2
        assert p.status == "triaged"
        assert p.assessment.authority == "human_confirmed"
        assert p.assessment.classification == "test_bug"
        assert p.assessment.severity == "low"

    def test_work_started_sets_in_progress(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_assessment_confirmed",
                classification="product_bug",
                severity="high",
                reason="triaged",
                evidence_refs=["OCC-1111111111111111"],
            ),
            _mk_problem_event(
                3,
                "E3",
                "I3",
                PROB_ID,
                2,
                type="problem_work_started",
                reason="assigned",
                evidence_refs=["OCC-1111111111111111"],
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        p = proj.problems[0]
        assert p.status == "in_progress"
        assert p.version == 3

    def test_verification_requested(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_verification_requested",
                verification_scope=["API-TEST-001"],
                linked_fix_disposition="PR-42",
                change_id="CH-002",
                batch_id="B-003",
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        p = proj.problems[0]
        assert p.status == "verification_pending"
        assert p.version == 2
        assert p.verification_request is not None
        assert p.verification_request.verification_scope == ["API-TEST-001"]
        assert p.verification_request.linked_fix_disposition == "PR-42"
        assert p.verification_request.batch_id == "B-003"

    def test_resolved_sets_resolution(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_verification_requested",
                verification_scope=["API-TEST-001"],
                linked_fix_disposition="PR-42",
                change_id="CH-002",
                batch_id="B-003",
            ),
            _mk_problem_event(
                3,
                "E3",
                "I3",
                PROB_ID,
                2,
                type="problem_resolved",
                resolved_at="2026-07-25T12:00:00Z",
                change_id="CH-002",
                batch_id="B-003",
                disposition="PR-42 merged",
                verification_scope=["API-TEST-001"],
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        p = proj.problems[0]
        assert p.status == "resolved"
        assert p.resolution is not None
        assert p.resolution.disposition == "PR-42 merged"
        assert p.version == 3

    def test_not_an_issue(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_marked_not_an_issue",
                reason="expected behavior",
                evidence_refs=["OCC-1111111111111111"],
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        assert proj.problems[0].status == "not_an_issue"

    def test_accepted_risk(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_assessment_confirmed",
                classification="product_bug",
                severity="low",
                reason="triaged",
                evidence_refs=["OCC-1111111111111111"],
            ),
            _mk_problem_event(
                3,
                "E3",
                "I3",
                PROB_ID,
                2,
                type="problem_risk_accepted",
                reason="low priority",
                evidence_refs=["OCC-1111111111111111"],
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        assert proj.problems[0].status == "accepted_risk"

    def test_reopened(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_marked_not_an_issue",
                reason="dismissed",
                evidence_refs=["OCC-1111111111111111"],
            ),
            _mk_problem_event(
                3,
                "E3",
                "I3",
                PROB_ID,
                2,
                type="problem_reopened",
                reason="still failing",
                evidence_refs=["OCC-2222222222222222"],
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        assert proj.problems[0].status == "detected"

    def test_regressed_reopens_resolved(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_verification_requested",
                verification_scope=["API-TEST-001"],
                linked_fix_disposition="PR-42",
                change_id="CH-002",
                batch_id="B-003",
            ),
            _mk_problem_event(
                3,
                "E3",
                "I3",
                PROB_ID,
                2,
                type="problem_resolved",
                resolved_at="2026-07-25T12:00:00Z",
                change_id="CH-002",
                batch_id="B-003",
                disposition="PR-42 merged",
                verification_scope=["API-TEST-001"],
            ),
            _mk_problem_event(
                4,
                "E4",
                "I4",
                PROB_ID,
                3,
                type="problem_regressed",
                occurrence_id="OCC-3333333333333333",
                change_id="CH-003",
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        p = proj.problems[0]
        assert p.status == "detected"
        assert p.resolution is None
        assert "OCC-3333333333333333" in p.occurrences
        assert p.last_seen.occurrence_id == "OCC-3333333333333333"

    def test_merged_keeps_source_resolves_as_alias(self) -> None:
        events = [
            _detected_event(1, PROB_ID),
            _detected_event(2, PROB_ID_B, "OCC-bbbbbbbbbbbbbbbb"),
            _mk_problem_event(
                3,
                "E3",
                "I3",
                PROB_ID,
                1,
                type="problem_merged",
                target_problem_id=PROB_ID_B,
                reason="duplicate",
                evidence_refs=["OCC-1111111111111111"],
                resolved_at="2026-07-25T13:00:00Z",
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        assert len(proj.problems) == 2
        source = next(p for p in proj.problems if p.problem_id == PROB_ID)
        target = next(p for p in proj.problems if p.problem_id == PROB_ID_B)
        assert source.status == "resolved"
        assert source.resolution is not None
        assert f"merged_into:{PROB_ID_B}" in source.resolution.disposition
        assert target.status == "detected"

    def test_merge_suggested_does_not_modify_problem(self) -> None:
        events = [
            _detected_event(1, PROB_ID),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_merge_suggested",
                source_occurrence_id="OCC-1111111111111111",
                source_change_id="CH-001",
                target_problem_id=PROB_ID_B,
                reason="possible match",
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        p = proj.problems[0]
        assert p.version == 1  # unchanged
        assert p.status == "detected"

    def test_stale_version_raises_projection_error(self) -> None:
        events = [
            _detected_event(1),
            # expected_problem_version=99 but current is 1
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                99,
                type="problem_occurrence_linked",
                occurrence_id="OCC-2222222222222222",
                change_id="CH-001",
                batch_id="B-002",
            ),
        ]
        with pytest.raises(ProjectionError, match="expected version 99"):
            project_problems(events)  # type: ignore[arg-type]

    def test_unknown_problem_id_raises(self) -> None:
        events = [
            _mk_problem_event(
                1,
                "E1",
                "I1",
                "PROB-unknownxxxxxxxx",
                1,
                type="problem_occurrence_linked",
                occurrence_id="OCC-0000000000000000",
                change_id="CH-001",
                batch_id="B-001",
            ),
        ]
        with pytest.raises(ProjectionError, match="unknown"):
            project_problems(events)  # type: ignore[arg-type]

    def test_multiple_problems_tracked_independently(self) -> None:
        events = [
            _detected_event(1, PROB_ID, "OCC-1111111111111111"),
            _detected_event(2, PROB_ID_B, "OCC-bbbbbbbbbbbbbbbb"),
            _mk_problem_event(
                3,
                "E3",
                "I3",
                PROB_ID,
                1,
                type="problem_marked_not_an_issue",
                reason="false positive",
                evidence_refs=["OCC-1111111111111111"],
            ),
        ]
        proj = project_problems(events)  # type: ignore[arg-type]
        by_id = {p.problem_id: p for p in proj.problems}
        assert by_id[PROB_ID].status == "not_an_issue"
        assert by_id[PROB_ID_B].status == "detected"

    def test_generated_at_from_last_event_ts(self) -> None:
        events = [_detected_event(1)]
        proj = project_problems(events)  # type: ignore[arg-type]
        assert "2026-07-25" in proj.generated_at


# ---------------------------------------------------------------------------
# project_review_queue
# ---------------------------------------------------------------------------


class TestProjectReviewQueue:
    def test_empty_events_returns_empty_queue(self) -> None:
        queue = project_review_queue([])
        assert queue.entries == []

    def test_merge_suggested_creates_entry(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_merge_suggested",
                source_occurrence_id="OCC-1111111111111111",
                source_change_id="CH-001",
                target_problem_id=PROB_ID_B,
                reason="semantic match",
            ),
        ]
        queue = project_review_queue(events)  # type: ignore[arg-type]
        assert len(queue.entries) == 1
        entry = queue.entries[0]
        assert entry.occurrence_id == "OCC-1111111111111111"
        assert entry.possible_problem_ids == [PROB_ID_B]
        assert entry.reason == "semantic match"

    def test_entry_id_is_deterministic(self) -> None:
        events = [
            _mk_problem_event(
                1,
                "EVT-FIXED",
                "IDEM-FIXED",
                PROB_ID,
                1,
                type="problem_merge_suggested",
                source_occurrence_id="OCC-1111111111111111",
                source_change_id="CH-001",
                target_problem_id=PROB_ID_B,
                reason="match",
            ),
        ]
        queue1 = project_review_queue(events)  # type: ignore[arg-type]
        queue2 = project_review_queue(events)  # type: ignore[arg-type]
        assert queue1.entries[0].entry_id == queue2.entries[0].entry_id

    def test_non_merge_events_ignored(self) -> None:
        events = [_detected_event(1)]
        queue = project_review_queue(events)  # type: ignore[arg-type]
        assert queue.entries == []


# ---------------------------------------------------------------------------
# dump_projection: byte-identical on double replay
# ---------------------------------------------------------------------------


class TestDumpProjection:
    def test_byte_identical_on_double_replay_change_issues(self) -> None:
        events = [
            _mk_change_event(
                1,
                "E1",
                "I1",
                type="observation_recorded",
                observation=OBS_1.model_dump(mode="json"),
            ),
            _mk_change_event(
                2,
                "E2",
                "I2",
                type="issue_analysis_completed",
                analysis_status=ANALYSIS_OK.model_dump(mode="json"),
            ),
        ]
        snap1 = project_change_issues(events)  # type: ignore[arg-type]
        snap2 = project_change_issues(events)  # type: ignore[arg-type]
        assert dump_projection(snap1) == dump_projection(snap2)

    def test_byte_identical_on_double_replay_problems(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_assessment_confirmed",
                classification="product_bug",
                severity="high",
                reason="confirmed",
                evidence_refs=["OCC-1111111111111111"],
            ),
        ]
        proj1 = project_problems(events)  # type: ignore[arg-type]
        proj2 = project_problems(events)  # type: ignore[arg-type]
        assert dump_projection(proj1) == dump_projection(proj2)

    def test_byte_identical_on_double_replay_queue(self) -> None:
        events = [
            _detected_event(1),
            _mk_problem_event(
                2,
                "E2",
                "I2",
                PROB_ID,
                1,
                type="problem_merge_suggested",
                source_occurrence_id="OCC-1111111111111111",
                source_change_id="CH-001",
                target_problem_id=PROB_ID_B,
                reason="match",
            ),
        ]
        q1 = project_review_queue(events)  # type: ignore[arg-type]
        q2 = project_review_queue(events)  # type: ignore[arg-type]
        assert dump_projection(q1) == dump_projection(q2)

    def test_dump_is_valid_json_with_trailing_newline(self) -> None:
        events = [_detected_event(1)]
        proj = project_problems(events)  # type: ignore[arg-type]
        raw = dump_projection(proj)
        assert raw.endswith(b"\n")
        parsed = json.loads(raw.decode("utf-8"))
        assert isinstance(parsed, dict)

    def test_dump_uses_sort_keys(self) -> None:
        """Keys must appear in sorted order in the output bytes."""
        events = [_detected_event(1)]
        proj = project_problems(events)  # type: ignore[arg-type]
        raw = dump_projection(proj).decode("utf-8")
        # Verify the first key in the output is alphabetically first
        parsed = json.loads(raw)
        raw_keys_order = [k for k in parsed.keys()]
        assert raw_keys_order == sorted(raw_keys_order)

    def test_dump_compact_no_space_after_colon(self) -> None:
        events = [_detected_event(1)]
        proj = project_problems(events)  # type: ignore[arg-type]
        raw = dump_projection(proj).decode("utf-8")
        # Compact separators mean no ": " or ", " in output
        assert ": " not in raw
        assert ", " not in raw
