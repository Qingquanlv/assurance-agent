"""Integration tests for LedgerIssueHistoryReader stable Project head reads."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
from assurance_agent.workflow.issues.history import (
    IssueHistoryConflict,
    LedgerIssueHistoryReader,
)
from assurance_agent.workflow.issues.history_models import IssueWindowSelection
from assurance_agent.workflow.issues.ledger import ChangeIssueStore, ProjectProblemStore
from tests.helpers_aa import write_aa_config

CHANGE_ID = "RET-1"
PROB_ID = "PROB-deadbeef12345678"
FINGERPRINT = ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)

OBS = Observation(
    observation_id="OBS-1111111111111111",
    change_id=CHANGE_ID,
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

OCC = IssueOccurrence(
    occurrence_id="OCC-1111111111111111",
    change_id=CHANGE_ID,
    batch_id="B-001",
    observation_ids=[OBS.observation_id],
    problem_id=PROB_ID,
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

ANALYSIS_OK = IssueAnalysisStatus(
    schema_version="1.0",
    change_id=CHANGE_ID,
    batch_id="B-001",
    status="completed",
    evidence_bundle_digest="sha256:aabbccdd",
    candidate_count=1,
    candidate_digest="sha256:11223344",
)


def _setup(tmp_path: Path) -> Path:
    write_aa_config(tmp_path)
    change_root = tmp_path / "qa/archive" / CHANGE_ID
    change_root.mkdir(parents=True)
    change_events = [
        CHANGE_ISSUE_EVENT_ADAPTER.validate_python(
            {
                "schema_version": "1.0",
                "seq": 1,
                "event_id": "CEVT-1",
                "idempotency_key": "IDEM-CEVT-1",
                "ts": "2026-07-25T10:00:00Z",
                "evidence_digest": "sha256:aabbccdd",
                "type": "observation_recorded",
                "change_id": CHANGE_ID,
                "batch_id": "B-001",
                "observation": OBS.model_dump(mode="json"),
            }
        ),
        CHANGE_ISSUE_EVENT_ADAPTER.validate_python(
            {
                "schema_version": "1.0",
                "seq": 2,
                "event_id": "CEVT-2",
                "idempotency_key": "IDEM-CEVT-2",
                "ts": "2026-07-25T10:01:00Z",
                "evidence_digest": "sha256:aabbccdd",
                "type": "issue_analysis_completed",
                "change_id": CHANGE_ID,
                "batch_id": "B-001",
                "analysis_status": ANALYSIS_OK.model_dump(mode="json"),
            }
        ),
        CHANGE_ISSUE_EVENT_ADAPTER.validate_python(
            {
                "schema_version": "1.0",
                "seq": 3,
                "event_id": "CEVT-3",
                "idempotency_key": "IDEM-CEVT-3",
                "ts": "2026-07-25T10:02:00Z",
                "evidence_digest": "sha256:aabbccdd",
                "type": "occurrence_detected",
                "change_id": CHANGE_ID,
                "batch_id": "B-001",
                "occurrence": OCC.model_dump(mode="json"),
            }
        ),
    ]
    ChangeIssueStore(change_root).append_and_rebuild(change_events)
    problem_events = [
        PROBLEM_EVENT_ADAPTER.validate_python(
            {
                "schema_version": "1.0",
                "seq": 1,
                "event_id": "PEVT-1",
                "idempotency_key": "IDEM-PEVT-1",
                "ts": "2026-07-25T10:03:00Z",
                "evidence_digest": "sha256:aabbccdd",
                "type": "problem_detected",
                "problem_id": PROB_ID,
                "expected_problem_version": 0,
                "occurrence_id": OCC.occurrence_id,
                "change_id": CHANGE_ID,
                "batch_id": "B-001",
                "fingerprint": FINGERPRINT.model_dump(mode="json"),
                "title": "HTTP 500",
                "classification": "product_bug",
                "severity": "high",
            }
        )
    ]
    ProjectProblemStore(tmp_path).append_and_rebuild(problem_events)
    return tmp_path


def test_stable_read_succeeds_when_head_unchanged(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    slice_ = LedgerIssueHistoryReader(root).read_window(
        IssueWindowSelection(change_ids=(CHANGE_ID,), project_event_through="PEVT-1")
    )
    assert slice_.integrity.status == "complete"
    assert slice_.sources[-1].head_event_id == "PEVT-1"


def test_changed_project_head_during_read_raises_conflict(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    path = root / "qa/issues/events.jsonl"
    original = path.read_bytes()
    mutated = original + b"\n"
    reads = {"n": 0}
    real_read_bytes = Path.read_bytes

    def flapping_read_bytes(self: Path) -> bytes:
        if self != path:
            return real_read_bytes(self)
        reads["n"] += 1
        return original if reads["n"] % 2 == 1 else mutated

    reader = LedgerIssueHistoryReader(root)
    with patch.object(Path, "read_bytes", flapping_read_bytes):
        with pytest.raises(IssueHistoryConflict, match="changed during"):
            reader.read_window(IssueWindowSelection(change_ids=(CHANGE_ID,)))


def test_stable_read_retries_then_succeeds(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    path = root / "qa/issues/events.jsonl"
    original = path.read_bytes()
    reads = {"n": 0}
    real_read_bytes = Path.read_bytes
    # Attempt 1: before != after; attempt 2: before == after.
    sequence = [original, original + b"\n", original, original]

    def sequenced(self: Path) -> bytes:
        if self != path:
            return real_read_bytes(self)
        idx = min(reads["n"], len(sequence) - 1)
        reads["n"] += 1
        return sequence[idx]

    reader = LedgerIssueHistoryReader(root)
    with patch.object(Path, "read_bytes", sequenced):
        slice_ = reader.read_window(
            IssueWindowSelection(change_ids=(CHANGE_ID,), project_event_through="PEVT-1")
        )
    assert slice_.integrity.status == "complete"
    assert reads["n"] >= 4
