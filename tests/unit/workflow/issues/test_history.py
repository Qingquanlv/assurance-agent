"""Contract tests for the read-only IssueHistoryReader seam.

Covers production/in-memory adapter equivalence, merge alias closure, late
review closure, resolved/regressed Problems, incomplete analysis/sync, and
corrupt ledger hard failures.
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
    ChangeIssueEvent,
    ProblemEvent,
)
from assurance_agent.workflow.issues.history import (
    InMemoryIssueHistoryReader,
    IssueHistoryIntegrityError,
    LedgerIssueHistoryReader,
)
from assurance_agent.workflow.issues.history_models import (
    IssueTypedEvents,
    IssueWindowSelection,
)
from assurance_agent.workflow.issues.ledger import ChangeIssueStore, ProjectProblemStore
from tests.helpers_aa import write_aa_config

CHANGE_ID = "RET-1"
OLDER_CHANGE_ID = "RET-0"
PROB_ID = "PROB-deadbeef12345678"
PROB_ALIAS = "PROB-alias000011112222"
FINGERPRINT = ProblemFingerprint(version="1", digest="sha256:" + "a" * 64)
FINGERPRINT_B = ProblemFingerprint(version="1", digest="sha256:" + "b" * 64)

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

OBS_OLD = Observation(
    observation_id="OBS-0000000000000000",
    change_id=OLDER_CHANGE_ID,
    batch_id="B-000",
    kind="test_failure",
    target="api",
    case_id=None,
    source=ObservationSource(
        artifact="execution/runs/B-000/api-result.json",
        json_pointer="/cases/0",
    ),
    evidence_refs=["execution/runs/B-000/api-result.json"],
    signature="http_500_old",
    observed_at="2026-07-20T10:00:00Z",
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

OCC_OLD = IssueOccurrence(
    occurrence_id="OCC-0000000000000000",
    change_id=OLDER_CHANGE_ID,
    batch_id="B-000",
    observation_ids=[OBS_OLD.observation_id],
    problem_id=PROB_ID,
    provisional_assessment=ProvisionalAssessment(
        classification="product_bug",
        severity="high",
        authority="llm_provisional",
        root_cause_hypothesis="null pointer",
    ),
    analysis=OccurrenceAnalysis(
        evidence_bundle_digest="sha256:olddigest",
        analyzer="aa-issue-analyzer",
        prompt_version="v1",
        candidate_digest="sha256:oldcand",
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

ANALYSIS_FAILED = IssueAnalysisStatus(
    schema_version="1.0",
    change_id=CHANGE_ID,
    batch_id="B-001",
    status="failed",
    evidence_bundle_digest="sha256:aabbccdd",
    candidate_count=0,
    reason="timeout",
    retryable=True,
)


def _ce(
    *,
    seq: int,
    event_id: str,
    type_: str,
    ts: str = "2026-07-25T10:00:00Z",
    change_id: str = CHANGE_ID,
    batch_id: str = "B-001",
    **extra: object,
) -> ChangeIssueEvent:
    data = {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id,
        "idempotency_key": f"IDEM-{event_id}",
        "ts": ts,
        "evidence_digest": "sha256:aabbccdd",
        "type": type_,
        "change_id": change_id,
        "batch_id": batch_id,
        **extra,
    }
    return CHANGE_ISSUE_EVENT_ADAPTER.validate_python(data)


def _pe(
    *,
    seq: int,
    event_id: str,
    type_: str,
    problem_id: str = PROB_ID,
    expected_problem_version: int = 0,
    ts: str = "2026-07-25T10:05:00Z",
    **extra: object,
) -> ProblemEvent:
    data = {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id,
        "idempotency_key": f"IDEM-{event_id}",
        "ts": ts,
        "evidence_digest": "sha256:aabbccdd",
        "type": type_,
        "problem_id": problem_id,
        "expected_problem_version": expected_problem_version,
        **extra,
    }
    return PROBLEM_EVENT_ADAPTER.validate_python(data)


def _happy_change_events(change_id: str = CHANGE_ID) -> list[ChangeIssueEvent]:
    obs = OBS if change_id == CHANGE_ID else OBS_OLD
    occ = OCC if change_id == CHANGE_ID else OCC_OLD
    analysis = ANALYSIS_OK.model_copy(update={"change_id": change_id})
    return [
        _ce(
            seq=1,
            event_id=f"CEVT-{change_id}-1",
            type_="observation_recorded",
            change_id=change_id,
            observation=obs.model_dump(mode="json"),
            ts="2026-07-25T10:00:00Z" if change_id == CHANGE_ID else "2026-07-20T10:00:00Z",
        ),
        _ce(
            seq=2,
            event_id=f"CEVT-{change_id}-2",
            type_="issue_analysis_completed",
            change_id=change_id,
            analysis_status=analysis.model_dump(mode="json"),
            ts="2026-07-25T10:01:00Z" if change_id == CHANGE_ID else "2026-07-20T10:01:00Z",
        ),
        _ce(
            seq=3,
            event_id=f"CEVT-{change_id}-3",
            type_="occurrence_detected" if change_id == CHANGE_ID else "occurrence_linked",
            change_id=change_id,
            occurrence=occ.model_dump(mode="json"),
            ts="2026-07-25T10:02:00Z" if change_id == CHANGE_ID else "2026-07-20T10:02:00Z",
        ),
    ]


def _happy_problem_events() -> list[ProblemEvent]:
    return [
        _pe(
            seq=1,
            event_id="PEVT-1",
            type_="problem_detected",
            occurrence_id=OCC.occurrence_id,
            change_id=CHANGE_ID,
            batch_id="B-001",
            fingerprint=FINGERPRINT.model_dump(mode="json"),
            title="HTTP 500 on empty name",
            classification="product_bug",
            severity="high",
            root_cause_hypothesis="null pointer",
            ts="2026-07-25T10:03:00Z",
        ),
        _pe(
            seq=2,
            event_id="PEVT-2",
            type_="problem_assessment_confirmed",
            expected_problem_version=1,
            classification="product_bug",
            severity="high",
            root_cause_hypothesis="null pointer",
            reason="confirmed by reviewer",
            evidence_refs=["review/note-1"],
            ts="2026-07-25T12:00:00Z",
        ),
        _pe(
            seq=3,
            event_id="PEVT-3",
            type_="problem_work_started",
            expected_problem_version=2,
            reason="fix started",
            evidence_refs=["ticket-1"],
            ts="2026-07-25T13:00:00Z",
        ),
        _pe(
            seq=4,
            event_id="PEVT-4",
            type_="problem_resolved",
            expected_problem_version=3,
            resolved_at="2026-07-25T14:00:00Z",
            change_id=CHANGE_ID,
            batch_id="B-001",
            disposition="fixed",
            verification_scope=["api"],
            ts="2026-07-25T14:00:00Z",
        ),
    ]


def _write_project(
    tmp_path: Path,
    *,
    change_events: dict[str, list[ChangeIssueEvent]],
    problem_events: list[ProblemEvent],
    prefer_archive: bool = True,
) -> tuple[Path, IssueTypedEvents]:
    write_aa_config(tmp_path)
    change_bytes: dict[str, bytes] = {}
    for change_id, events in change_events.items():
        root = tmp_path / ("qa/archive" if prefer_archive else "qa/changes") / change_id
        root.mkdir(parents=True, exist_ok=True)
        store = ChangeIssueStore(root)
        store.append_and_rebuild(events)
        change_bytes[change_id] = (root / "issues/events.jsonl").read_bytes()

    ProjectProblemStore(tmp_path).append_and_rebuild(problem_events)
    problem_bytes = (tmp_path / "qa/issues/events.jsonl").read_bytes()
    typed = IssueTypedEvents(
        change_events={cid: tuple(evs) for cid, evs in change_events.items()},
        problem_events=tuple(problem_events),
        change_ledger_bytes=change_bytes,
        problem_ledger_bytes=problem_bytes,
    )
    return tmp_path, typed


@pytest.fixture
def issue_tree(tmp_path: Path) -> Path:
    root, _ = _write_project(
        tmp_path,
        change_events={CHANGE_ID: _happy_change_events()},
        problem_events=_happy_problem_events(),
    )
    return root


@pytest.fixture
def typed_events(tmp_path: Path) -> IssueTypedEvents:
    _, typed = _write_project(
        tmp_path,
        change_events={CHANGE_ID: _happy_change_events()},
        problem_events=_happy_problem_events(),
    )
    return typed


def test_file_and_memory_adapters_return_same_slice(issue_tree: Path, typed_events: IssueTypedEvents) -> None:
    selection = IssueWindowSelection(change_ids=(CHANGE_ID,), project_event_through="PEVT-4")
    file_slice = LedgerIssueHistoryReader(issue_tree).read_window(selection)
    memory_slice = InMemoryIssueHistoryReader.from_events(typed_events).read_window(selection)
    assert file_slice.model_dump(mode="json") == memory_slice.model_dump(mode="json")
    assert file_slice.integrity.status == "complete"
    assert file_slice.schema_version == "1"
    assert OCC.occurrence_id in file_slice.resolvable_ids()
    assert PROB_ID in file_slice.resolvable_ids()
    assert file_slice.digest().startswith("sha256:")


def test_merge_alias_closure_includes_target_and_source(tmp_path: Path) -> None:
    change_events = _happy_change_events()
    problem_events = [
        _pe(
            seq=1,
            event_id="PEVT-1",
            type_="problem_detected",
            problem_id=PROB_ALIAS,
            occurrence_id=OCC.occurrence_id,
            change_id=CHANGE_ID,
            batch_id="B-001",
            fingerprint=FINGERPRINT_B.model_dump(mode="json"),
            title="alias source",
            classification="product_bug",
            severity="medium",
        ),
        _pe(
            seq=2,
            event_id="PEVT-2",
            type_="problem_detected",
            problem_id=PROB_ID,
            expected_problem_version=0,
            occurrence_id="OCC-target11111111",
            change_id="RET-X",
            batch_id="B-X",
            fingerprint=FINGERPRINT.model_dump(mode="json"),
            title="canonical target",
            classification="product_bug",
            severity="high",
            ts="2026-07-25T10:04:00Z",
        ),
        _pe(
            seq=3,
            event_id="PEVT-3",
            type_="problem_merged",
            problem_id=PROB_ALIAS,
            expected_problem_version=1,
            target_problem_id=PROB_ID,
            reason="duplicate fingerprint family",
            evidence_refs=["merge-review"],
            resolved_at="2026-07-25T11:00:00Z",
            ts="2026-07-25T11:00:00Z",
        ),
    ]
    # occurrence on RET-1 points at alias source
    occ_alias = OCC.model_copy(update={"problem_id": PROB_ALIAS})
    change_events = [
        change_events[0],
        change_events[1],
        _ce(
            seq=3,
            event_id="CEVT-RET-1-3",
            type_="occurrence_detected",
            occurrence=occ_alias.model_dump(mode="json"),
        ),
    ]
    root, typed = _write_project(
        tmp_path,
        change_events={CHANGE_ID: change_events},
        problem_events=problem_events,
    )
    selection = IssueWindowSelection(change_ids=(CHANGE_ID,), project_event_through="PEVT-3")
    slice_ = LedgerIssueHistoryReader(root).read_window(selection)
    problem_ids = {p.problem_id for p in slice_.problem_snapshots}
    assert PROB_ALIAS in problem_ids
    assert PROB_ID in problem_ids
    event_types = {e.type for e in slice_.problem_events}
    assert "problem_merged" in event_types
    memory = InMemoryIssueHistoryReader.from_events(typed).read_window(selection)
    assert slice_.model_dump(mode="json") == memory.model_dump(mode="json")


def test_late_review_closure_pulls_older_change_refs(tmp_path: Path) -> None:
    older_events = _happy_change_events(OLDER_CHANGE_ID)
    # Older change detected the problem; RET-1 only has a later link.
    older_detect = [
        older_events[0],
        older_events[1],
        _ce(
            seq=3,
            event_id="CEVT-RET-0-3",
            type_="occurrence_detected",
            change_id=OLDER_CHANGE_ID,
            occurrence=OCC_OLD.model_dump(mode="json"),
            ts="2026-07-20T10:02:00Z",
        ),
    ]
    newer = [
        _ce(
            seq=1,
            event_id="CEVT-RET-1-1",
            type_="observation_recorded",
            observation=OBS.model_dump(mode="json"),
        ),
        _ce(
            seq=2,
            event_id="CEVT-RET-1-2",
            type_="issue_analysis_completed",
            analysis_status=ANALYSIS_OK.model_dump(mode="json"),
        ),
        _ce(
            seq=3,
            event_id="CEVT-RET-1-3",
            type_="occurrence_linked",
            occurrence=OCC.model_dump(mode="json"),
        ),
    ]
    problem_events = [
        _pe(
            seq=1,
            event_id="PEVT-1",
            type_="problem_detected",
            occurrence_id=OCC_OLD.occurrence_id,
            change_id=OLDER_CHANGE_ID,
            batch_id="B-000",
            fingerprint=FINGERPRINT.model_dump(mode="json"),
            title="shared problem",
            classification="product_bug",
            severity="high",
            ts="2026-07-20T10:03:00Z",
        ),
        _pe(
            seq=2,
            event_id="PEVT-2",
            type_="problem_occurrence_linked",
            expected_problem_version=1,
            occurrence_id=OCC.occurrence_id,
            change_id=CHANGE_ID,
            batch_id="B-001",
            ts="2026-07-25T10:03:00Z",
        ),
        _pe(
            seq=3,
            event_id="PEVT-3",
            type_="problem_assessment_confirmed",
            expected_problem_version=2,
            classification="test_bug",
            severity="medium",
            root_cause_hypothesis="flaky fixture",
            reason="late human review after RET-1",
            evidence_refs=["review/late"],
            ts="2026-07-26T09:00:00Z",
        ),
    ]
    root, _ = _write_project(
        tmp_path,
        change_events={OLDER_CHANGE_ID: older_detect, CHANGE_ID: newer},
        problem_events=problem_events,
    )
    selection = IssueWindowSelection(
        change_ids=(CHANGE_ID,),
        project_event_through="PEVT-3",
        include_late_review_closure=True,
    )
    slice_ = LedgerIssueHistoryReader(root).read_window(selection)
    obs_ids = {o.observation_id for o in slice_.observations}
    occ_ids = {o.occurrence_id for o in slice_.occurrences}
    assert OBS.observation_id in obs_ids
    assert OBS_OLD.observation_id in obs_ids
    assert OCC_OLD.occurrence_id in occ_ids
    assert any(e.type == "problem_assessment_confirmed" for e in slice_.problem_events)


def test_resolved_and_regressed_problem_events_in_window(tmp_path: Path) -> None:
    problem_events = [
        *_happy_problem_events(),
        _pe(
            seq=5,
            event_id="PEVT-5",
            type_="problem_regressed",
            expected_problem_version=4,
            occurrence_id="OCC-regressed111111",
            change_id=CHANGE_ID,
            ts="2026-07-26T10:00:00Z",
        ),
    ]
    root, typed = _write_project(
        tmp_path,
        change_events={CHANGE_ID: _happy_change_events()},
        problem_events=problem_events,
    )
    selection = IssueWindowSelection(change_ids=(CHANGE_ID,), project_event_through="PEVT-5")
    slice_ = InMemoryIssueHistoryReader.from_events(typed).read_window(selection)
    types = [e.type for e in slice_.problem_events]
    assert "problem_resolved" in types
    assert "problem_regressed" in types
    assert slice_.problem_snapshots[0].status == "detected"
    file_slice = LedgerIssueHistoryReader(root).read_window(selection)
    assert file_slice.model_dump(mode="json") == slice_.model_dump(mode="json")


def _write_change_only(
    tmp_path: Path,
    events: list[ChangeIssueEvent],
) -> tuple[Path, IssueTypedEvents]:
    write_aa_config(tmp_path)
    change_root = tmp_path / "qa/archive" / CHANGE_ID
    change_root.mkdir(parents=True)
    ChangeIssueStore(change_root).append_and_rebuild(events)
    change_bytes = {CHANGE_ID: (change_root / "issues/events.jsonl").read_bytes()}
    (tmp_path / "qa/issues").mkdir(parents=True, exist_ok=True)
    (tmp_path / "qa/issues/events.jsonl").write_bytes(b"")
    typed = IssueTypedEvents(
        change_events={CHANGE_ID: tuple(events)},
        problem_events=(),
        change_ledger_bytes=change_bytes,
        problem_ledger_bytes=b"",
    )
    return tmp_path, typed


def test_analysis_failed_marks_incomplete(tmp_path: Path) -> None:
    events = [
        _ce(
            seq=1,
            event_id="CEVT-1",
            type_="observation_recorded",
            observation=OBS.model_dump(mode="json"),
        ),
        _ce(
            seq=2,
            event_id="CEVT-2",
            type_="issue_analysis_failed",
            analysis_status=ANALYSIS_FAILED.model_dump(mode="json"),
        ),
    ]
    root, typed = _write_change_only(tmp_path, events)
    selection = IssueWindowSelection(change_ids=(CHANGE_ID,))
    slice_ = LedgerIssueHistoryReader(root).read_window(selection)
    assert slice_.integrity.status == "incomplete"
    assert "analysis_failed" in slice_.integrity.reasons
    assert InMemoryIssueHistoryReader.from_events(typed).read_window(selection).integrity.status == (
        "incomplete"
    )


def test_project_sync_pending_marks_incomplete(tmp_path: Path) -> None:
    events = [
        *_happy_change_events()[:2],
        _ce(
            seq=3,
            event_id="CEVT-3",
            type_="project_sync_pending",
            candidate_digest="sha256:11223344",
        ),
    ]
    root, typed = _write_change_only(tmp_path, events)
    selection = IssueWindowSelection(change_ids=(CHANGE_ID,))
    slice_ = LedgerIssueHistoryReader(root).read_window(selection)
    assert slice_.integrity.status == "incomplete"
    assert "project_sync_pending" in slice_.integrity.reasons
    assert (
        InMemoryIssueHistoryReader.from_events(typed).read_window(selection).integrity.reasons
        == slice_.integrity.reasons
    )


def test_in_memory_missing_selected_change_raises_integrity_error(
    typed_events: IssueTypedEvents,
) -> None:
    """Selected change_ids absent from the bundle must hard-fail like ledger."""
    selection = IssueWindowSelection(change_ids=("RET-MISSING",))
    with pytest.raises(IssueHistoryIntegrityError):
        InMemoryIssueHistoryReader.from_events(typed_events).read_window(selection)


def test_in_memory_batch_tolerates_missing_member_and_keeps_complete_member(
    typed_events: IssueTypedEvents,
) -> None:
    slice_ = InMemoryIssueHistoryReader.from_events(typed_events).read_window(
        IssueWindowSelection(
            change_ids=(CHANGE_ID, "RET-MISSING"),
            allow_member_gaps=True,
            member_execution_statuses=((CHANGE_ID, "completed"), ("RET-MISSING", "failed")),
        )
    )
    assert tuple(item.change_id for item in slice_.occurrences) == (CHANGE_ID,)
    assert "batch_member_evidence_gap:RET-MISSING:failed:issue:workspace_missing" in slice_.integrity.reasons


def test_malformed_jsonl_raises_integrity_error(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    change_root = tmp_path / "qa/archive" / CHANGE_ID
    change_root.mkdir(parents=True)
    events_path = change_root / "issues/events.jsonl"
    events_path.parent.mkdir(parents=True)
    events_path.write_text("{not json\n", encoding="utf-8")
    (tmp_path / "qa/issues").mkdir(parents=True)
    (tmp_path / "qa/issues/events.jsonl").write_bytes(b"")
    with pytest.raises(IssueHistoryIntegrityError):
        LedgerIssueHistoryReader(tmp_path).read_window(IssueWindowSelection(change_ids=(CHANGE_ID,)))


def test_unknown_event_schema_raises_integrity_error(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    change_root = tmp_path / "qa/archive" / CHANGE_ID
    change_root.mkdir(parents=True)
    events_path = change_root / "issues/events.jsonl"
    events_path.parent.mkdir(parents=True)
    bad = {
        "schema_version": "1.0",
        "seq": 1,
        "event_id": "CEVT-1",
        "idempotency_key": "IDEM-1",
        "ts": "2026-07-25T10:00:00Z",
        "evidence_digest": "sha256:aabbccdd",
        "type": "totally_unknown_event",
        "change_id": CHANGE_ID,
        "batch_id": "B-001",
    }
    events_path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    (tmp_path / "qa/issues").mkdir(parents=True)
    (tmp_path / "qa/issues/events.jsonl").write_bytes(b"")
    with pytest.raises(IssueHistoryIntegrityError):
        LedgerIssueHistoryReader(tmp_path).read_window(IssueWindowSelection(change_ids=(CHANGE_ID,)))


def test_mismatched_projection_raises_integrity_error(tmp_path: Path) -> None:
    root, _ = _write_project(
        tmp_path,
        change_events={CHANGE_ID: _happy_change_events()},
        problem_events=_happy_problem_events(),
    )
    snapshot_path = root / "qa/archive" / CHANGE_ID / "issues/snapshot.json"
    snapshot_path.write_text('{"schema_version":"1.0","tampered":true}\n', encoding="utf-8")
    with pytest.raises(IssueHistoryIntegrityError, match="projection"):
        LedgerIssueHistoryReader(root).read_window(
            IssueWindowSelection(change_ids=(CHANGE_ID,), project_event_through="PEVT-4")
        )


def test_since_until_selection_applies_reference_closure(tmp_path: Path) -> None:
    root, typed = _write_project(
        tmp_path,
        change_events={CHANGE_ID: _happy_change_events()},
        problem_events=_happy_problem_events(),
    )
    selection = IssueWindowSelection(
        change_ids=(),
        event_since="2026-07-25T10:00:00Z",
        event_until="2026-07-25T10:02:00Z",
        project_event_through="PEVT-4",
    )
    slice_ = LedgerIssueHistoryReader(root).read_window(selection)
    # Change issue events in the interval, plus problem closure through head.
    assert any(o.observation_id == OBS.observation_id for o in slice_.observations)
    assert any(e.event_id == "PEVT-4" for e in slice_.problem_events)
    assert slice_.model_dump(mode="json") == InMemoryIssueHistoryReader.from_events(typed).read_window(
        selection
    ).model_dump(mode="json")


def test_project_event_through_pins_head(tmp_path: Path) -> None:
    root, _ = _write_project(
        tmp_path,
        change_events={CHANGE_ID: _happy_change_events()},
        problem_events=_happy_problem_events(),
    )
    selection = IssueWindowSelection(change_ids=(CHANGE_ID,), project_event_through="PEVT-2")
    slice_ = LedgerIssueHistoryReader(root).read_window(selection)
    assert [e.event_id for e in slice_.problem_events] == ["PEVT-1", "PEVT-2"]
    assert slice_.sources[-1].head_event_id == "PEVT-2"
    # Snapshot at pinned head is assessment-confirmed (triaged), not resolved.
    assert slice_.problem_snapshots[0].status == "triaged"
