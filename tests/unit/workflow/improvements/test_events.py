"""Tests for strict Improvement event adapters and JSONL reader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.improvements import (
    ImprovementLedgerProjection,
    ImprovementSourceRefs,
    ImprovementVerification,
)
from assurance_agent.workflow.improvements.events import (
    IMPROVEMENT_EVENT_ADAPTER,
    ImprovementAppliedEvent,
    ImprovementEvalCompletedEvent,
    ImprovementEvalRequestedEvent,
    ImprovementEvidenceLinkedEvent,
    ImprovementExportedEvent,
    ImprovementLedgerIntegrityError,
    ImprovementProposedEvent,
    ImprovementReviewApprovedEvent,
    ImprovementReviewRejectedEvent,
    ImprovementReworkRequestedEvent,
    ImprovementRolledBackEvent,
    ImprovementSupersededEvent,
    read_improvement_events,
)
from assurance_agent.workflow.improvements.projection import project_improvements

HISTORICAL_IMPROVEMENTS = (
    Path(__file__).resolve().parents[3] / "fixtures" / "improvements" / "historical-max-length"
)

SOURCE_REFS = ImprovementSourceRefs(problem_ids=("PROB-1",))
VERIFICATION = ImprovementVerification(
    suites=("workflow-full",),
    success_criteria="No truncation",
)

_BASE_ENVELOPE = {
    "schema_version": "1.0",
    "seq": 1,
    "event_id": "IMPEVT-0001",
    "idempotency_key": "IDEM-0001",
    "ts": "2026-07-26T00:00:00Z",
    "improvement_id": "IMP-ABCDEF0123456789FFFF",
    "expected_improvement_version": 0,
}


def _event_data(**overrides: object) -> dict:
    return {**_BASE_ENVELOPE, **overrides}


def _proposed_payload(**overrides: object) -> dict:
    data = _event_data(
        type="improvement_proposed",
        fingerprint="a" * 64,
        fingerprint_version="1",
        kind="workflow_improvement",
        delivery="change_draft",
        source_refs=SOURCE_REFS.model_dump(mode="json"),
        target="assurance_agent/workflow/inspect",
        rationale="Repeated truncation",
        proposed_change="Preserve pytest E lines",
        verification=VERIFICATION.model_dump(mode="json"),
        risk="low",
        confidence="high",
        retro_id="RETRO-1",
        candidate_id="IMP-CAND-1",
        context_sha256="b" * 64,
        candidate_batch_digest="c" * 64,
    )
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Event type coverage
# ---------------------------------------------------------------------------


class TestImprovementProposedEvent:
    def test_round_trip(self) -> None:
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(_proposed_payload())
        assert isinstance(event, ImprovementProposedEvent)
        assert event.kind.value == "workflow_improvement"
        assert event.supersedes is None

    def test_optional_supersedes_and_knowledge_delta(self) -> None:
        data = _proposed_payload(
            kind="domain_knowledge",
            delivery="knowledge_delta",
            supersedes="IMP-OLD",
            knowledge_delta={
                "schema_version": "1",
                "mode": "delta",
                "entities": {"dept": {"required_fields": ["name"]}},
            },
        )
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ImprovementProposedEvent)
        assert event.supersedes == "IMP-OLD"
        assert event.knowledge_delta is not None

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            IMPROVEMENT_EVENT_ADAPTER.validate_python(_proposed_payload(severity="high"))


class TestImprovementEvidenceLinkedEvent:
    def test_round_trip(self) -> None:
        data = _event_data(
            type="improvement_evidence_linked",
            expected_improvement_version=1,
            source_refs=SOURCE_REFS.model_dump(mode="json"),
            retro_id="RETRO-2",
            candidate_id="IMP-CAND-2",
            context_sha256="d" * 64,
            candidate_batch_digest="e" * 64,
        )
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ImprovementEvidenceLinkedEvent)


class TestReviewEvents:
    def test_approved(self) -> None:
        data = _event_data(
            type="improvement_review_approved",
            expected_improvement_version=1,
            who="alice",
            reason="Looks good",
            review_id="REV-1",
        )
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ImprovementReviewApprovedEvent)

    def test_rejected(self) -> None:
        data = _event_data(
            type="improvement_review_rejected",
            expected_improvement_version=1,
            who="bob",
            reason="Too risky",
            review_id="REV-2",
        )
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ImprovementReviewRejectedEvent)

    def test_rework_requested(self) -> None:
        data = _event_data(
            type="improvement_rework_requested",
            expected_improvement_version=1,
            who="carol",
            reason="Need more verification",
            review_id="REV-3",
        )
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ImprovementReworkRequestedEvent)


class TestEvalEvents:
    def test_requested(self) -> None:
        data = _event_data(
            type="improvement_eval_requested",
            expected_improvement_version=2,
            eval_run_id="EVAL-1",
            suites=["workflow-full"],
            staged_sha256="f" * 64,
        )
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ImprovementEvalRequestedEvent)
        assert event.baseline_sha256 is None

    def test_completed_outcomes(self) -> None:
        for outcome in ("passed", "regressed", "awaiting_baseline", "error"):
            data = _event_data(
                type="improvement_eval_completed",
                expected_improvement_version=3,
                eval_run_id="EVAL-1",
                outcome=outcome,
                report_sha256="1" * 64,
                staged_sha256="2" * 64,
                error="boom" if outcome == "error" else None,
            )
            event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
            assert isinstance(event, ImprovementEvalCompletedEvent)
            assert event.outcome == outcome


class TestDeliveryEvents:
    def test_exported(self) -> None:
        data = _event_data(
            type="improvement_exported",
            expected_improvement_version=2,
            artifact_path="qa/improvements/drafts/IMP-1.diff",
            artifact_sha256="3" * 64,
        )
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ImprovementExportedEvent)

    def test_applied(self) -> None:
        data = _event_data(
            type="improvement_applied",
            expected_improvement_version=4,
            target="assurance_agent/workflow/inspect",
            before_sha256="4" * 64,
            after_sha256="5" * 64,
            receipt_sha256="6" * 64,
        )
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ImprovementAppliedEvent)

    def test_rolled_back(self) -> None:
        data = _event_data(
            type="improvement_rolled_back",
            expected_improvement_version=5,
            target="assurance_agent/workflow/inspect",
            restored_sha256="7" * 64,
            reason="eval regressed",
        )
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ImprovementRolledBackEvent)

    def test_superseded(self) -> None:
        data = _event_data(
            type="improvement_superseded",
            expected_improvement_version=1,
            superseded_by="IMP-NEWER",
            reason="replaced by better candidate",
        )
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        assert isinstance(event, ImprovementSupersededEvent)


def test_unknown_type_fails() -> None:
    with pytest.raises(ValidationError):
        IMPROVEMENT_EVENT_ADAPTER.validate_python(_event_data(type="totally_unknown"))


# ---------------------------------------------------------------------------
# Strict JSONL reader
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _proposed_line(seq: int, event_id: str, idem: str) -> dict:
    return _proposed_payload(seq=seq, event_id=event_id, idempotency_key=idem)


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_improvement_events(tmp_path / "missing.jsonl") == []


def test_reads_valid_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_jsonl(
        path,
        [
            _proposed_line(1, "IMPEVT-1", "IDEM-1"),
            _event_data(
                seq=2,
                event_id="IMPEVT-2",
                idempotency_key="IDEM-2",
                type="improvement_review_approved",
                expected_improvement_version=1,
                who="alice",
                reason="ok",
                review_id="REV-1",
            ),
        ],
    )
    events = read_improvement_events(path)
    assert len(events) == 2
    assert isinstance(events[0], ImprovementProposedEvent)
    assert isinstance(events[1], ImprovementReviewApprovedEvent)


def test_rejects_blank_hole(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(_proposed_line(1, "IMPEVT-1", "IDEM-1"))
        + "\n\n"
        + json.dumps(
            _event_data(
                seq=2,
                event_id="IMPEVT-2",
                idempotency_key="IDEM-2",
                type="improvement_review_approved",
                expected_improvement_version=1,
                who="alice",
                reason="ok",
                review_id="REV-1",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ImprovementLedgerIntegrityError, match="blank"):
        read_improvement_events(path)


def test_rejects_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ImprovementLedgerIntegrityError, match="invalid JSON"):
        read_improvement_events(path)


def test_rejects_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ImprovementLedgerIntegrityError, match="not a JSON object"):
        read_improvement_events(path)


def test_rejects_seq_gap(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_jsonl(
        path,
        [
            _proposed_line(1, "IMPEVT-1", "IDEM-1"),
            _proposed_line(3, "IMPEVT-3", "IDEM-3"),
        ],
    )
    with pytest.raises(ImprovementLedgerIntegrityError, match="expected seq 2"):
        read_improvement_events(path)


def test_rejects_duplicate_event_id_with_different_bytes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    first = _proposed_line(1, "IMPEVT-SAME", "IDEM-1")
    second = _proposed_line(2, "IMPEVT-SAME", "IDEM-2")
    second["rationale"] = "different payload bytes"
    _write_jsonl(path, [first, second])
    with pytest.raises(ImprovementLedgerIntegrityError, match="duplicate event_id"):
        read_improvement_events(path)


def test_rejects_unknown_event_type(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    bad = {**_proposed_line(1, "IMPEVT-1", "IDEM-1"), "type": "unknown_type"}
    _write_jsonl(path, [bad])
    with pytest.raises(ImprovementLedgerIntegrityError, match="invalid event"):
        read_improvement_events(path)


def test_rejects_undeclared_fields(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    bad = {**_proposed_line(1, "IMPEVT-1", "IDEM-1"), "rogue_field": True}
    _write_jsonl(path, [bad])
    with pytest.raises(ImprovementLedgerIntegrityError, match="invalid event"):
        read_improvement_events(path)


def test_historical_boolean_max_length_events_and_projections_remain_readable() -> None:
    events_path = HISTORICAL_IMPROVEMENTS / "events.jsonl"
    projection_path = HISTORICAL_IMPROVEMENTS / "improvements.json"
    event_bytes = events_path.read_bytes()
    projection_bytes = projection_path.read_bytes()

    events = read_improvement_events(events_path)
    historical_event = next(event for event in events if event.event_id == "IMPEVT-HISTORICAL-MAX-LENGTH")
    assert isinstance(historical_event, ImprovementProposedEvent)
    assert historical_event.knowledge_delta is not None
    event_constraints = historical_event.knowledge_delta.entities["dept"].constraints
    assert event_constraints is not None
    assert event_constraints["name"]["max_length"] is True

    rebuilt = project_improvements(events)
    rebuilt_delta = rebuilt.improvements["IMP-HISTORICAL-MAX-LENGTH"].knowledge_delta
    assert rebuilt_delta is not None
    rebuilt_constraints = rebuilt_delta.entities["dept"].constraints
    assert rebuilt_constraints is not None
    assert rebuilt_constraints["name"]["max_length"] is True

    persisted = ImprovementLedgerProjection.model_validate_json(projection_bytes)
    persisted_delta = persisted.improvements["IMP-HISTORICAL-MAX-LENGTH"].knowledge_delta
    assert persisted_delta is not None
    persisted_constraints = persisted_delta.entities["dept"].constraints
    assert persisted_constraints is not None
    assert persisted_constraints["name"]["max_length"] is True

    assert events_path.read_bytes() == event_bytes
    assert projection_path.read_bytes() == projection_bytes
