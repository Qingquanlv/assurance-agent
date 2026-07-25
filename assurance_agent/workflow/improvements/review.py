"""Pure Improvement review planning, action validation, and graph operations.

Public API:
    ImprovementReviewContext          — re-exported artifact model
    ReviewContextError                — raised by build_improvement_review_context
    ReviewValidationError             — raised by validate_improvement_review_action
    REVIEW_ACTIONS                    — human actions that append ledger events
    build_improvement_review_context  — build display context from a projection
    validate_improvement_review_action — validate action → typed Improvement event
    load_improvement_review_context_operation  — operation:load-improvement-review-context
    apply_improvement_review_operation         — operation:apply-improvement-review

Hard rules:
    - Apply writes only the Project Improvement Ledger (never qa/issues/**).
    - Context carries source-ref IDs only; Problem snapshots are not expanded.
    - Delivery advice is display-only and never influences canonical events.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementLedgerProjection,
    ImprovementProjection,
    ImprovementReviewAdvice,
    ImprovementReviewContext,
    ImprovementState,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.improvements.events import (
    ImprovementEvent,
    ImprovementReviewApprovedEvent,
    ImprovementReviewRejectedEvent,
    ImprovementReworkRequestedEvent,
    ImprovementSupersededEvent,
)
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore
from assurance_agent.workflow.improvements.transitions import (
    InvalidImprovementTransitionError,
    assert_improvement_transition,
)

# ---------------------------------------------------------------------------
# Supported review actions
# ---------------------------------------------------------------------------

REVIEW_ACTIONS: frozenset[str] = frozenset(
    {"approve", "reject", "request_rework", "supersede"}
)

_ACTION_TARGET: dict[str, ImprovementState] = {
    "approve": ImprovementState.APPROVED,
    "reject": ImprovementState.REJECTED,
    "request_rework": ImprovementState.NEEDS_REWORK,
    "supersede": ImprovementState.SUPERSEDED,
}

_TERMINAL_STATES: frozenset[ImprovementState] = frozenset(
    {ImprovementState.REJECTED, ImprovementState.SUPERSEDED}
)


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class ReviewContextError(Exception):
    """Raised when build_improvement_review_context cannot build a valid context."""


class ReviewValidationError(Exception):
    """Raised when validate_improvement_review_action rejects the action."""


# ---------------------------------------------------------------------------
# Advice + allowed actions
# ---------------------------------------------------------------------------


def _delivery_advice(projection: ImprovementProjection) -> ImprovementReviewAdvice:
    delivery = projection.delivery
    if delivery is DeliveryKind.MEMORY_PATCH:
        return ImprovementReviewAdvice(
            delivery=delivery,
            checklist=(
                "Confirm the target skill memory path is correct",
                "Ensure the proposed patch does not rewrite Issue authority",
            ),
            memory_patch_path=f".aa/memory/{projection.target}.md",
        )
    if delivery is DeliveryKind.CHANGE_DRAFT:
        return ImprovementReviewAdvice(
            delivery=delivery,
            checklist=(
                "Confirm verification suites cover the proposed change",
                "Ensure the draft references source IDs, not Problem copies",
            ),
            change_draft_outline=(
                f"Draft change against {projection.target}: {projection.proposed_change}"
            ),
        )
    if delivery is DeliveryKind.KNOWLEDGE_DELTA:
        summary = "Knowledge delta present" if projection.knowledge_delta is not None else (
            "Knowledge delta missing"
        )
        return ImprovementReviewAdvice(
            delivery=delivery,
            checklist=(
                "Confirm source Problems are human-confirmed or resolved",
                "Review entity/field deltas before promote",
            ),
            knowledge_delta_summary=summary,
        )
    return ImprovementReviewAdvice(delivery=delivery)


def _allowed_actions(state: ImprovementState) -> tuple[str, ...]:
    if state in _TERMINAL_STATES:
        return ()
    allowed: list[str] = []
    for action, target in _ACTION_TARGET.items():
        try:
            assert_improvement_transition(state, target)
        except InvalidImprovementTransitionError:
            continue
        allowed.append(action)
    return tuple(allowed)


# ---------------------------------------------------------------------------
# build_improvement_review_context
# ---------------------------------------------------------------------------


def build_improvement_review_context(
    projection: ImprovementProjection,
) -> ImprovementReviewContext:
    """Build a review context from a live Improvement projection.

    Raises ReviewContextError if the projection identity is invalid.
    """
    if not projection.improvement_id or not projection.improvement_id.strip():
        raise ReviewContextError("improvement_id must be a non-empty string")

    return ImprovementReviewContext(
        improvement_id=projection.improvement_id,
        expected_improvement_version=projection.version,
        state=projection.state,
        kind=projection.kind,
        delivery=projection.delivery,
        source_refs=projection.source_refs,
        target=projection.target,
        proposed_change=projection.proposed_change,
        verification=projection.verification,
        risk=projection.risk,
        confidence=projection.confidence,
        allowed_actions=_allowed_actions(projection.state),
        advice=_delivery_advice(projection),
    )


# ---------------------------------------------------------------------------
# validate_improvement_review_action
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_id(idempotency_key: str) -> str:
    return "IMPEVT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def validate_improvement_review_action(
    projection: ImprovementProjection,
    *,
    action: str,
    reason: str,
    who: str,
    expected_version: int,
    review_id: str,
    superseded_by: str | None = None,
    payload: Mapping[str, object] | None = None,
) -> ImprovementEvent:
    """Validate a human Improvement review action and return one typed event.

    ``payload`` may carry display advice; it is ignored for canonical fields
    except optional ``superseded_by``.
    """
    if superseded_by is None and payload is not None:
        raw_supersede = payload.get("superseded_by")
        if isinstance(raw_supersede, str) and raw_supersede.strip():
            superseded_by = raw_supersede.strip()
    if not action or not action.strip():
        raise ReviewValidationError("action must not be empty")
    if not reason or not reason.strip():
        raise ReviewValidationError("reason must not be empty")
    if not who or not who.strip():
        raise ReviewValidationError("who must not be empty")
    if action not in REVIEW_ACTIONS:
        raise ReviewValidationError(
            f"unsupported review action {action!r}; supported: {sorted(REVIEW_ACTIONS)}"
        )
    if expected_version != projection.version:
        raise ReviewValidationError(
            f"stale review context: improvement {projection.improvement_id!r} is now at "
            f"version {projection.version} but context was built at version {expected_version}"
        )
    if projection.state in _TERMINAL_STATES:
        raise ReviewValidationError(
            f"improvement {projection.improvement_id!r} is in terminal state "
            f"{projection.state.value}; review actions are not allowed"
        )

    target = _ACTION_TARGET[action]
    try:
        assert_improvement_transition(projection.state, target)
    except InvalidImprovementTransitionError as exc:
        raise ReviewValidationError(str(exc)) from exc

    if action != "supersede":
        if not review_id or not review_id.strip():
            raise ReviewValidationError("review_id must be a non-empty string")
    else:
        if not superseded_by or not superseded_by.strip():
            raise ReviewValidationError(
                "supersede requires 'superseded_by' (non-empty string)"
            )
        if superseded_by.strip() == projection.improvement_id:
            raise ReviewValidationError("superseded_by must differ from improvement_id")

    ts = _utc_now()
    expected = expected_version

    if action == "approve":
        idem_key = f"improvement-review:approve:{projection.improvement_id}:{expected}:{review_id}"
        return ImprovementReviewApprovedEvent(
            schema_version="1.0",
            seq=1,
            event_id=_event_id(idem_key),
            idempotency_key=idem_key,
            ts=ts,
            improvement_id=projection.improvement_id,
            expected_improvement_version=expected,
            type="improvement_review_approved",
            who=who.strip(),
            reason=reason.strip(),
            review_id=review_id.strip(),
        )

    if action == "reject":
        idem_key = f"improvement-review:reject:{projection.improvement_id}:{expected}:{review_id}"
        return ImprovementReviewRejectedEvent(
            schema_version="1.0",
            seq=1,
            event_id=_event_id(idem_key),
            idempotency_key=idem_key,
            ts=ts,
            improvement_id=projection.improvement_id,
            expected_improvement_version=expected,
            type="improvement_review_rejected",
            who=who.strip(),
            reason=reason.strip(),
            review_id=review_id.strip(),
        )

    if action == "request_rework":
        idem_key = (
            f"improvement-review:request_rework:{projection.improvement_id}:{expected}:{review_id}"
        )
        return ImprovementReworkRequestedEvent(
            schema_version="1.0",
            seq=1,
            event_id=_event_id(idem_key),
            idempotency_key=idem_key,
            ts=ts,
            improvement_id=projection.improvement_id,
            expected_improvement_version=expected,
            type="improvement_rework_requested",
            who=who.strip(),
            reason=reason.strip(),
            review_id=review_id.strip(),
        )

    # supersede
    assert superseded_by is not None
    idem_key = (
        f"improvement-review:supersede:{projection.improvement_id}:{expected}:"
        f"{superseded_by.strip()}"
    )
    return ImprovementSupersededEvent(
        schema_version="1.0",
        seq=1,
        event_id=_event_id(idem_key),
        idempotency_key=idem_key,
        ts=ts,
        improvement_id=projection.improvement_id,
        expected_improvement_version=expected,
        type="improvement_superseded",
        superseded_by=superseded_by.strip(),
        reason=reason.strip(),
    )


# ---------------------------------------------------------------------------
# Operation helpers
# ---------------------------------------------------------------------------


def _canonical_json(model_dict: object) -> bytes:
    return (
        json.dumps(model_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _write_json(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _load_ledger(project_root: Path) -> ImprovementLedgerProjection:
    path = project_root / "qa" / "improvements" / "improvements.json"
    if not path.is_file():
        raise ValueError(f"improvement ledger projection missing at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"corrupt improvements.json: {exc}") from exc
    return ImprovementLedgerProjection.model_validate(data)


def _find_improvement(
    improvement_id: str, ledger: ImprovementLedgerProjection
) -> ImprovementProjection:
    item = ledger.improvements.get(improvement_id)
    if item is None:
        raise ReviewContextError(
            f"improvement {improvement_id!r} not found in projection "
            f"(ledger has {len(ledger.improvements)} improvement(s))"
        )
    return item


def _read_resume_event(change_dir: Path, invocation_id: str) -> dict[str, object] | None:
    from assurance_agent.workflow.core.events import read_events_strict

    try:
        events = read_events_strict(change_dir)
    except Exception:
        return None
    for event in reversed(events):
        if (
            isinstance(event, dict)
            and event.get("type") == "graph_resumed"
            and event.get("invocation_id") == invocation_id
        ):
            return cast(dict[str, object], event)
    return None


# ---------------------------------------------------------------------------
# load_improvement_review_context_operation
# ---------------------------------------------------------------------------


def load_improvement_review_context_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Write non-canonical review context to change:improvement-review/<id>/context.json."""
    del task
    improvement_id = context.params.get("improvement_id")
    review_id = context.params.get("improvement_review_id")
    if not isinstance(improvement_id, str) or not improvement_id.strip():
        return task_failure(
            "invalid_input",
            "load-improvement-review-context: improvement_id param is required",
        )
    if not isinstance(review_id, str) or not review_id.strip():
        return task_failure(
            "invalid_input",
            "load-improvement-review-context: improvement_review_id param is required",
        )
    improvement_id = improvement_id.strip()
    review_id = review_id.strip()

    try:
        ledger = _load_ledger(workspace.project_root)
        projection = _find_improvement(improvement_id, ledger)
        review_ctx = build_improvement_review_context(projection)
    except (ValueError, ReviewContextError) as exc:
        return task_failure("invalid_input", f"load-improvement-review-context: {exc}")

    context_doc = {
        **review_ctx.model_dump(mode="json"),
        "improvement_review_id": review_id,
        "generated_at": _utc_now(),
    }
    review_dir = workspace.change_dir / "improvement-review" / review_id
    review_dir.mkdir(parents=True, exist_ok=True)
    _write_json(review_dir / "context.json", _canonical_json(context_doc))

    return TaskResult(
        status="succeeded",
        value={
            "improvement_id": improvement_id,
            "improvement_review_id": review_id,
            "improvement_version": review_ctx.expected_improvement_version,
            "state": review_ctx.state.value,
            "allowed_actions": list(review_ctx.allowed_actions),
        },
    )


