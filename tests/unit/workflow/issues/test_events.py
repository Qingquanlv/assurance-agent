"""Tests for strict Change Issue and Project Problem event adapters.

Covers:
- Every event type round-trips through the TypeAdapter
- extra="forbid": unknown fields raise ValidationError
- Unknown type discriminator value fails the whole read
- Readers enforce contiguous seq, duplicate event_id, duplicate idempotency_key
- Blank holes and malformed JSON fail the whole read
- Non-dict JSON lines fail
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

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
    IssueAnalysisCompletedEvent,
    IssueAnalysisFailedEvent,
    LedgerIntegrityError,
    ObservationRecordedEvent,
    OccurrenceDetectedEvent,
    OccurrenceLinkedEvent,
    ProblemAssessmentConfirmedEvent,
    ProblemDetectedEvent,
    ProblemMergedEvent,
    ProblemMergeSuggestedEvent,
    ProblemMarkedNotAnIssueEvent,
    ProblemOccurrenceLinkedEvent,
    ProblemRegressedEvent,
    ProblemReopenedEvent,
    ProblemResolvedEvent,
    ProblemRiskAcceptedEvent,
    ProblemVerificationRequestedEvent,
    ProblemWorkStartedEvent,
    ProjectSyncPendingEvent,
    read_change_issue_events,
    read_problem_events,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

OBSERVATION = Observation(
    observation_id="OBS-1a2b3c4d5e6f7890",
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
    signature="http_500_on_empty_name",
    observed_at="2026-07-25T10:00:00Z",
)

ANALYSIS_STATUS = IssueAnalysisStatus(
    schema_version="1.0",
    change_id="CH-001",
    batch_id="B-001",
    status="completed",
    evidence_bundle_digest="sha256:aabbccdd",
    candidate_count=2,
)

ANALYSIS_STATUS_FAILED = IssueAnalysisStatus(
    schema_version="1.0",
    change_id="CH-001",
    batch_id="B-001",
    status="failed",
    evidence_bundle_digest="sha256:aabbccdd",
    candidate_count=0,
    reason="timeout",
    retryable=True,
)

OCCURRENCE = IssueOccurrence(
    occurrence_id="OCC-aabbccddeeff0011",
    change_id="CH-001",
    batch_id="B-001",
    observation_ids=["OBS-1a2b3c4d5e6f7890"],
    problem_id="PROB-deadbeef12345678",
    provisional_assessment=ProvisionalAssessment(
        classification="product_bug",
        severity="high",
        authority="llm_provisional",
        root_cause_hypothesis="Null pointer on empty name",
    ),
    analysis=OccurrenceAnalysis(
        evidence_bundle_digest="sha256:aabbccdd",
        analyzer="aa-issue-analyzer",
        prompt_version="v1",
        candidate_digest="sha256:11223344",
    ),
)

FINGERPRINT = ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)

_BASE_CHANGE_ENVELOPE = {
    "schema_version": "1.0",
    "seq": 1,
    "event_id": "EVT-0001",
    "idempotency_key": "IDEM-0001",
    "ts": "2026-07-25T10:00:00Z",
    "evidence_digest": "sha256:aabbccdd",
    "change_id": "CH-001",
    "batch_id": "B-001",
}

_BASE_PROBLEM_ENVELOPE = {
    "schema_version": "1.0",
    "seq": 1,
    "event_id": "EVT-0001",
    "idempotency_key": "IDEM-0001",
    "ts": "2026-07-25T10:00:00Z",
    "evidence_digest": "sha256:aabbccdd",
    "problem_id": "PROB-deadbeef12345678",
    "expected_problem_version": 0,
}


def _change_event_data(**overrides: object) -> dict:
    return {**_BASE_CHANGE_ENVELOPE, **overrides}


def _problem_event_data(**overrides: object) -> dict:
    return {**_BASE_PROBLEM_ENVELOPE, **overrides}


# ---------------------------------------------------------------------------
# Change Issue event type coverage
# ---------------------------------------------------------------------------


class TestObservationRecordedEvent:
    def test_round_trip(self) -> None:
        data = _change_event_data(
            type="observation_recorded",
            observation=OBSERVATION.model_dump(mode="json"),
        )
        event = CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ObservationRecordedEvent)
        assert event.observation.observation_id == OBSERVATION.observation_id

    def test_rejects_unknown_field(self) -> None:
        data = _change_event_data(
            type="observation_recorded",
            observation=OBSERVATION.model_dump(mode="json"),
            extra_field="bad",
        )
        with pytest.raises(ValidationError):
            CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)


class TestIssueAnalysisCompletedEvent:
    def test_round_trip(self) -> None:
        data = _change_event_data(
            type="issue_analysis_completed",
            analysis_status=ANALYSIS_STATUS.model_dump(mode="json"),
        )
        event = CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, IssueAnalysisCompletedEvent)
        assert event.analysis_status.candidate_count == 2

    def test_rejects_unknown_field(self) -> None:
        data = _change_event_data(
            type="issue_analysis_completed",
            analysis_status=ANALYSIS_STATUS.model_dump(mode="json"),
            bogus="x",
        )
        with pytest.raises(ValidationError):
            CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)


class TestIssueAnalysisFailedEvent:
    def test_round_trip(self) -> None:
        data = _change_event_data(
            type="issue_analysis_failed",
            analysis_status=ANALYSIS_STATUS_FAILED.model_dump(mode="json"),
        )
        event = CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, IssueAnalysisFailedEvent)
        assert event.analysis_status.reason == "timeout"


class TestOccurrenceDetectedEvent:
    def test_round_trip(self) -> None:
        data = _change_event_data(
            type="occurrence_detected",
            occurrence=OCCURRENCE.model_dump(mode="json"),
        )
        event = CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, OccurrenceDetectedEvent)
        assert event.occurrence.occurrence_id == OCCURRENCE.occurrence_id


class TestOccurrenceLinkedEvent:
    def test_round_trip(self) -> None:
        data = _change_event_data(
            type="occurrence_linked",
            occurrence=OCCURRENCE.model_dump(mode="json"),
        )
        event = CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, OccurrenceLinkedEvent)


class TestProjectSyncPendingEvent:
    def test_round_trip(self) -> None:
        data = _change_event_data(
            type="project_sync_pending",
            candidate_digest="sha256:11223344",
        )
        event = CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProjectSyncPendingEvent)
        assert event.candidate_digest == "sha256:11223344"


# ---------------------------------------------------------------------------
# Project Problem event type coverage
# ---------------------------------------------------------------------------


class TestProblemDetectedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_detected",
            occurrence_id="OCC-aabbccddeeff0011",
            change_id="CH-001",
            batch_id="B-001",
            fingerprint=FINGERPRINT.model_dump(mode="json"),
            title="HTTP 500 on empty name",
            classification="product_bug",
            severity="high",
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemDetectedEvent)
        assert event.expected_problem_version == 0

    def test_rejects_nonzero_expected_version(self) -> None:
        data = _problem_event_data(
            type="problem_detected",
            expected_problem_version=1,  # must be 0
            occurrence_id="OCC-aabbccddeeff0011",
            change_id="CH-001",
            batch_id="B-001",
            fingerprint=FINGERPRINT.model_dump(mode="json"),
            title="T",
            classification="product_bug",
            severity="high",
        )
        with pytest.raises(ValidationError):
            PROBLEM_EVENT_ADAPTER.validate_python(data)

    def test_rejects_unknown_field(self) -> None:
        data = _problem_event_data(
            type="problem_detected",
            occurrence_id="OCC-aabbccddeeff0011",
            change_id="CH-001",
            batch_id="B-001",
            fingerprint=FINGERPRINT.model_dump(mode="json"),
            title="T",
            classification="product_bug",
            severity="high",
            extra_key="bad",
        )
        with pytest.raises(ValidationError):
            PROBLEM_EVENT_ADAPTER.validate_python(data)


class TestProblemOccurrenceLinkedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_occurrence_linked",
            expected_problem_version=1,
            occurrence_id="OCC-aabbccddeeff0011",
            change_id="CH-001",
            batch_id="B-002",
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemOccurrenceLinkedEvent)


class TestProblemAssessmentConfirmedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_assessment_confirmed",
            expected_problem_version=1,
            classification="product_bug",
            severity="critical",
            reason="confirmed via test evidence",
            evidence_refs=["OCC-aabbccddeeff0011"],
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemAssessmentConfirmedEvent)


class TestProblemWorkStartedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_work_started",
            expected_problem_version=2,
            reason="assigned to dev",
            evidence_refs=["OCC-001"],
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemWorkStartedEvent)


class TestProblemVerificationRequestedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_verification_requested",
            expected_problem_version=3,
            verification_scope=["API-TEST-001"],
            linked_fix_disposition="PR-42",
            change_id="CH-002",
            batch_id="B-003",
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemVerificationRequestedEvent)


class TestProblemResolvedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_resolved",
            expected_problem_version=4,
            resolved_at="2026-07-25T12:00:00Z",
            change_id="CH-002",
            batch_id="B-004",
            disposition="PR-42 merged",
            verification_scope=["API-TEST-001"],
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemResolvedEvent)


class TestProblemMarkedNotAnIssueEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_marked_not_an_issue",
            expected_problem_version=1,
            reason="expected behavior",
            evidence_refs=["OCC-001"],
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemMarkedNotAnIssueEvent)


class TestProblemRiskAcceptedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_risk_accepted",
            expected_problem_version=2,
            reason="low impact, accepted",
            evidence_refs=["OCC-001"],
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemRiskAcceptedEvent)


class TestProblemReopenedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_reopened",
            expected_problem_version=2,
            reason="still failing",
            evidence_refs=["OCC-002"],
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemReopenedEvent)


class TestProblemRegressedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_regressed",
            expected_problem_version=5,
            occurrence_id="OCC-new001",
            change_id="CH-003",
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemRegressedEvent)


class TestProblemMergeSuggestedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_merge_suggested",
            expected_problem_version=1,
            source_occurrence_id="OCC-aabbccddeeff0011",
            source_change_id="CH-001",
            target_problem_id="PROB-ffffffffffffffff",
            reason="semantic match",
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemMergeSuggestedEvent)
        assert event.candidate_id is None


class TestProblemMergedEvent:
    def test_round_trip(self) -> None:
        data = _problem_event_data(
            type="problem_merged",
            expected_problem_version=2,
            target_problem_id="PROB-ffffffffffffffff",
            reason="duplicate fingerprint confirmed",
            evidence_refs=["OCC-aabbccddeeff0011"],
            resolved_at="2026-07-25T13:00:00Z",
        )
        event = PROBLEM_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ProblemMergedEvent)


# ---------------------------------------------------------------------------
# Unknown type and extra field rejection
# ---------------------------------------------------------------------------


def test_change_event_unknown_type_fails() -> None:
    data = _change_event_data(type="totally_unknown")
    with pytest.raises(ValidationError):
        CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)


def test_problem_event_unknown_type_fails() -> None:
    data = _problem_event_data(type="nonexistent_event", expected_problem_version=0)
    with pytest.raises(ValidationError):
        PROBLEM_EVENT_ADAPTER.validate_python(data)


def test_change_event_extra_field_fails() -> None:
    data = _change_event_data(
        type="project_sync_pending",
        candidate_digest="sha256:aabbccdd",
        rogue_field=True,
    )
    with pytest.raises(ValidationError):
        CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)


# ---------------------------------------------------------------------------
# Reader: file not found → empty list
# ---------------------------------------------------------------------------


def test_read_change_issue_events_missing_file(tmp_path: Path) -> None:
    result = read_change_issue_events(tmp_path / "issues" / "events.jsonl")
    assert result == []


def test_read_problem_events_missing_file(tmp_path: Path) -> None:
    result = read_problem_events(tmp_path / "qa" / "issues" / "events.jsonl")
    assert result == []


# ---------------------------------------------------------------------------
# Reader: JSONL helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _obs_event_dict(seq: int, event_id: str, idem: str) -> dict:
    return {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id,
        "idempotency_key": idem,
        "ts": "2026-07-25T10:00:00Z",
        "evidence_digest": "sha256:aabbccdd",
        "type": "observation_recorded",
        "change_id": "CH-001",
        "batch_id": "B-001",
        "observation": OBSERVATION.model_dump(mode="json"),
    }


class TestReadChangeIssueEvents:
    def test_reads_valid_single_event(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_jsonl(p, [_obs_event_dict(1, "EVT-1", "IDEM-1")])
        events = read_change_issue_events(p)
        assert len(events) == 1
        assert isinstance(events[0], ObservationRecordedEvent)

    def test_reads_multiple_events(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_jsonl(
            p,
            [
                _obs_event_dict(1, "EVT-1", "IDEM-1"),
                _obs_event_dict(2, "EVT-2", "IDEM-2"),
            ],
        )
        events = read_change_issue_events(p)
        assert len(events) == 2

    def test_rejects_blank_hole(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        content = (
            json.dumps(_obs_event_dict(1, "EVT-1", "IDEM-1"))
            + "\n\n"
            + json.dumps(_obs_event_dict(2, "EVT-2", "IDEM-2"))
            + "\n"
        )
        p.write_text(content, encoding="utf-8")
        with pytest.raises(LedgerIntegrityError, match="blank hole"):
            read_change_issue_events(p)

    def test_rejects_bad_json(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text("not json\n", encoding="utf-8")
        with pytest.raises(LedgerIntegrityError, match="invalid JSON"):
            read_change_issue_events(p)

    def test_rejects_non_dict_json(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text("[1, 2, 3]\n", encoding="utf-8")
        with pytest.raises(LedgerIntegrityError, match="not a JSON object"):
            read_change_issue_events(p)

    def test_rejects_seq_gap(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_jsonl(
            p,
            [
                _obs_event_dict(1, "EVT-1", "IDEM-1"),
                _obs_event_dict(3, "EVT-3", "IDEM-3"),  # gap: seq 2 missing
            ],
        )
        with pytest.raises(LedgerIntegrityError, match="expected seq 2"):
            read_change_issue_events(p)

    def test_rejects_seq_starting_wrong(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_jsonl(p, [_obs_event_dict(0, "EVT-1", "IDEM-1")])
        with pytest.raises(LedgerIntegrityError, match="expected seq 1"):
            read_change_issue_events(p)

    def test_rejects_duplicate_event_id(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_jsonl(
            p,
            [
                _obs_event_dict(1, "EVT-SAME", "IDEM-1"),
                _obs_event_dict(2, "EVT-SAME", "IDEM-2"),
            ],
        )
        with pytest.raises(LedgerIntegrityError, match="duplicate event_id"):
            read_change_issue_events(p)

    def test_rejects_duplicate_idempotency_key(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_jsonl(
            p,
            [
                _obs_event_dict(1, "EVT-1", "IDEM-SAME"),
                _obs_event_dict(2, "EVT-2", "IDEM-SAME"),
            ],
        )
        with pytest.raises(LedgerIntegrityError, match="duplicate idempotency_key"):
            read_change_issue_events(p)

    def test_rejects_unknown_event_type(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        bad_event = {**_obs_event_dict(1, "EVT-1", "IDEM-1"), "type": "unknown_type"}
        _write_jsonl(p, [bad_event])
        with pytest.raises(LedgerIntegrityError, match="invalid event"):
            read_change_issue_events(p)

    def test_rejects_unknown_field_in_event(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        bad_event = {**_obs_event_dict(1, "EVT-1", "IDEM-1"), "surprise_field": True}
        _write_jsonl(p, [bad_event])
        with pytest.raises(LedgerIntegrityError, match="invalid event"):
            read_change_issue_events(p)
