"""Tests for pure Improvement ledger projection and review-queue rebuild."""

from __future__ import annotations

import pytest

from assurance_agent.artifacts.models.improvements import ImprovementState
from assurance_agent.workflow.improvements.events import IMPROVEMENT_EVENT_ADAPTER
from assurance_agent.workflow.improvements.projection import (
    ProjectionError,
    dump_projection,
    project_improvement_review_queue,
    project_improvements,
)

IMP_ID = "IMP-ABC"
FINGERPRINT = "a" * 64

_SOURCE_A = {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]}
_SOURCE_B = {"problem_ids": ["PROB-2"], "occurrence_ids": ["OCC-2"]}
_VERIFICATION = {
    "suites": ["workflow-full"],
    "success_criteria": "No truncation",
}


def _envelope(
    *,
    seq: int,
    event_id: str,
    idempotency_key: str,
    expected_version: int,
    improvement_id: str = IMP_ID,
) -> dict:
    return {
        "schema_version": "1.0",
        "seq": seq,
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "ts": f"2026-07-26T00:00:{seq:02d}Z",
        "improvement_id": improvement_id,
        "expected_improvement_version": expected_version,
    }


def _proposed(
    *,
    seq: int = 1,
    event_id: str = "IMPEVT-PROP",
    idempotency_key: str = "IDEM-PROP",
    source_refs: dict | None = None,
    retro_id: str = "RETRO-1",
    improvement_id: str = IMP_ID,
    fingerprint: str = FINGERPRINT,
    supersedes: str | None = None,
) -> dict:
    data = {
        **_envelope(
            seq=seq,
            event_id=event_id,
            idempotency_key=idempotency_key,
            expected_version=0,
            improvement_id=improvement_id,
        ),
        "type": "improvement_proposed",
        "fingerprint": fingerprint,
        "fingerprint_version": "1",
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "source_refs": source_refs or _SOURCE_A,
        "target": "assurance_agent/workflow/inspect",
        "rationale": "Repeated truncation",
        "proposed_change": "Preserve pytest E lines",
        "verification": _VERIFICATION,
        "risk": "low",
        "confidence": "high",
        "retro_id": retro_id,
        "candidate_id": "IMP-CAND-1",
        "context_sha256": "b" * 64,
        "candidate_batch_digest": "c" * 64,
    }
    if supersedes is not None:
        data["supersedes"] = supersedes
    return data


def _evidence_linked(
    *,
    seq: int = 2,
    event_id: str = "IMPEVT-EVID",
    idempotency_key: str = "IDEM-EVID",
    expected_version: int = 1,
    source_refs: dict | None = None,
    retro_id: str = "RETRO-2",
) -> dict:
    return {
        **_envelope(
            seq=seq,
            event_id=event_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        ),
        "type": "improvement_evidence_linked",
        "source_refs": source_refs or _SOURCE_B,
        "retro_id": retro_id,
        "candidate_id": "IMP-CAND-2",
        "context_sha256": "d" * 64,
        "candidate_batch_digest": "e" * 64,
    }


def _mk(*dicts: dict):
    return [IMPROVEMENT_EVENT_ADAPTER.validate_python(item) for item in dicts]


def test_projection_links_evidence_without_duplicate_improvement() -> None:
    events = _mk(_proposed(), _evidence_linked())
    projection = project_improvements(events)
    assert tuple(projection.improvements) == (IMP_ID,)
    item = projection.improvements[IMP_ID]
    assert item.source_refs.problem_ids == ("PROB-1", "PROB-2")
    assert item.source_refs.occurrence_ids == ("OCC-1", "OCC-2")
    assert item.state is ImprovementState.PROPOSED
    assert item.version == 2
    assert item.proposed_by_retro_ids == ("RETRO-1", "RETRO-2")
    assert projection.by_fingerprint[FINGERPRINT] == IMP_ID


