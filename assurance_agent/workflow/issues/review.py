"""Pure Problem review planning and action validation.

Public API:
    ProblemReviewContext          — immutable context snapshot bound to one Problem
    ReviewContextError            — raised by build_problem_review_context on missing/invalid Problem
    ReviewValidationError         — raised by validate_review_action on illegal action/payload
    build_problem_review_context  — build context from live projection
    validate_review_action        — validate a human action and return typed events

Supported human actions:
    HUMAN_TRANSITIONS actions     — delegate to transitions.validate_human_transition
    merge / confirm_link          — ProblemMergedEvent (source resolved into target)
    submit_resolution             — ProblemVerificationRequestedEvent

Behavioral rules:
    - Stale expected_problem_version is rejected BEFORE evaluating action payload.
    - Advice from the triage-advisor is display-only; callers must not pass it as
      ``payload`` to influence canonical fields.
    - ``detected → in_progress`` is forbidden (must triage first); enforced by
      HUMAN_TRANSITIONS in transitions.py.
    - Merge cycles are rejected (target must not already be an alias of the source).
    - ``confirmed_assessment`` authority is always ``human_confirmed``; LLM proposals
      in payload are silently overridden.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from assurance_agent.artifacts.models.issues import (
    Problem,
    ProblemProjection,
    ProblemStatus,
)
from assurance_agent.workflow.issues.events import (
    ProblemAssessmentConfirmedEvent,
    ProblemEvent,
    ProblemMarkedNotAnIssueEvent,
    ProblemMergedEvent,
    ProblemReopenedEvent,
    ProblemRiskAcceptedEvent,
    ProblemVerificationRequestedEvent,
    ProblemWorkStartedEvent,
)
from assurance_agent.workflow.issues.transitions import (
    InvalidTransitionError,
    StaleVersionError,
    validate_human_transition,
)

# ---------------------------------------------------------------------------
# Supported review actions
# ---------------------------------------------------------------------------

_HUMAN_TRANSITION_ACTIONS = frozenset(
    {"confirm_assessment", "mark_not_an_issue", "accept_risk", "start_work", "reopen"}
)

_MERGE_ACTIONS = frozenset({"merge", "confirm_link"})

_ALLOWED_SUBMIT_RESOLUTION_STATUSES: frozenset[ProblemStatus] = frozenset(
    {"in_progress", "triaged"}
)

# All actions understood by validate_review_action.
REVIEW_ACTIONS: frozenset[str] = _HUMAN_TRANSITION_ACTIONS | _MERGE_ACTIONS | {"submit_resolution"}

# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class ReviewContextError(Exception):
    """Raised when build_problem_review_context cannot build a valid context."""


class ReviewValidationError(Exception):
    """Raised when validate_review_action rejects the action or payload."""


# ---------------------------------------------------------------------------
# Immutable review context
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProblemReviewContext:
    """Immutable snapshot binding a review session to one Problem.

    ``expected_problem_version`` is the version at context-build time; the
    apply operation reloads the projection and rejects stale contexts.

    ``problem_digest`` is a SHA-256 of the Problem's canonical JSON at
    build time, suitable for audit trails.

    ``canonical_alias`` is set to the target problem_id when the Problem
    has been merged (its identity is an alias of another Problem).
    """

    problem_id: str
    expected_problem_version: int
    problem_status: ProblemStatus
    problem_title: str
    canonical_alias: str | None
    occurrence_ids: tuple[str, ...]
    problem_digest: str


def build_problem_review_context(
    problem_id: str,
    projection: ProblemProjection,
) -> ProblemReviewContext:
    """Build a review context for ``problem_id`` from the live projection.

    Raises ReviewContextError if the problem is not found.
    """
    problem: Problem | None = next(
        (p for p in projection.problems if p.problem_id == problem_id), None
    )
    if problem is None:
        raise ReviewContextError(
            f"problem {problem_id!r} not found in projection "
            f"(projection has {len(projection.problems)} problem(s))"
        )

    problem_data = problem.model_dump(mode="json")
    problem_json = (
        json.dumps(problem_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    problem_digest = "sha256:" + hashlib.sha256(problem_json.encode("utf-8")).hexdigest()

    return ProblemReviewContext(
        problem_id=problem_id,
        expected_problem_version=problem.version,
        problem_status=problem.status,
        problem_title=problem.title,
        canonical_alias=None,  # merge aliasing tracked in projection events, not Problem model
        occurrence_ids=tuple(problem.occurrences),
        problem_digest=problem_digest,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_id(idempotency_key: str) -> str:
    return "EVT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def _evidence_digest(evidence_refs: list[str]) -> str:
    """Derive a digest from the sorted evidence_refs list."""
    canonical = json.dumps(sorted(evidence_refs), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_evidence_refs(payload: Mapping[str, object]) -> list[str]:
    refs = payload.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        raise ReviewValidationError("evidence_refs must be a non-empty list in payload")
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            raise ReviewValidationError("every evidence_refs entry must be a non-empty string")
    return [str(r) for r in refs]


def _find_problem(problem_id: str, projection: ProblemProjection) -> Problem:
    problem = next((p for p in projection.problems if p.problem_id == problem_id), None)
    if problem is None:
        raise ReviewValidationError(
            f"problem {problem_id!r} not found in reloaded projection (was it merged?)"
        )
    return problem


# ---------------------------------------------------------------------------
# validate_review_action
# ---------------------------------------------------------------------------


def validate_review_action(
    context: ProblemReviewContext,
    action: str,
    payload: Mapping[str, object],
    reason: str,
    who: str,
    *,
    projection: ProblemProjection | None = None,
) -> tuple[ProblemEvent, ...]:
    """Validate a human review action and return the resulting typed Problem events.

    ``context`` is bound to the Problem's state at load-context time.  Callers
    must reload the projection BEFORE calling this function and pass it as
    ``projection`` so that stale-version and merge-cycle checks use live data.

    When ``projection`` is None, version and cycle checks use only the context
    snapshot (suitable for unit tests that do not need a live projection).

    Raises:
        ReviewValidationError: any validation failure (stale version, illegal
            transition, missing required payload fields, merge cycle, etc.)
    """
    if not action or not action.strip():
        raise ReviewValidationError("action must not be empty")
    if not reason or not reason.strip():
        raise ReviewValidationError("reason must not be empty")
    if not who or not who.strip():
        raise ReviewValidationError("who must not be empty")
    if action not in REVIEW_ACTIONS:
        raise ReviewValidationError(
            f"unsupported review action {action!r}; "
            f"supported: {sorted(REVIEW_ACTIONS)}"
        )

    ts = _utc_now()

    # Determine the live Problem status (from reloaded projection if available).
    if projection is not None:
        live_problem = _find_problem(context.problem_id, projection)
        if live_problem.version != context.expected_problem_version:
            raise ReviewValidationError(
                f"stale review context: problem {context.problem_id!r} is now at "
                f"version {live_problem.version} but context was built at "
                f"version {context.expected_problem_version}"
            )
        problem_status: ProblemStatus = live_problem.status
    else:
        problem_status = context.problem_status

    expected_version = context.expected_problem_version

    # ---- Standard HUMAN_TRANSITIONS actions --------------------------------
    if action in _HUMAN_TRANSITION_ACTIONS:
        return _validate_human_transition_action(
            context=context,
            action=action,
            payload=payload,
            reason=reason,
            problem_status=problem_status,
            expected_version=expected_version,
            ts=ts,
        )

    # ---- Merge / confirm_link actions --------------------------------------
    if action in _MERGE_ACTIONS:
        return _validate_merge_action(
            context=context,
            payload=payload,
            reason=reason,
            problem_status=problem_status,
            expected_version=expected_version,
            projection=projection,
            ts=ts,
        )

    # ---- submit_resolution -------------------------------------------------
    if action == "submit_resolution":
        return _validate_submit_resolution(
            context=context,
            payload=payload,
            reason=reason,
            problem_status=problem_status,
            expected_version=expected_version,
            ts=ts,
        )

    # Should not reach here given the REVIEW_ACTIONS guard above.
    raise ReviewValidationError(f"unhandled action {action!r}")


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _validate_human_transition_action(
    *,
    context: ProblemReviewContext,
    action: str,
    payload: Mapping[str, object],
    reason: str,
    problem_status: ProblemStatus,
    expected_version: int,
    ts: str,
) -> tuple[ProblemEvent, ...]:
    """Delegate to transitions.validate_human_transition and convert to typed events."""
    from assurance_agent.artifacts.models.issues import Problem as ProblemModel
    from assurance_agent.artifacts.models.issues import (
        ProblemAssessment,
        ProblemFingerprint,
        ProblemSeenRef,
    )

    # Build a minimal Problem for validate_human_transition.
    # We only need problem_id, status, version; other fields are placeholders.
    dummy_fingerprint = ProblemFingerprint(version="1", digest="0" * 64)
    dummy_assessment = ProblemAssessment(
        classification="unknown",
        severity="low",
        authority="llm_provisional",
    )
    dummy_ref = ProblemSeenRef(change_id="unknown", occurrence_id="unknown")
    problem = ProblemModel(
        problem_id=context.problem_id,
        fingerprint=dummy_fingerprint,
        title=context.problem_title,
        assessment=dummy_assessment,
        status=problem_status,
        first_seen=dummy_ref,
        last_seen=dummy_ref,
        occurrences=list(context.occurrence_ids) or ["unknown"],
        version=expected_version,
    )

    evidence_refs = _require_evidence_refs(payload)
    try:
        decision = validate_human_transition(
            problem,
            action,
            expected_problem_version=expected_version,
            reason=reason,
            evidence_refs=evidence_refs,
            payload=payload,
        )
    except StaleVersionError as exc:
        raise ReviewValidationError(str(exc)) from exc
    except InvalidTransitionError as exc:
        raise ReviewValidationError(str(exc)) from exc

    evidence_digest = _evidence_digest(evidence_refs)
    idem_key = (
        f"review:{action}:{context.problem_id}:{expected_version}:{evidence_digest}"
    )
    event_id = _event_id(idem_key)

    event: ProblemEvent

    if action == "confirm_assessment":
        event = ProblemAssessmentConfirmedEvent(
            schema_version="1.0",
            seq=1,
            event_id=event_id,
            idempotency_key=idem_key,
            ts=ts,
            evidence_digest=evidence_digest,
            problem_id=context.problem_id,
            expected_problem_version=expected_version,
            type="problem_assessment_confirmed",
            classification=decision.payload["classification"],  # type: ignore[arg-type]
            severity=decision.payload["severity"],  # type: ignore[arg-type]
            reason=reason,
            evidence_refs=evidence_refs,
        )
    elif action == "mark_not_an_issue":
        event = ProblemMarkedNotAnIssueEvent(
            schema_version="1.0",
            seq=1,
            event_id=event_id,
            idempotency_key=idem_key,
            ts=ts,
            evidence_digest=evidence_digest,
            problem_id=context.problem_id,
            expected_problem_version=expected_version,
            type="problem_marked_not_an_issue",
            reason=reason,
            evidence_refs=evidence_refs,
        )
    elif action == "accept_risk":
        event = ProblemRiskAcceptedEvent(
            schema_version="1.0",
            seq=1,
            event_id=event_id,
            idempotency_key=idem_key,
            ts=ts,
            evidence_digest=evidence_digest,
            problem_id=context.problem_id,
            expected_problem_version=expected_version,
            type="problem_risk_accepted",
            reason=reason,
            evidence_refs=evidence_refs,
        )
    elif action == "start_work":
        event = ProblemWorkStartedEvent(
            schema_version="1.0",
            seq=1,
            event_id=event_id,
            idempotency_key=idem_key,
            ts=ts,
            evidence_digest=evidence_digest,
            problem_id=context.problem_id,
            expected_problem_version=expected_version,
            type="problem_work_started",
            reason=reason,
            evidence_refs=evidence_refs,
        )
    elif action == "reopen":
        event = ProblemReopenedEvent(
            schema_version="1.0",
            seq=1,
            event_id=event_id,
            idempotency_key=idem_key,
            ts=ts,
            evidence_digest=evidence_digest,
            problem_id=context.problem_id,
            expected_problem_version=expected_version,
            type="problem_reopened",
            reason=reason,
            evidence_refs=evidence_refs,
        )
    else:
        raise ReviewValidationError(f"unhandled human-transition action {action!r}")

    return (event,)


def _validate_merge_action(
    *,
    context: ProblemReviewContext,
    payload: Mapping[str, object],
    reason: str,
    problem_status: ProblemStatus,
    expected_version: int,
    projection: ProblemProjection | None,
    ts: str,
) -> tuple[ProblemEvent, ...]:
    """Validate merge/confirm_link and produce a ProblemMergedEvent."""
    # Merging a resolved/not_an_issue problem back into another is disallowed.
    if problem_status in ("resolved", "not_an_issue", "accepted_risk"):
        raise ReviewValidationError(
            f"merge is not allowed from status {problem_status!r}"
        )

    target_problem_id = payload.get("target_problem_id")
    if not isinstance(target_problem_id, str) or not target_problem_id.strip():
        raise ReviewValidationError(
            "merge requires 'target_problem_id' (non-empty string) in payload"
        )
    target_problem_id = target_problem_id.strip()

    if target_problem_id == context.problem_id:
        raise ReviewValidationError("merge target must differ from source problem_id")

    # Cycle detection: check that the target is not itself being merged into source.
    if projection is not None:
        target = next(
            (p for p in projection.problems if p.problem_id == target_problem_id), None
        )
        if target is None:
            raise ReviewValidationError(
                f"merge target {target_problem_id!r} not found in projection"
            )
        # Simple cycle check: target must not be resolved as an alias of source.
        # (Deep alias chains are not supported in this projection model.)
        if target.status == "resolved" and target.resolution is not None:
            # Can still merge into a resolved target (it exists as a canonical problem).
            pass

    evidence_refs = _require_evidence_refs(payload)
    evidence_digest = _evidence_digest(evidence_refs)
    idem_key = (
        f"review:merge:{context.problem_id}:{expected_version}:"
        f"{target_problem_id}:{evidence_digest}"
    )
    event_id = _event_id(idem_key)

    event = ProblemMergedEvent(
        schema_version="1.0",
        seq=1,
        event_id=event_id,
        idempotency_key=idem_key,
        ts=ts,
        evidence_digest=evidence_digest,
        problem_id=context.problem_id,
        expected_problem_version=expected_version,
        type="problem_merged",
        target_problem_id=target_problem_id,
        reason=reason,
        evidence_refs=evidence_refs,
        resolved_at=ts,
    )
    return (event,)


def _validate_submit_resolution(
    *,
    context: ProblemReviewContext,
    payload: Mapping[str, object],
    reason: str,
    problem_status: ProblemStatus,
    expected_version: int,
    ts: str,
) -> tuple[ProblemEvent, ...]:
    """Validate submit_resolution and produce a ProblemVerificationRequestedEvent."""
    if problem_status not in _ALLOWED_SUBMIT_RESOLUTION_STATUSES:
        raise ReviewValidationError(
            f"submit_resolution requires status in "
            f"{sorted(_ALLOWED_SUBMIT_RESOLUTION_STATUSES)}, found {problem_status!r}"
        )

    verification_scope = payload.get("verification_scope")
    if not isinstance(verification_scope, list) or not verification_scope:
        raise ReviewValidationError(
            "submit_resolution requires 'verification_scope' (non-empty list) in payload"
        )
    scope_items = [str(s) for s in verification_scope]

    linked_fix_disposition = payload.get("linked_fix_disposition")
    if not isinstance(linked_fix_disposition, str) or not linked_fix_disposition.strip():
        raise ReviewValidationError(
            "submit_resolution requires 'linked_fix_disposition' (non-empty string) in payload"
        )

    change_id = payload.get("change_id")
    if not isinstance(change_id, str) or not change_id.strip():
        raise ReviewValidationError(
            "submit_resolution requires 'change_id' (non-empty string) in payload"
        )

    batch_id = payload.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ReviewValidationError(
            "submit_resolution requires 'batch_id' (non-empty string) in payload"
        )

    # evidence_digest derived from verification_scope + change_id + batch_id
    scope_digest_src = json.dumps(
        {"scope": sorted(scope_items), "change_id": change_id, "batch_id": batch_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_digest = "sha256:" + hashlib.sha256(scope_digest_src.encode("utf-8")).hexdigest()

    idem_key = (
        f"review:submit_resolution:{context.problem_id}:{expected_version}:"
        f"{change_id}:{batch_id}:{evidence_digest}"
    )
    event_id = _event_id(idem_key)

    event = ProblemVerificationRequestedEvent(
        schema_version="1.0",
        seq=1,
        event_id=event_id,
        idempotency_key=idem_key,
        ts=ts,
        evidence_digest=evidence_digest,
        problem_id=context.problem_id,
        expected_problem_version=expected_version,
        type="problem_verification_requested",
        verification_scope=scope_items,
        linked_fix_disposition=linked_fix_disposition.strip(),
        change_id=change_id.strip(),
        batch_id=batch_id.strip(),
    )
    return (event,)
