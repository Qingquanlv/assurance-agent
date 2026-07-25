"""Tests for ProjectImprovementStore append/filter/rebuild."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.improvements import ImprovementState
from assurance_agent.workflow.improvements.events import (
    IMPROVEMENT_EVENT_ADAPTER,
    ImprovementEvent,
    read_improvement_events,
)
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore

IMP_ID = "IMP-ABCDEF0123456789FFFF"
FINGERPRINT = "a" * 64


def _proposed(
    *,
    seq: int = 1,
    event_id: str = "IMPEVT-PROP",
    idempotency_key: str = "IDEM-PROP",
    problem_ids: list[str] | None = None,
    retro_id: str = "RETRO-1",
) -> ImprovementEvent:
    return IMPROVEMENT_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": seq,
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "ts": "2026-07-26T00:00:00Z",
            "improvement_id": IMP_ID,
            "expected_improvement_version": 0,
            "type": "improvement_proposed",
            "fingerprint": FINGERPRINT,
            "fingerprint_version": "1",
            "kind": "workflow_improvement",
            "delivery": "change_draft",
            "source_refs": {"problem_ids": problem_ids or ["PROB-1"]},
            "target": "assurance_agent/workflow/inspect",
            "rationale": "Repeated truncation",
            "proposed_change": "Preserve pytest E lines",
            "verification": {
                "suites": ["workflow-full"],
                "success_criteria": "No truncation",
            },
            "risk": "low",
            "confidence": "high",
            "retro_id": retro_id,
            "candidate_id": "IMP-CAND-1",
            "context_sha256": "b" * 64,
            "candidate_batch_digest": "c" * 64,
        }
    )


def _evidence_linked(
    *,
    seq: int = 2,
    event_id: str = "IMPEVT-EVID",
    idempotency_key: str = "IDEM-EVID",
    expected_version: int = 1,
) -> ImprovementEvent:
    return IMPROVEMENT_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": seq,
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "ts": "2026-07-26T00:00:01Z",
            "improvement_id": IMP_ID,
            "expected_improvement_version": expected_version,
            "type": "improvement_evidence_linked",
            "source_refs": {"problem_ids": ["PROB-2"]},
            "retro_id": "RETRO-2",
            "candidate_id": "IMP-CAND-2",
            "context_sha256": "d" * 64,
            "candidate_batch_digest": "e" * 64,
        }
    )


def test_empty_events_raises(tmp_path: Path) -> None:
    store = ProjectImprovementStore(tmp_path)
    with pytest.raises(ValueError, match="at least one event"):
        store.append_and_rebuild([])


def test_creates_events_projection_and_queue(tmp_path: Path) -> None:
    store = ProjectImprovementStore(tmp_path)
    projection = store.append_and_rebuild([_proposed(), _evidence_linked()])

    events_path = tmp_path / "qa" / "improvements" / "events.jsonl"
    improvements_path = tmp_path / "qa" / "improvements" / "improvements.json"
    queue_path = tmp_path / "qa" / "improvements" / "review-queue.json"

    assert events_path.is_file()
    assert improvements_path.is_file()
    assert queue_path.is_file()

    committed = read_improvement_events(events_path)
    assert len(committed) == 2
    assert [event.seq for event in committed] == [1, 2]

    assert tuple(projection.improvements) == (IMP_ID,)
    assert projection.improvements[IMP_ID].source_refs.problem_ids == ("PROB-1", "PROB-2")
    assert projection.improvements[IMP_ID].state is ImprovementState.PROPOSED

    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert queue["improvement_ids"] == [IMP_ID]


def test_rebuild_is_byte_stable(tmp_path: Path) -> None:
    store = ProjectImprovementStore(tmp_path)
    events = [_proposed(), _evidence_linked()]
    store.append_and_rebuild(events)
    first = (tmp_path / "qa/improvements/improvements.json").read_bytes()
    queue_first = (tmp_path / "qa/improvements/review-queue.json").read_bytes()
    store.append_and_rebuild(events)
    assert (tmp_path / "qa/improvements/improvements.json").read_bytes() == first
    assert (tmp_path / "qa/improvements/review-queue.json").read_bytes() == queue_first
    assert first.endswith(b"\n")
    assert queue_first.endswith(b"\n")

    committed = read_improvement_events(tmp_path / "qa/improvements/events.jsonl")
    assert len(committed) == 2


def test_partial_idempotency_appends_only_new(tmp_path: Path) -> None:
    store = ProjectImprovementStore(tmp_path)
    store.append_and_rebuild([_proposed()])
    projection = store.append_and_rebuild([_proposed(), _evidence_linked()])

    committed = read_improvement_events(tmp_path / "qa/improvements/events.jsonl")
    assert len(committed) == 2
    assert committed[0].idempotency_key == "IDEM-PROP"
    assert committed[1].idempotency_key == "IDEM-EVID"
    assert projection.improvements[IMP_ID].version == 2
    assert projection.last_seq == 2


def test_seq_continues_from_committed(tmp_path: Path) -> None:
    store = ProjectImprovementStore(tmp_path)
    store.append_and_rebuild([_proposed(seq=99)])
    store.append_and_rebuild([_evidence_linked(seq=1)])

    committed = read_improvement_events(tmp_path / "qa/improvements/events.jsonl")
    assert [event.seq for event in committed] == [1, 2]


def test_projection_files_are_canonical(tmp_path: Path) -> None:
    store = ProjectImprovementStore(tmp_path)
    store.append_and_rebuild([_proposed()])

    raw = (tmp_path / "qa/improvements/improvements.json").read_bytes()
    parsed = json.loads(raw)
    assert list(parsed.keys()) == sorted(parsed.keys())