def test_by_fingerprint_aliases_are_sorted_and_stable() -> None:
    events = _mk(
        _proposed(
            seq=1,
            event_id="E1",
            idempotency_key="I1",
            improvement_id="IMP-ZZZ",
            fingerprint="f" * 64,
            source_refs={"problem_ids": ["PROB-Z"]},
            retro_id="RETRO-Z",
        ),
        _proposed(
            seq=2,
            event_id="E2",
            idempotency_key="I2",
            improvement_id="IMP-AAA",
            fingerprint="0" * 64,
            source_refs={"problem_ids": ["PROB-A"]},
            retro_id="RETRO-A",
        ),
    )
    projection = project_improvements(events)
    assert tuple(projection.improvements) == ("IMP-AAA", "IMP-ZZZ")
    assert list(projection.by_fingerprint) == sorted(projection.by_fingerprint)
    assert projection.by_fingerprint["0" * 64] == "IMP-AAA"
    assert projection.by_fingerprint["f" * 64] == "IMP-ZZZ"


def test_review_queue_contains_actionable_states_sorted() -> None:
    events = _mk(
        _proposed(
            seq=1,
            event_id="E1",
            idempotency_key="I1",
            improvement_id="IMP-B",
            fingerprint="1" * 64,
            source_refs={"problem_ids": ["PROB-B"]},
            retro_id="RETRO-B",
        ),
        _proposed(
            seq=2,
            event_id="E2",
            idempotency_key="I2",
            improvement_id="IMP-A",
            fingerprint="2" * 64,
            source_refs={"problem_ids": ["PROB-A"]},
            retro_id="RETRO-A",
        ),
        {
            **_envelope(
                seq=3,
                event_id="E3",
                idempotency_key="I3",
                expected_version=1,
                improvement_id="IMP-A",
            ),
            "type": "improvement_review_approved",
            "who": "alice",
            "reason": "ok",
            "review_id": "REV-1",
        },
        {
            **_envelope(
                seq=4,
                event_id="E4",
                idempotency_key="I4",
                expected_version=1,
                improvement_id="IMP-B",
            ),
            "type": "improvement_rework_requested",
            "who": "bob",
            "reason": "more evidence",
            "review_id": "REV-2",
        },
        _proposed(
            seq=5,
            event_id="E5",
            idempotency_key="I5",
            improvement_id="IMP-C",
            fingerprint="3" * 64,
            source_refs={"problem_ids": ["PROB-C"]},
            retro_id="RETRO-C",
        ),
    )
    queue = project_improvement_review_queue(events)
    # IMP-A approved (excluded), IMP-B needs_rework, IMP-C proposed
    assert queue.improvement_ids == ("IMP-B", "IMP-C")


def test_state_transitions_through_eval_outcomes() -> None:
    base = [
        _proposed(),
        {
            **_envelope(seq=2, event_id="E2", idempotency_key="I2", expected_version=1),
            "type": "improvement_review_approved",
            "who": "alice",
            "reason": "ok",
            "review_id": "REV-1",
        },
        {
            **_envelope(seq=3, event_id="E3", idempotency_key="I3", expected_version=2),
            "type": "improvement_eval_requested",
            "eval_run_id": "EVAL-1",
            "suites": ["workflow-full"],
            "staged_sha256": "1" * 64,
        },
    ]

    passed = project_improvements(
        _mk(
            *base,
            {
                **_envelope(seq=4, event_id="E4", idempotency_key="I4", expected_version=3),
                "type": "improvement_eval_completed",
                "eval_run_id": "EVAL-1",
                "outcome": "passed",
                "report_sha256": "2" * 64,
                "staged_sha256": "1" * 64,
            },
            {
                **_envelope(seq=5, event_id="E5", idempotency_key="I5", expected_version=4),
                "type": "improvement_applied",
                "target": "assurance_agent/workflow/inspect",
                "before_sha256": "3" * 64,
                "after_sha256": "4" * 64,
                "receipt_sha256": "5" * 64,
            },
        )
    )
    assert passed.improvements[IMP_ID].state is ImprovementState.APPLIED
    assert passed.improvements[IMP_ID].version == 5

    for outcome, expected_state in (
        ("regressed", ImprovementState.ROLLED_BACK),
        ("awaiting_baseline", ImprovementState.AWAITING_BASELINE),
        ("error", ImprovementState.EVAL_ERROR),
    ):
        projection = project_improvements(
            _mk(
                *base,
                {
                    **_envelope(seq=4, event_id="E4", idempotency_key="I4", expected_version=3),
                    "type": "improvement_eval_completed",
                    "eval_run_id": "EVAL-1",
                    "outcome": outcome,
                    "report_sha256": "2" * 64,
                    "staged_sha256": "1" * 64,
                    "error": "boom" if outcome == "error" else None,
                },
            )
        )
        assert projection.improvements[IMP_ID].state is expected_state


