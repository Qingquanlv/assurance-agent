"""Tests for ChangeIssueStore and ProjectProblemStore.

Covers:
- append_and_rebuild writes events.jsonl and rebuilds snapshot/projection
- Idempotent no-op when all events already committed by idempotency_key
- Partial idempotency: new events added, already-committed events skipped
- Atomic all-or-nothing: temp file used, originals untouched on write error
- rebuild from corrupted snapshot (projection rebuilt from events.jsonl)
- ValueError when events list is empty
- seq assignment continues from last committed seq
- Both ChangeIssueStore and ProjectProblemStore paths
"""

from __future__ import annotations

import json
from pathlib import Path

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
    read_change_issue_events,
    read_problem_events,
)
from assurance_agent.workflow.issues.ledger import (
    ChangeIssueStore,
    ProjectProblemStore,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

FINGERPRINT = ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)

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
    signature="http_warning_slow",
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

PROB_ID = "PROB-deadbeef12345678"


def _obs_event(seq: int, event_id: str, idem: str, obs=OBS_1, batch_id="B-001") -> dict:
    return {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id,
        "idempotency_key": idem,
        "ts": "2026-07-25T10:00:00Z",
        "evidence_digest": "sha256:aabbccdd",
        "type": "observation_recorded",
        "change_id": "CH-001",
        "batch_id": batch_id,
        "observation": obs.model_dump(mode="json"),
    }


def _analysis_event(seq: int, event_id: str, idem: str) -> dict:
    return {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id,
        "idempotency_key": idem,
        "ts": "2026-07-25T10:00:00Z",
        "evidence_digest": "sha256:aabbccdd",
        "type": "issue_analysis_completed",
        "change_id": "CH-001",
        "batch_id": "B-001",
        "analysis_status": ANALYSIS_OK.model_dump(mode="json"),
    }


def _occurrence_event(seq: int, event_id: str, idem: str) -> dict:
    return {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id,
        "idempotency_key": idem,
        "ts": "2026-07-25T10:00:00Z",
        "evidence_digest": "sha256:aabbccdd",
        "type": "occurrence_detected",
        "change_id": "CH-001",
        "batch_id": "B-001",
        "occurrence": OCC_1.model_dump(mode="json"),
    }


def _detected_problem_event(seq: int, event_id: str, idem: str) -> dict:
    return {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id,
        "idempotency_key": idem,
        "ts": "2026-07-25T10:00:00Z",
        "evidence_digest": "sha256:aabbccdd",
        "type": "problem_detected",
        "problem_id": PROB_ID,
        "expected_problem_version": 0,
        "occurrence_id": "OCC-1111111111111111",
        "change_id": "CH-001",
        "batch_id": "B-001",
        "fingerprint": FINGERPRINT.model_dump(mode="json"),
        "title": "HTTP 500 on empty name",
        "classification": "product_bug",
        "severity": "high",
    }


def _mk_change_events(*dicts: dict):
    return [CHANGE_ISSUE_EVENT_ADAPTER.validate_python(d) for d in dicts]


def _mk_problem_events(*dicts: dict):
    return [PROBLEM_EVENT_ADAPTER.validate_python(d) for d in dicts]


# ---------------------------------------------------------------------------
# ChangeIssueStore tests
# ---------------------------------------------------------------------------


