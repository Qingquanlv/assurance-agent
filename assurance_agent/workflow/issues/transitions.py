"""Pure Problem lifecycle transition validation.

Human actions are limited to ``HUMAN_TRANSITIONS``. Resolution and regression
are dedicated deterministic transitions; callers cannot force those statuses
through human payload fields. ``detected -> in_progress`` is intentionally
absent from ``HUMAN_TRANSITIONS`` because triage must happen first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from assurance_agent.artifacts.models.issues import (
    IssueClassification,
    IssueSeverity,
    Problem,
    ProblemStatus,
)

HUMAN_TRANSITIONS: dict[str, dict[ProblemStatus, ProblemStatus]] = {
    "confirm_assessment": {"detected": "triaged"},
    "mark_not_an_issue": {"detected": "not_an_issue", "triaged": "not_an_issue"},
    "accept_risk": {
        "triaged": "accepted_risk",
        "in_progress": "accepted_risk",
        "verification_pending": "accepted_risk",
    },
    "start_work": {"triaged": "in_progress", "accepted_risk": "in_progress"},
    "reopen": {"not_an_issue": "detected"},
}

_ACTION_EVENT_TYPES: dict[str, str] = {
    "confirm_assessment": "problem_assessment_confirmed",
    "mark_not_an_issue": "problem_marked_not_an_issue",
    "accept_risk": "problem_risk_accepted",
    "start_work": "problem_work_started",
    "reopen": "problem_reopened",
}

_FORBIDDEN_PAYLOAD_KEYS = frozenset({"status", "version", "authority"})


class TransitionError(Exception):
    """Base class for lifecycle transition validation failures."""


class StaleVersionError(TransitionError):
    """Raised when ``expected_problem_version`` does not match the Problem."""


class InvalidTransitionError(TransitionError):
    """Raised when an action or resolution/regression precondition fails."""


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    event_type: str
    problem_id: str
    expected_version: int
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    linked_fix_exists: bool
    authoritative_batch_selected_target: bool
    verification_cases_executed: bool
    verification_cases_passed: bool
    fingerprint_absent_in_batch: bool

    def is_complete(self) -> bool:
        return all(
            (
                self.linked_fix_exists,
                self.authoritative_batch_selected_target,
                self.verification_cases_executed,
                self.verification_cases_passed,
                self.fingerprint_absent_in_batch,
            )
        )


def _require_current_version(problem: Problem, expected_problem_version: int) -> None:
    if expected_problem_version != problem.version:
        raise StaleVersionError(
            f"expected problem version {expected_problem_version}, found {problem.version}"
        )


def _reject_forbidden_payload_keys(payload: Mapping[str, object]) -> None:
    forbidden = sorted(key for key in payload if key in _FORBIDDEN_PAYLOAD_KEYS)
    if forbidden:
        joined = ", ".join(forbidden)
        raise InvalidTransitionError(f"payload must not set canonical fields: {joined}")


def _require_reason_and_evidence(reason: str, evidence_refs: Sequence[str]) -> None:
    if not reason.strip():
        raise InvalidTransitionError("reason must not be empty")
    if not evidence_refs:
        raise InvalidTransitionError("evidence_refs must not be empty")


def _extract_assessment(payload: Mapping[str, object]) -> tuple[IssueClassification, IssueSeverity]:
    classification = payload.get("classification")
    severity = payload.get("severity")
    if classification not in {
        "product_bug",
        "test_bug",
        "test_data_issue",
        "environment_issue",
        "coverage_gap",
        "performance_issue",
        "workflow_issue",
        "unknown",
    }:
        raise InvalidTransitionError("classification is required for confirm_assessment")
    if severity not in {"critical", "high", "medium", "low"}:
        raise InvalidTransitionError("severity is required for confirm_assessment")
    return cast(IssueClassification, classification), cast(IssueSeverity, severity)


def validate_human_transition(
    problem: Problem,
    action: str,
    *,
    expected_problem_version: int,
    reason: str,
    evidence_refs: Sequence[str],
    payload: Mapping[str, object] | None = None,
) -> TransitionDecision:
    _require_current_version(problem, expected_problem_version)

    transitions = HUMAN_TRANSITIONS.get(action)
    if transitions is None:
        raise InvalidTransitionError(f"unsupported human action: {action}")

    next_status = transitions.get(problem.status)
    if next_status is None:
        raise InvalidTransitionError(f"action {action} is not allowed from status {problem.status}")

    _require_reason_and_evidence(reason, evidence_refs)

    payload_data = dict(payload or ())
    _reject_forbidden_payload_keys(payload_data)

    decision_payload: dict[str, object] = {
        "next_status": next_status,
        "reason": reason.strip(),
        "evidence_refs": list(evidence_refs),
    }

    if action == "confirm_assessment":
        classification, severity = _extract_assessment(payload_data)
        decision_payload["classification"] = classification
        decision_payload["severity"] = severity
        decision_payload["authority"] = "human_confirmed"

    return TransitionDecision(
        event_type=_ACTION_EVENT_TYPES[action],
        problem_id=problem.problem_id,
        expected_version=expected_problem_version,
        payload=decision_payload,
    )


def plan_resolution(
    problem: Problem,
    *,
    expected_problem_version: int,
    linked_fix_disposition: str,
    verification_scope: Sequence[str],
    verification_evidence_digest: str,
    change_id: str,
    batch_id: str,
    context: ResolutionContext,
) -> TransitionDecision:
    _require_current_version(problem, expected_problem_version)

    if problem.status != "verification_pending":
        raise InvalidTransitionError(
            f"resolution requires status verification_pending, found {problem.status}"
        )

    if not verification_scope:
        raise InvalidTransitionError("verification_scope must not be empty")

    if not context.is_complete():
        raise InvalidTransitionError("verification evidence is incomplete")

    return TransitionDecision(
        event_type="problem_resolved",
        problem_id=problem.problem_id,
        expected_version=expected_problem_version,
        payload={
            "next_status": "resolved",
            "disposition": linked_fix_disposition,
            "verification_scope": list(verification_scope),
            "evidence_digest": verification_evidence_digest,
            "change_id": change_id,
            "batch_id": batch_id,
        },
    )


def plan_regression(
    problem: Problem,
    *,
    expected_problem_version: int,
    occurrence_id: str,
    change_id: str,
) -> TransitionDecision:
    _require_current_version(problem, expected_problem_version)

    if problem.status != "resolved":
        raise InvalidTransitionError(f"regression requires status resolved, found {problem.status}")

    if not occurrence_id.strip():
        raise InvalidTransitionError("occurrence_id must not be empty")

    return TransitionDecision(
        event_type="problem_regressed",
        problem_id=problem.problem_id,
        expected_version=expected_problem_version,
        payload={
            "next_status": "detected",
            "occurrence_id": occurrence_id,
            "change_id": change_id,
        },
    )