def test_recovery_eval_from_awaiting_baseline_and_eval_error() -> None:
    events = _mk(
        _proposed(),
        {
            **_envelope(seq=2, event_id="E2", idempotency_key="I2", expected_version=1),
            "type": "improvement_review_approved",
            "who": "alice",
            "reason": "ok",
            "review_id": "REV-1",
        },
        {
            **_envelope(seq=3, event_id="E3", idempotency_key="I3", expected_version=2),
            "type": "improvement_eval_requested",
            "eval_run_id": "EVAL-1",
            "suites": ["workflow-full"],
            "staged_sha256": "1" * 64,
        },
        {
            **_envelope(seq=4, event_id="E4", idempotency_key="I4", expected_version=3),
            "type": "improvement_eval_completed",
            "eval_run_id": "EVAL-1",
            "outcome": "awaiting_baseline",
            "report_sha256": "2" * 64,
            "staged_sha256": "1" * 64,
        },
        {
            **_envelope(seq=5, event_id="E5", idempotency_key="I5", expected_version=4),
            "type": "improvement_eval_requested",
            "eval_run_id": "EVAL-2",
            "suites": ["workflow-full"],
            "staged_sha256": "6" * 64,
            "baseline_sha256": "7" * 64,
        },
        {
            **_envelope(seq=6, event_id="E6", idempotency_key="I6", expected_version=5),
            "type": "improvement_eval_completed",
            "eval_run_id": "EVAL-2",
            "outcome": "error",
            "report_sha256": "8" * 64,
            "staged_sha256": "6" * 64,
            "error": "timeout",
        },
        {
            **_envelope(seq=7, event_id="E7", idempotency_key="I7", expected_version=6),
            "type": "improvement_eval_requested",
            "eval_run_id": "EVAL-3",
            "suites": ["workflow-full"],
            "staged_sha256": "9" * 64,
            "baseline_sha256": "7" * 64,
        },
    )
    projection = project_improvements(events)
    assert projection.improvements[IMP_ID].state is ImprovementState.EVALUATING
    queue = project_improvement_review_queue(events)
    assert IMP_ID not in queue.improvement_ids


def test_stale_version_and_unknown_id_raise() -> None:
    with pytest.raises(ProjectionError, match="expected version"):
        project_improvements(
            _mk(
                _proposed(),
                _evidence_linked(expected_version=99),
            )
        )
    with pytest.raises(ProjectionError, match="unknown"):
        project_improvements(_mk(_evidence_linked(expected_version=0)))


def test_idempotent_duplicate_event_is_skipped() -> None:
    proposed = _proposed()
    # Same idempotency identity (including event_id); only seq differs.
    events = _mk(proposed, {**proposed, "seq": 2})
    projection = project_improvements(events)
    assert projection.improvements[IMP_ID].version == 1
    assert projection.last_seq == 2


def test_idempotency_conflict_raises() -> None:
    first = _proposed()
    conflict = {
        **first,
        "seq": 2,
        "event_id": "IMPEVT-CONFLICT",
        "rationale": "Different rationale under same key",
    }
    with pytest.raises(ProjectionError, match="idempotency"):
        project_improvements(_mk(first, conflict))


def test_dump_projection_is_byte_stable() -> None:
    events = _mk(_proposed(), _evidence_linked())
    first = dump_projection(project_improvements(events))
    second = dump_projection(project_improvements(events))
    assert first == second
    assert first.endswith(b"\n")


def test_empty_events_yield_empty_ledger() -> None:
    projection = project_improvements([])
    assert projection.last_seq == 0
    assert projection.improvements == {}
    assert projection.by_fingerprint == {}
    assert project_improvement_review_queue([]).improvement_ids == ()