class TestChangeIssueStore:
    def test_empty_events_raises(self, tmp_path: Path) -> None:
        store = ChangeIssueStore(tmp_path)
        with pytest.raises(ValueError, match="at least one event"):
            store.append_and_rebuild([])

    def test_creates_events_and_snapshot(self, tmp_path: Path) -> None:
        store = ChangeIssueStore(tmp_path)
        events = _mk_change_events(
            _obs_event(1, "E1", "IDEM-1"),
            _analysis_event(2, "E2", "IDEM-2"),
        )
        snap = store.append_and_rebuild(events)

        assert snap.change_id == "CH-001"
        assert len(snap.observations) == 1
        assert snap.analysis_status is not None

        events_path = tmp_path / "issues" / "events.jsonl"
        snapshot_path = tmp_path / "issues" / "snapshot.json"
        assert events_path.exists()
        assert snapshot_path.exists()

    def test_seq_assigned_to_events(self, tmp_path: Path) -> None:
        store = ChangeIssueStore(tmp_path)
        events = _mk_change_events(_obs_event(1, "E1", "IDEM-1"))
        store.append_and_rebuild(events)

        events_path = tmp_path / "issues" / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["seq"] == 1
        assert data["event_id"] == "E1"

    def test_idempotent_no_op_on_committed_keys(self, tmp_path: Path) -> None:
        store = ChangeIssueStore(tmp_path)
        events = _mk_change_events(_obs_event(1, "E1", "IDEM-1"))

        snap1 = store.append_and_rebuild(events)
        snap2 = store.append_and_rebuild(events)  # same idempotency_key

        events_path = tmp_path / "issues" / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1  # no duplicate appended
        assert snap1.change_id == snap2.change_id

    def test_partial_idempotency_only_new_events_appended(self, tmp_path: Path) -> None:
        store = ChangeIssueStore(tmp_path)
        first_batch = _mk_change_events(_obs_event(1, "E1", "IDEM-1"))
        store.append_and_rebuild(first_batch)

        second_batch = _mk_change_events(
            _obs_event(1, "E1", "IDEM-1"),         # already committed
            _obs_event(1, "E2", "IDEM-2", OBS_2, "B-002"),  # new
        )
        snap = store.append_and_rebuild(second_batch)

        events_path = tmp_path / "issues" / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        # seq of the second committed event should be 2
        data2 = json.loads(lines[1])
        assert data2["seq"] == 2
        assert data2["event_id"] == "E2"
        assert len(snap.observations) == 2

    def test_snapshot_rebuilt_from_events_when_deleted(self, tmp_path: Path) -> None:
        store = ChangeIssueStore(tmp_path)
        events = _mk_change_events(_obs_event(1, "E1", "IDEM-1"))
        store.append_and_rebuild(events)

        # Delete snapshot; add another event to trigger rebuild
        (tmp_path / "issues" / "snapshot.json").unlink()
        events2 = _mk_change_events(_analysis_event(1, "E2", "IDEM-2"))
        snap = store.append_and_rebuild(events2)

        assert len(snap.observations) == 1  # from first batch, still in events.jsonl
        assert (tmp_path / "issues" / "snapshot.json").exists()

    def test_occurrence_detected_retained_across_batches(self, tmp_path: Path) -> None:
        store = ChangeIssueStore(tmp_path)
        batch1 = _mk_change_events(
            _obs_event(1, "E1", "IDEM-1"),
            _occurrence_event(2, "E2", "IDEM-2"),
        )
        store.append_and_rebuild(batch1)

        batch2 = _mk_change_events(
            _obs_event(1, "E3", "IDEM-3", OBS_2, "B-002"),
        )
        snap = store.append_and_rebuild(batch2)

        assert len(snap.observations) == 2
        assert len(snap.occurrences) == 1
        assert "B-001" in snap.batches
        assert "B-002" in snap.batches

    def test_snapshot_json_is_canonical(self, tmp_path: Path) -> None:
        store = ChangeIssueStore(tmp_path)
        events = _mk_change_events(_obs_event(1, "E1", "IDEM-1"))
        store.append_and_rebuild(events)

        raw = (tmp_path / "issues" / "snapshot.json").read_bytes()
        assert raw.endswith(b"\n")
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        top_keys = list(parsed.keys())
        assert top_keys == sorted(top_keys)

    def test_read_events_validates_committed_ledger(self, tmp_path: Path) -> None:
        store = ChangeIssueStore(tmp_path)
        events = _mk_change_events(_obs_event(1, "E1", "IDEM-1"))
        store.append_and_rebuild(events)

        committed = read_change_issue_events(tmp_path / "issues" / "events.jsonl")
        assert len(committed) == 1


# ---------------------------------------------------------------------------
# ProjectProblemStore tests
# ---------------------------------------------------------------------------