# ---------------------------------------------------------------------------
# apply_improvement_review_operation
# ---------------------------------------------------------------------------


def apply_improvement_review_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Apply a human Improvement review decision to the project Improvement Ledger.

    Contract: synchronized [project:qa/improvements/**] + exclusive
    [project:improvement-registry]. Never reads or writes qa/issues/**.
    """
    improvement_id = context.params.get("improvement_id")
    review_id = context.params.get("improvement_review_id")
    if not isinstance(improvement_id, str) or not improvement_id.strip():
        return task_failure(
            "invalid_input",
            "apply-improvement-review: improvement_id param is required",
        )
    if not isinstance(review_id, str) or not review_id.strip():
        return task_failure(
            "invalid_input",
            "apply-improvement-review: improvement_review_id param is required",
        )
    improvement_id = improvement_id.strip()
    review_id = review_id.strip()

    review_dir = workspace.change_dir / "improvement-review" / review_id
    context_path = review_dir / "context.json"
    if not context_path.is_file():
        return task_failure(
            "invalid_input",
            f"apply-improvement-review: review context not found at {context_path}; "
            "run load-improvement-review-context first",
        )
    try:
        saved_ctx = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return task_failure(
            "invalid_input", f"apply-improvement-review: corrupt context.json: {exc}"
        )
    saved_version = saved_ctx.get("expected_improvement_version")

    resumed = _read_resume_event(context.change_dir, task.invocation_id)
    if resumed is None:
        return task_failure(
            "invalid_input",
            f"apply-improvement-review: no graph_resumed event found for invocation "
            f"{task.invocation_id}; interrupt must be resolved before apply",
        )
    action = str(resumed.get("action", ""))
    reason = str(resumed.get("reason", ""))
    who = str(resumed.get("who", "unknown"))
    raw_payload = resumed.get("payload")
    payload: dict[str, object] = dict(raw_payload) if isinstance(raw_payload, dict) else {}

    if not action:
        return task_failure("invalid_input", "apply-improvement-review: graph_resumed has no action")
    if action == "stop":
        return task_failure(
            "invalid_input",
            "apply-improvement-review: stop must route to STOP, not apply",
        )

    try:
        ledger = _load_ledger(workspace.project_root)
        projection = _find_improvement(improvement_id, ledger)
    except (ValueError, ReviewContextError) as exc:
        return task_failure("invalid_input", f"apply-improvement-review: {exc}")

    if projection.version != saved_version:
        return task_failure(
            "invalid_input",
            f"apply-improvement-review: stale review context — improvement "
            f"{improvement_id!r} is now at version {projection.version} but context "
            f"was built at version {saved_version}",
        )

    superseded_by = payload.get("superseded_by")
    superseded_by_str = (
        superseded_by.strip()
        if isinstance(superseded_by, str) and superseded_by.strip()
        else None
    )

    try:
        event = validate_improvement_review_action(
            projection,
            action=action,
            reason=reason,
            who=who,
            expected_version=projection.version,
            review_id=review_id,
            superseded_by=superseded_by_str,
            payload=payload,
        )
    except ReviewValidationError as exc:
        return task_failure("invalid_input", f"apply-improvement-review: {exc}")

    try:
        store = ProjectImprovementStore(workspace.project_root)
        store.append_and_rebuild([event])
    except Exception as exc:
        return task_failure(
            "invalid_output",
            f"apply-improvement-review: project improvement ledger write failed: {exc}",
        )

    receipt: dict[str, object] = {
        "improvement_id": improvement_id,
        "improvement_review_id": review_id,
        "action": action,
        "reason": reason,
        "who": who,
        "applied_at": _utc_now(),
        "events_appended": 1,
        "event_ids": [event.event_id],
    }
    _write_json(review_dir / "apply-receipt.json", _canonical_json(receipt))

    return TaskResult(
        status="succeeded",
        value={
            "improvement_id": improvement_id,
            "improvement_review_id": review_id,
            "action": action,
            "events_appended": 1,
        },
    )


__all__ = [
    "REVIEW_ACTIONS",
    "ReviewContextError",
    "ReviewValidationError",
    "ImprovementReviewContext",
    "build_improvement_review_context",
    "validate_improvement_review_action",
    "load_improvement_review_context_operation",
    "apply_improvement_review_operation",
]
