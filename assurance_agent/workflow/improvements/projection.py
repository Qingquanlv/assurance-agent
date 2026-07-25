"""Pure deterministic projections over Improvement ledger events.

All functions are free of filesystem access and clock calls; callers supply
already-validated event sequences and receive frozen Pydantic models.

``dump_projection`` produces canonical JSON (compact separators, sorted keys,
trailing newline) that is byte-identical for the same model on every replay.

Public API:
    project_improvements(events) -> ImprovementLedgerProjection
    project_improvement_review_queue(events) -> ImprovementReviewQueue
    dump_projection(model) -> bytes
    ProjectionError
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from pydantic import BaseModel

from assurance_agent.artifacts.models.improvements import (
    ImprovementLedgerProjection,
    ImprovementProjection,
    ImprovementReviewQueue,
    ImprovementSourceRefs,
    ImprovementState,
)
from assurance_agent.workflow.improvements.events import (
    ImprovementAppliedEvent,
    ImprovementEvalCompletedEvent,
    ImprovementEvalRequestedEvent,
    ImprovementEvent,
    ImprovementEvidenceLinkedEvent,
    ImprovementExportedEvent,
    ImprovementProposedEvent,
    ImprovementReviewApprovedEvent,
    ImprovementReviewRejectedEvent,
    ImprovementReworkRequestedEvent,
    ImprovementRolledBackEvent,
    ImprovementSupersededEvent,
)
from assurance_agent.workflow.improvements.transitions import assert_improvement_transition

_REVIEW_QUEUE_STATES = frozenset(
    {
        ImprovementState.PROPOSED,
        ImprovementState.NEEDS_REWORK,
        ImprovementState.AWAITING_BASELINE,
        ImprovementState.EVAL_ERROR,
    }
)


class ProjectionError(Exception):
    """Raised when an event cannot be applied to the current projected state."""


def _canonical_event_bytes(event: ImprovementEvent) -> bytes:
    data = event.model_dump(mode="json")
    data.pop("seq", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _assert_idempotency(
    event: ImprovementEvent, seen_idempotency: dict[str, bytes]
) -> bool:
    """Record or validate idempotency. Returns True when the event should be skipped."""
    payload = _canonical_event_bytes(event)
    prior = seen_idempotency.get(event.idempotency_key)
    if prior is None:
        seen_idempotency[event.idempotency_key] = payload
        return False
    if prior != payload:
        raise ProjectionError(
            f"idempotency conflict for key {event.idempotency_key!r}: "
            "same key with different event payload"
        )
    return True


def _assert_expected_version(
    event: ImprovementEvent, current: ImprovementProjection | None
) -> None:
    if current is None:
        if not isinstance(event, ImprovementProposedEvent):
            raise ProjectionError(
                f"{event.type}: unknown improvement_id {event.improvement_id!r}"
            )
        if event.expected_improvement_version != 0:
            raise ProjectionError(
                f"improvement_proposed for {event.improvement_id} must have "
                f"expected_improvement_version=0, got {event.expected_improvement_version}"
            )
        return
    if event.expected_improvement_version != current.version:
        raise ProjectionError(
            f"Improvement {event.improvement_id}: expected version "
            f"{event.expected_improvement_version}, found {current.version}"
        )


def _union_source_refs(
    left: ImprovementSourceRefs, right: ImprovementSourceRefs
) -> ImprovementSourceRefs:
    return ImprovementSourceRefs(
        problem_ids=tuple(sorted(set(left.problem_ids) | set(right.problem_ids))),
        occurrence_ids=tuple(sorted(set(left.occurrence_ids) | set(right.occurrence_ids))),
        issue_event_ids=tuple(sorted(set(left.issue_event_ids) | set(right.issue_event_ids))),
        workflow_evidence_ids=tuple(
            sorted(set(left.workflow_evidence_ids) | set(right.workflow_evidence_ids))
        ),
        eval_run_ids=tuple(sorted(set(left.eval_run_ids) | set(right.eval_run_ids))),
    )


def _with_version(
    current: ImprovementProjection,
    *,
    state: ImprovementState | None = None,
    source_refs: ImprovementSourceRefs | None = None,
    proposed_by_retro_ids: tuple[str, ...] | None = None,
    last_event_id: str,
) -> ImprovementProjection:
    target_state = state if state is not None else current.state
    if target_state is not current.state:
        assert_improvement_transition(current.state, target_state)
    return current.model_copy(
        update={
            "state": target_state,
            "source_refs": source_refs if source_refs is not None else current.source_refs,
            "proposed_by_retro_ids": (
                proposed_by_retro_ids
                if proposed_by_retro_ids is not None
                else current.proposed_by_retro_ids
            ),
            "version": current.version + 1,
            "last_event_id": last_event_id,
        }
    )


def _apply_event(
    current: ImprovementProjection | None, event: ImprovementEvent
) -> ImprovementProjection:
    if isinstance(event, ImprovementProposedEvent):
        if current is not None:
            raise ProjectionError(
                f"improvement_proposed: {event.improvement_id} already exists"
            )
        return ImprovementProjection(
            improvement_id=event.improvement_id,
            fingerprint=event.fingerprint,
            fingerprint_version=event.fingerprint_version,
            kind=event.kind,
            delivery=event.delivery,
            source_refs=event.source_refs,
            target=event.target,
            rationale=event.rationale,
            proposed_change=event.proposed_change,
            knowledge_delta=event.knowledge_delta,
            verification=event.verification,
            risk=event.risk,
            confidence=event.confidence,
            state=ImprovementState.PROPOSED,
            version=1,
            proposed_by_retro_ids=(event.retro_id,),
            supersedes=event.supersedes,
            last_event_id=event.event_id,
        )

    if current is None:
        raise ProjectionError(f"{event.type}: unknown improvement_id {event.improvement_id!r}")

    if isinstance(event, ImprovementEvidenceLinkedEvent):
        retros = tuple(sorted({*current.proposed_by_retro_ids, event.retro_id}))
        return _with_version(
            current,
            source_refs=_union_source_refs(current.source_refs, event.source_refs),
            proposed_by_retro_ids=retros,
            last_event_id=event.event_id,
        )

    if isinstance(event, ImprovementReviewApprovedEvent):
        return _with_version(
            current, state=ImprovementState.APPROVED, last_event_id=event.event_id
        )

    if isinstance(event, ImprovementReviewRejectedEvent):
        return _with_version(
            current, state=ImprovementState.REJECTED, last_event_id=event.event_id
        )

    if isinstance(event, ImprovementReworkRequestedEvent):
        return _with_version(
            current, state=ImprovementState.NEEDS_REWORK, last_event_id=event.event_id
        )

    if isinstance(event, ImprovementEvalRequestedEvent):
        return _with_version(
            current, state=ImprovementState.EVALUATING, last_event_id=event.event_id
        )

    if isinstance(event, ImprovementEvalCompletedEvent):
        if event.outcome == "passed":
            # Pin successful staged/report digests in the event log; projection
            # remains evaluating until the later atomic improvement_applied.
            return _with_version(current, last_event_id=event.event_id)
        outcome_state = {
            "regressed": ImprovementState.ROLLED_BACK,
            "awaiting_baseline": ImprovementState.AWAITING_BASELINE,
            "error": ImprovementState.EVAL_ERROR,
        }[event.outcome]
        return _with_version(current, state=outcome_state, last_event_id=event.event_id)

    if isinstance(event, ImprovementExportedEvent):
        return _with_version(
            current, state=ImprovementState.EXPORTED, last_event_id=event.event_id
        )

    if isinstance(event, ImprovementAppliedEvent):
        return _with_version(
            current, state=ImprovementState.APPLIED, last_event_id=event.event_id
        )

    if isinstance(event, ImprovementRolledBackEvent):
        return _with_version(
            current, state=ImprovementState.ROLLED_BACK, last_event_id=event.event_id
        )

    if isinstance(event, ImprovementSupersededEvent):
        return _with_version(
            current, state=ImprovementState.SUPERSEDED, last_event_id=event.event_id
        )

    raise ProjectionError(f"unsupported event type: {type(event).__name__}")


def project_improvements(
    events: Sequence[ImprovementEvent],
) -> ImprovementLedgerProjection:
    """Fold Improvement events into a byte-stable ledger projection."""
    state: dict[str, ImprovementProjection] = {}
    seen_idempotency: dict[str, bytes] = {}
    for event in events:
        if _assert_idempotency(event, seen_idempotency):
            continue
        current = state.get(event.improvement_id)
        _assert_expected_version(event, current)
        state[event.improvement_id] = _apply_event(current, event)
    return ImprovementLedgerProjection(
        schema_version="1",
        last_seq=events[-1].seq if events else 0,
        improvements={key: state[key] for key in sorted(state)},
        by_fingerprint={
            item.fingerprint: item.improvement_id
            for item in sorted(state.values(), key=lambda value: value.fingerprint)
        },
    )


def project_improvement_review_queue(
    events: Sequence[ImprovementEvent],
) -> ImprovementReviewQueue:
    """Derive the review queue from actionable Improvement states."""
    projection = project_improvements(events)
    improvement_ids = tuple(
        improvement_id
        for improvement_id, item in projection.improvements.items()
        if item.state in _REVIEW_QUEUE_STATES
    )
    return ImprovementReviewQueue(schema_version="1", improvement_ids=improvement_ids)


def dump_projection(model: BaseModel) -> bytes:
    """Serialize a projection model as canonical JSON bytes."""
    data = model.model_dump(mode="json")
    return (
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
