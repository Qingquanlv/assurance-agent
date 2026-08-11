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

import hashlib
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
from assurance_agent.evidence.issue_identity import (
    ObservationIdentityInput,
    event_id,
    observation_id,
    occurrence_id,
    problem_id,
    recomputable_issue_event_idempotency_key,
)
from assurance_agent.evidence.issue_replay import (
    IssueLedgerIntegrityError,
    IssueLedgerMissingError,
    load_change_issue_ledger,
    load_problem_ledger,
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


def _obs_id(*, json_pointer: str = "/cases/0") -> str:
    return observation_id(
        ObservationIdentityInput(
            change_id="CH-001",
            batch_id="B-001",
            kind="test_failure",
            target="api",
            case_id=None,
            source_artifact="execution/runs/B-001/api-result.json",
            source_json_pointer=json_pointer,
            signature="http_500_on_empty_name",
        )
    )


CANDIDATE_DIGEST = "sha256:11223344"
OCCURRENCE_ID = occurrence_id("CH-001", "B-001", CANDIDATE_DIGEST)
FINGERPRINT = ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)
PROBLEM_ID = problem_id(FINGERPRINT)

OBSERVATION = Observation(
    observation_id=_obs_id(),
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

OBSERVATION_2 = Observation(
    observation_id=_obs_id(json_pointer="/cases/1"),
    change_id="CH-001",
    batch_id="B-001",
    kind="test_failure",
    target="api",
    case_id=None,
    source=ObservationSource(
        artifact="execution/runs/B-001/api-result.json",
        json_pointer="/cases/1",
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
    candidate_digest="sha256:batch",
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
    occurrence_id=OCCURRENCE_ID,
    change_id="CH-001",
    batch_id="B-001",
    observation_ids=[OBSERVATION.observation_id],
    problem_id=PROBLEM_ID,
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
        candidate_digest=CANDIDATE_DIGEST,
    ),
)

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
    "problem_id": PROBLEM_ID,
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


def _evidence_refs_digest(evidence_refs: list[str]) -> str:
    canonical = json.dumps(sorted(evidence_refs), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _with_identity(payload: dict, *, mutate: dict | None = None) -> dict:
    """Apply mutate, then ensure event_id matches the (possibly mutated) key."""
    if mutate:
        payload = {**payload, **mutate}
    payload["event_id"] = event_id(payload["idempotency_key"])
    return payload


def _obs_event_dict(
    seq: int,
    *,
    observation: Observation = OBSERVATION,
    mutate: dict | None = None,
) -> dict:
    idem = f"observation_recorded:CH-001:B-001:{observation.observation_id}"
    payload = {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id(idem),
        "idempotency_key": idem,
        "ts": "2026-07-25T10:00:00Z",
        "evidence_digest": "sha256:aabbccdd",
        "type": "observation_recorded",
        "change_id": "CH-001",
        "batch_id": "B-001",
        "observation": observation.model_dump(mode="json"),
    }
    if mutate:
        payload.update(mutate)
    return payload


def _analysis_completed_event_dict(seq: int = 1, *, mutate: dict | None = None) -> dict:
    digest = ANALYSIS_STATUS.candidate_digest
    idem = f"issue_analysis_completed:CH-001:B-001:{digest}"
    return _with_identity(
        {
            "schema_version": "1.0",
            "seq": seq,
            "event_id": event_id(idem),
            "idempotency_key": idem,
            "ts": "2026-07-25T10:00:00Z",
            "evidence_digest": ANALYSIS_STATUS.evidence_bundle_digest,
            "type": "issue_analysis_completed",
            "change_id": "CH-001",
            "batch_id": "B-001",
            "analysis_status": ANALYSIS_STATUS.model_dump(mode="json"),
        },
        mutate=mutate,
    )


def _analysis_failed_event_dict(seq: int = 1, *, mutate: dict | None = None) -> dict:
    digest = ANALYSIS_STATUS_FAILED.evidence_bundle_digest
    idem = f"issue_analysis_failed:CH-001:B-001:{digest}"
    return _with_identity(
        {
            "schema_version": "1.0",
            "seq": seq,
            "event_id": event_id(idem),
            "idempotency_key": idem,
            "ts": "2026-07-25T10:00:00Z",
            "evidence_digest": digest,
            "type": "issue_analysis_failed",
            "change_id": "CH-001",
            "batch_id": "B-001",
            "analysis_status": ANALYSIS_STATUS_FAILED.model_dump(mode="json"),
        },
        mutate=mutate,
    )


def _occurrence_event_dict(
    event_type: str,
    seq: int = 1,
    *,
    mutate: dict | None = None,
) -> dict:
    idem = f"{event_type}:CH-001:B-001:{CANDIDATE_DIGEST}"
    return _with_identity(
        {
            "schema_version": "1.0",
            "seq": seq,
            "event_id": event_id(idem),
            "idempotency_key": idem,
            "ts": "2026-07-25T10:00:00Z",
            "evidence_digest": OCCURRENCE.analysis.evidence_bundle_digest,
            "type": event_type,
            "change_id": "CH-001",
            "batch_id": "B-001",
            "occurrence": OCCURRENCE.model_dump(mode="json"),
        },
        mutate=mutate,
    )


def _sync_event_dict(seq: int = 1, *, mutate: dict | None = None) -> dict:
    idem = f"project_sync_pending:CH-001:B-001:{CANDIDATE_DIGEST}"
    return _with_identity(
        {
            "schema_version": "1.0",
            "seq": seq,
            "event_id": event_id(idem),
            "idempotency_key": idem,
            "ts": "2026-07-25T10:00:00Z",
            "evidence_digest": "sha256:aabbccdd",
            "type": "project_sync_pending",
            "change_id": "CH-001",
            "batch_id": "B-001",
            "candidate_digest": CANDIDATE_DIGEST,
        },
        mutate=mutate,
    )


def _problem_detected_event_dict(seq: int = 1, *, mutate: dict | None = None) -> dict:
    # Not recomputable; use a stable writer-shaped key.
    idem = f"problem_detected:{PROBLEM_ID}:CH-001:B-001:{CANDIDATE_DIGEST}"
    return _with_identity(
        {
            "schema_version": "1.0",
            "seq": seq,
            "event_id": event_id(idem),
            "idempotency_key": idem,
            "ts": "2026-07-25T10:00:00Z",
            "evidence_digest": "sha256:aabbccdd",
            "type": "problem_detected",
            "problem_id": PROBLEM_ID,
            "expected_problem_version": 0,
            "occurrence_id": OCCURRENCE_ID,
            "change_id": "CH-001",
            "batch_id": "B-001",
            "fingerprint": FINGERPRINT.model_dump(mode="json"),
            "title": "Endpoint fails",
            "classification": "product_bug",
            "severity": "high",
        },
        mutate=mutate,
    )


def _problem_resolved_event_dict(seq: int = 1, *, mutate: dict | None = None) -> dict:
    digest = "sha256:aabbccdd"
    idem = f"problem_resolved:{PROBLEM_ID}:B-004:{digest}"
    return _with_identity(
        {
            "schema_version": "1.0",
            "seq": seq,
            "event_id": event_id(idem),
            "idempotency_key": idem,
            "ts": "2026-07-25T10:00:00Z",
            "evidence_digest": digest,
            "type": "problem_resolved",
            "problem_id": PROBLEM_ID,
            "expected_problem_version": 4,
            "resolved_at": "2026-07-25T12:00:00Z",
            "change_id": "CH-002",
            "batch_id": "B-004",
            "disposition": "PR-42 merged",
            "verification_scope": ["API-TEST-001"],
        },
        mutate=mutate,
    )


def _review_event_dict(
    event_type: str,
    *,
    seq: int = 1,
    expected_problem_version: int = 1,
    mutate: dict | None = None,
) -> dict:
    evidence_refs = ["ref-a"]
    digest = _evidence_refs_digest(evidence_refs)
    key_prefix = {
        "problem_assessment_confirmed": "review:confirm_assessment",
        "problem_marked_not_an_issue": "review:mark_not_an_issue",
        "problem_risk_accepted": "review:accept_risk",
        "problem_work_started": "review:start_work",
        "problem_reopened": "review:reopen",
        "problem_merged": "review:merge",
        "problem_verification_requested": "review:submit_resolution",
    }[event_type]
    if event_type == "problem_merged":
        target = "PROB-" + "b" * 16
        idem = f"{key_prefix}:{PROBLEM_ID}:{expected_problem_version}:{target}:{digest}"
        body: dict = {
            "target_problem_id": target,
            "reason": "duplicate",
            "evidence_refs": evidence_refs,
            "resolved_at": "2026-07-25T12:00:00Z",
        }
    elif event_type == "problem_verification_requested":
        idem = f"{key_prefix}:{PROBLEM_ID}:{expected_problem_version}:CH-002:B-003:{digest}"
        body = {
            "verification_scope": ["API-TEST-001"],
            "linked_fix_disposition": "PR-42",
            "change_id": "CH-002",
            "batch_id": "B-003",
        }
    elif event_type == "problem_assessment_confirmed":
        idem = f"{key_prefix}:{PROBLEM_ID}:{expected_problem_version}:{digest}"
        body = {
            "classification": "product_bug",
            "severity": "critical",
            "reason": "confirmed",
            "evidence_refs": evidence_refs,
        }
    else:
        idem = f"{key_prefix}:{PROBLEM_ID}:{expected_problem_version}:{digest}"
        body = {"reason": "review action", "evidence_refs": evidence_refs}
    payload = {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id(idem),
        "idempotency_key": idem,
        "ts": "2026-07-25T10:00:00Z",
        "evidence_digest": digest,
        "type": event_type,
        "problem_id": PROBLEM_ID,
        "expected_problem_version": expected_problem_version,
        **body,
    }
    return _with_identity(payload, mutate=mutate)


class TestReadChangeIssueEvents:
    def test_reads_valid_single_event(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_jsonl(p, [_obs_event_dict(1)])
        events = read_change_issue_events(p)
        assert len(events) == 1
        assert isinstance(events[0], ObservationRecordedEvent)

    def test_reads_multiple_events(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_jsonl(
            p,
            [
                _obs_event_dict(1, observation=OBSERVATION),
                _obs_event_dict(2, observation=OBSERVATION_2),
            ],
        )
        events = read_change_issue_events(p)
        assert len(events) == 2

    def test_rejects_blank_hole(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        content = (
            json.dumps(_obs_event_dict(1, observation=OBSERVATION))
            + "\n\n"
            + json.dumps(_obs_event_dict(2, observation=OBSERVATION_2))
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
                _obs_event_dict(1, observation=OBSERVATION),
                _obs_event_dict(3, observation=OBSERVATION_2),  # gap: seq 2 missing
            ],
        )
        with pytest.raises(LedgerIntegrityError, match="expected seq 2"):
            read_change_issue_events(p)

    def test_rejects_seq_starting_wrong(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_jsonl(p, [_obs_event_dict(0)])
        with pytest.raises(LedgerIntegrityError, match="expected seq 1"):
            read_change_issue_events(p)

    def test_rejects_duplicate_event_id(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        first = _obs_event_dict(1, observation=OBSERVATION)
        second = _obs_event_dict(2, observation=OBSERVATION_2)
        second["event_id"] = first["event_id"]
        _write_jsonl(p, [first, second])
        with pytest.raises(LedgerIntegrityError, match="duplicate event_id"):
            read_change_issue_events(p)

    def test_rejects_duplicate_idempotency_key(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        first = _obs_event_dict(1, observation=OBSERVATION)
        second = _obs_event_dict(2, observation=OBSERVATION_2)
        # Keep a distinct event_id so the duplicate-key check runs before identity.
        second["idempotency_key"] = first["idempotency_key"]
        _write_jsonl(p, [first, second])
        with pytest.raises(LedgerIntegrityError, match="duplicate idempotency_key"):
            read_change_issue_events(p)

    def test_rejects_unknown_event_type(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        bad_event = {**_obs_event_dict(1), "type": "unknown_type"}
        _write_jsonl(p, [bad_event])
        with pytest.raises(LedgerIntegrityError, match="invalid event"):
            read_change_issue_events(p)

    def test_rejects_unknown_field_in_event(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        bad_event = {**_obs_event_dict(1), "surprise_field": True}
        _write_jsonl(p, [bad_event])
        with pytest.raises(LedgerIntegrityError, match="invalid event"):
            read_change_issue_events(p)

    def test_missing_path_returns_empty_for_mutation_compatibility(self, tmp_path: Path) -> None:
        assert read_problem_events(tmp_path / "missing.jsonl") == []

    def test_authority_loader_distinguishes_missing_from_empty(self, tmp_path: Path) -> None:
        with pytest.raises(IssueLedgerMissingError):
            load_problem_ledger(tmp_path / "missing.jsonl")
        empty = tmp_path / "empty.jsonl"
        empty.write_bytes(b"")
        assert load_problem_ledger(empty) == ()
        assert load_change_issue_ledger(empty) == ()

    def test_strict_change_replay_rejects_forged_event_id(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_jsonl(path, [_obs_event_dict(1, mutate={"event_id": "EVT-forged"})])
        with pytest.raises(IssueLedgerIntegrityError, match="event_id"):
            load_change_issue_ledger(path)

    def test_strict_change_replay_rejects_envelope_nested_change_id_mismatch(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        nested = OBSERVATION.model_dump(mode="json")
        nested["change_id"] = "CH-OTHER"
        _write_jsonl(path, [_obs_event_dict(1, mutate={"observation": nested})])
        with pytest.raises(IssueLedgerIntegrityError, match="change_id"):
            load_change_issue_ledger(path)

    def test_strict_change_replay_rejects_envelope_nested_batch_id_mismatch(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        nested = ANALYSIS_STATUS.model_dump(mode="json")
        nested["batch_id"] = "B-OTHER"
        _write_jsonl(path, [_analysis_completed_event_dict(mutate={"analysis_status": nested})])
        with pytest.raises(IssueLedgerIntegrityError, match="batch_id"):
            load_change_issue_ledger(path)

    def test_strict_change_replay_rejects_nested_evidence_digest_mismatch(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        nested = OCCURRENCE.model_dump(mode="json")
        nested["analysis"]["evidence_bundle_digest"] = "sha256:forged-digest"
        _write_jsonl(
            path,
            [_occurrence_event_dict("occurrence_detected", mutate={"occurrence": nested})],
        )
        with pytest.raises(IssueLedgerIntegrityError, match="evidence_digest"):
            load_change_issue_ledger(path)

    def test_strict_change_replay_rejects_forged_observation_id(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        nested = OBSERVATION.model_dump(mode="json")
        nested["observation_id"] = "OBS-forged00000001"
        # Keep the recomputable key consistent with the forged nested id so the
        # nested observation_id check is the rejection path.
        forged_key = "observation_recorded:CH-001:B-001:OBS-forged00000001"
        _write_jsonl(
            path,
            [
                _obs_event_dict(
                    1,
                    mutate={
                        "observation": nested,
                        "idempotency_key": forged_key,
                        "event_id": event_id(forged_key),
                    },
                )
            ],
        )
        with pytest.raises(IssueLedgerIntegrityError, match="observation_id"):
            load_change_issue_ledger(path)

    def test_strict_change_replay_rejects_forged_occurrence_id(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        nested = OCCURRENCE.model_dump(mode="json")
        nested["occurrence_id"] = "OCC-forged00000001"
        _write_jsonl(
            path,
            [_occurrence_event_dict("occurrence_detected", mutate={"occurrence": nested})],
        )
        with pytest.raises(IssueLedgerIntegrityError, match="occurrence_id"):
            load_change_issue_ledger(path)

    def test_strict_problem_replay_rejects_expected_problem_version(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_jsonl(
            path,
            [_problem_detected_event_dict(mutate={"expected_problem_version": 1})],
        )
        with pytest.raises(IssueLedgerIntegrityError, match="expected_problem_version"):
            load_problem_ledger(path)

    def test_strict_problem_replay_rejects_problem_id_mismatch(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_jsonl(
            path,
            [_problem_detected_event_dict(mutate={"problem_id": "PROB-" + "f" * 16})],
        )
        with pytest.raises(IssueLedgerIntegrityError, match="problem_id"):
            load_problem_ledger(path)

    @pytest.mark.parametrize(
        ("builder", "loader"),
        [
            (lambda: _obs_event_dict(1), load_change_issue_ledger),
            (lambda: _analysis_completed_event_dict(), load_change_issue_ledger),
            (lambda: _analysis_failed_event_dict(), load_change_issue_ledger),
            (lambda: _occurrence_event_dict("occurrence_detected"), load_change_issue_ledger),
            (lambda: _occurrence_event_dict("occurrence_linked"), load_change_issue_ledger),
            (lambda: _sync_event_dict(), load_change_issue_ledger),
            (lambda: _problem_resolved_event_dict(), load_problem_ledger),
            (
                lambda: _review_event_dict("problem_assessment_confirmed"),
                load_problem_ledger,
            ),
            (
                lambda: _review_event_dict("problem_marked_not_an_issue"),
                load_problem_ledger,
            ),
            (lambda: _review_event_dict("problem_risk_accepted"), load_problem_ledger),
            (lambda: _review_event_dict("problem_work_started"), load_problem_ledger),
            (lambda: _review_event_dict("problem_reopened"), load_problem_ledger),
            (lambda: _review_event_dict("problem_merged"), load_problem_ledger),
            (
                lambda: _review_event_dict("problem_verification_requested"),
                load_problem_ledger,
            ),
        ],
        ids=[
            "observation_recorded",
            "issue_analysis_completed",
            "issue_analysis_failed",
            "occurrence_detected",
            "occurrence_linked",
            "project_sync_pending",
            "problem_resolved",
            "problem_assessment_confirmed",
            "problem_marked_not_an_issue",
            "problem_risk_accepted",
            "problem_work_started",
            "problem_reopened",
            "problem_merged",
            "problem_verification_requested",
        ],
    )
    def test_strict_replay_rejects_forged_recomputable_idempotency_key(
        self,
        tmp_path: Path,
        builder,
        loader,
    ) -> None:
        path = tmp_path / "events.jsonl"
        payload = builder()
        forged = {**payload, "idempotency_key": "forged-recomputable-key"}
        forged["event_id"] = event_id(forged["idempotency_key"])
        _write_jsonl(path, [forged])
        with pytest.raises(IssueLedgerIntegrityError, match="idempotency_key"):
            loader(path)

    def test_strict_change_replay_rejects_duplicate_defining_observation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same observation_id implies the same recomputable key; disable that check so
        # the dedicated defining-observation uniqueness path is exercised.
        monkeypatch.setattr(
            "assurance_agent.evidence.issue_replay.recomputable_issue_event_idempotency_key",
            lambda _event: None,
        )
        path = tmp_path / "events.jsonl"
        first = _obs_event_dict(1, observation=OBSERVATION)
        second = _obs_event_dict(2, observation=OBSERVATION)
        second["idempotency_key"] = "distinct-key-same-defining-observation"
        second["event_id"] = event_id(second["idempotency_key"])
        _write_jsonl(path, [first, second])
        with pytest.raises(IssueLedgerIntegrityError, match="duplicate defining observation_id"):
            load_change_issue_ledger(path)

    def test_strict_change_replay_rejects_duplicate_defining_occurrence(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_jsonl(
            path,
            [
                _occurrence_event_dict("occurrence_detected", seq=1),
                _occurrence_event_dict("occurrence_linked", seq=2),
            ],
        )
        with pytest.raises(IssueLedgerIntegrityError, match="occurrence_id"):
            load_change_issue_ledger(path)

    def test_problem_reference_to_defined_occurrence_remains_legal(self, tmp_path: Path) -> None:
        """Problem events may reference an occurrence_id; they do not redefine it."""
        change_path = tmp_path / "change.jsonl"
        problem_path = tmp_path / "problem.jsonl"
        detected_key = f"occurrence_detected:CH-001:B-001:{CANDIDATE_DIGEST}"
        _write_jsonl(
            change_path,
            [
                {
                    "schema_version": "1.0",
                    "seq": 1,
                    "event_id": event_id(detected_key),
                    "idempotency_key": detected_key,
                    "ts": "2026-07-25T10:00:00Z",
                    "evidence_digest": "sha256:aabbccdd",
                    "type": "occurrence_detected",
                    "change_id": "CH-001",
                    "batch_id": "B-001",
                    "occurrence": OCCURRENCE.model_dump(mode="json"),
                }
            ],
        )
        # problem_detected omits per-candidate digest → recomputable key is None.
        problem_key = f"problem_detected:{PROBLEM_ID}:CH-001:B-001:{CANDIDATE_DIGEST}"
        _write_jsonl(
            problem_path,
            [
                {
                    "schema_version": "1.0",
                    "seq": 1,
                    "event_id": event_id(problem_key),
                    "idempotency_key": problem_key,
                    "ts": "2026-07-25T10:00:00Z",
                    "evidence_digest": "sha256:aabbccdd",
                    "type": "problem_detected",
                    "problem_id": PROBLEM_ID,
                    "expected_problem_version": 0,
                    "occurrence_id": OCCURRENCE_ID,
                    "change_id": "CH-001",
                    "batch_id": "B-001",
                    "fingerprint": FINGERPRINT.model_dump(mode="json"),
                    "title": "Endpoint fails",
                    "classification": "product_bug",
                    "severity": "high",
                }
            ],
        )
        assert len(load_change_issue_ledger(change_path)) == 1
        assert len(load_problem_ledger(problem_path)) == 1

    def test_legacy_merge_suggested_is_not_recomputable(self) -> None:
        event = PROBLEM_EVENT_ADAPTER.validate_python(
            _problem_event_data(
                type="problem_merge_suggested",
                source_occurrence_id=OCCURRENCE_ID,
                source_change_id="CH-001",
                target_problem_id="PROB-other",
                reason="possible match",
                problem_id=PROBLEM_ID,
                expected_problem_version=1,
            )
        )
        assert recomputable_issue_event_idempotency_key(event) is None

    def test_event_id_matches_existing_sha256_prefix(self) -> None:
        key = "project_sync_pending:CH-1:B1:sha256:candidate"
        assert event_id(key) == "EVT-" + hashlib.sha256(key.encode()).hexdigest()[:16]