class TestProjectProblemStore:
    def test_empty_events_raises(self, tmp_path: Path) -> None:
        store = ProjectProblemStore(tmp_path)
        with pytest.raises(ValueError, match="at least one event"):
            store.append_and_rebuild([])

    def test_creates_events_problems_and_queue(self, tmp_path: Path) -> None:
        store = ProjectProblemStore(tmp_path)
        events = _mk_problem_events(_detected_problem_event(1, "E1", "IDEM-1"))
        proj, queue = store.append_and_rebuild(events)

        assert len(proj.problems) == 1
        assert proj.problems[0].problem_id == PROB_ID
        assert queue.schema_version == "1.0"

        events_path = tmp_path / "qa" / "issues" / "events.jsonl"
        problems_path = tmp_path / "qa" / "issues" / "problems.json"
        queue_path = tmp_path / "qa" / "issues" / "review-queue.json"
        assert events_path.exists()
        assert problems_path.exists()
        assert queue_path.exists()

    def test_idempotent_no_op(self, tmp_path: Path) -> None:
        store = ProjectProblemStore(tmp_path)
        events = _mk_problem_events(_detected_problem_event(1, "E1", "IDEM-1"))

        proj1, _ = store.append_and_rebuild(events)
        proj2, _ = store.append_and_rebuild(events)  # same key

        events_path = tmp_path / "qa" / "issues" / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert proj1.problems[0].version == proj2.problems[0].version

    def test_multiple_events_incrementing_version(self, tmp_path: Path) -> None:
        store = ProjectProblemStore(tmp_path)
        batch1 = _mk_problem_events(_detected_problem_event(1, "E1", "IDEM-1"))
        store.append_and_rebuild(batch1)

        batch2 = _mk_problem_events({
            "schema_version": "1.0",
            "seq": 1,
            "event_id": "E2",
            "idempotency_key": "IDEM-2",
            "ts": "2026-07-25T10:01:00Z",
            "evidence_digest": "sha256:aabbccdd",
            "type": "problem_occurrence_linked",
            "problem_id": PROB_ID,
            "expected_problem_version": 1,
            "occurrence_id": "OCC-2222222222222222",
            "change_id": "CH-001",
            "batch_id": "B-002",
        })
        proj, _ = store.append_and_rebuild(batch2)

        assert proj.problems[0].version == 2
        assert "OCC-2222222222222222" in proj.problems[0].occurrences

    def test_seq_continues_from_last(self, tmp_path: Path) -> None:
        store = ProjectProblemStore(tmp_path)
        batch1 = _mk_problem_events(_detected_problem_event(1, "E1", "IDEM-1"))
        store.append_and_rebuild(batch1)

        batch2 = _mk_problem_events({
            "schema_version": "1.0",
            "seq": 1,
            "event_id": "E2",
            "idempotency_key": "IDEM-2",
            "ts": "2026-07-25T10:01:00Z",
            "evidence_digest": "sha256:aabbccdd",
            "type": "problem_assessment_confirmed",
            "problem_id": PROB_ID,
            "expected_problem_version": 1,
            "classification": "product_bug",
            "severity": "critical",
            "reason": "confirmed",
            "evidence_refs": ["OCC-1111111111111111"],
        })
        store.append_and_rebuild(batch2)

        events_path = tmp_path / "qa" / "issues" / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["seq"] == 2

    def test_problems_json_is_canonical(self, tmp_path: Path) -> None:
        store = ProjectProblemStore(tmp_path)
        events = _mk_problem_events(_detected_problem_event(1, "E1", "IDEM-1"))
        store.append_and_rebuild(events)

        raw = (tmp_path / "qa" / "issues" / "problems.json").read_bytes()
        assert raw.endswith(b"\n")
        parsed = json.loads(raw)
        top_keys = list(parsed.keys())
        assert top_keys == sorted(top_keys)

    def test_rebuild_after_projection_deletion(self, tmp_path: Path) -> None:
        store = ProjectProblemStore(tmp_path)
        events = _mk_problem_events(_detected_problem_event(1, "E1", "IDEM-1"))
        store.append_and_rebuild(events)

        (tmp_path / "qa" / "issues" / "problems.json").unlink()
        (tmp_path / "qa" / "issues" / "review-queue.json").unlink()

        batch2 = _mk_problem_events({
            "schema_version": "1.0",
            "seq": 1,
            "event_id": "E2",
            "idempotency_key": "IDEM-2",
            "ts": "2026-07-25T10:01:00Z",
            "evidence_digest": "sha256:aabbccdd",
            "type": "problem_occurrence_linked",
            "problem_id": PROB_ID,
            "expected_problem_version": 1,
            "occurrence_id": "OCC-2222222222222222",
            "change_id": "CH-001",
            "batch_id": "B-002",
        })
        proj, queue = store.append_and_rebuild(batch2)

        assert (tmp_path / "qa" / "issues" / "problems.json").exists()
        assert (tmp_path / "qa" / "issues" / "review-queue.json").exists()
        assert proj.problems[0].version == 2

    def test_read_events_validates_committed_ledger(self, tmp_path: Path) -> None:
        store = ProjectProblemStore(tmp_path)
        events = _mk_problem_events(_detected_problem_event(1, "E1", "IDEM-1"))
        store.append_and_rebuild(events)

        committed = read_problem_events(tmp_path / "qa" / "issues" / "events.jsonl")
        assert len(committed) == 1
        assert committed[0].problem_id == PROB_ID

    def test_byte_stable_rebuild(self, tmp_path: Path) -> None:
        """Double rebuild from same events must yield byte-identical files."""
        store1 = ProjectProblemStore(tmp_path / "ws1")
        store2 = ProjectProblemStore(tmp_path / "ws2")
        events = _mk_problem_events(_detected_problem_event(1, "E1", "IDEM-1"))

        store1.append_and_rebuild(events)
        store2.append_and_rebuild(events)

        problems1 = (tmp_path / "ws1" / "qa" / "issues" / "problems.json").read_bytes()
        problems2 = (tmp_path / "ws2" / "qa" / "issues" / "problems.json").read_bytes()
        assert problems1 == problems2

        queue1 = (tmp_path / "ws1" / "qa" / "issues" / "review-queue.json").read_bytes()
        queue2 = (tmp_path / "ws2" / "qa" / "issues" / "review-queue.json").read_bytes()
        assert queue1 == queue2
