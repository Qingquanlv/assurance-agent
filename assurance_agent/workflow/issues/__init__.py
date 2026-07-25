"""Deterministic issue identity, lifecycle transitions, and read-only history."""

from assurance_agent.workflow.issues.history import (
    InMemoryIssueHistoryReader,
    IssueHistoryConflict,
    IssueHistoryIntegrityError,
    IssueHistoryReader,
    LedgerIssueHistoryReader,
)
from assurance_agent.workflow.issues.history_models import (
    IssueEvidenceSlice,
    IssueHistoryIntegrity,
    IssueSourceDescriptor,
    IssueTypedEvents,
    IssueWindowSelection,
)
from assurance_agent.workflow.issues.identity import (
    DIGEST_PREFIX_LENGTH,
    ObservationIdentityInput,
    occurrence_id,
    observation_id,
    problem_fingerprint,
    problem_id,
    reconciliation_idempotency_key,
)
from assurance_agent.workflow.issues.transitions import (
    HUMAN_TRANSITIONS,
    InvalidTransitionError,
    ResolutionContext,
    StaleVersionError,
    TransitionDecision,
    plan_regression,
    plan_resolution,
    validate_human_transition,
)

__all__ = [
    "DIGEST_PREFIX_LENGTH",
    "HUMAN_TRANSITIONS",
    "InMemoryIssueHistoryReader",
    "InvalidTransitionError",
    "IssueEvidenceSlice",
    "IssueHistoryConflict",
    "IssueHistoryIntegrity",
    "IssueHistoryIntegrityError",
    "IssueHistoryReader",
    "IssueSourceDescriptor",
    "IssueTypedEvents",
    "IssueWindowSelection",
    "LedgerIssueHistoryReader",
    "ObservationIdentityInput",
    "ResolutionContext",
    "StaleVersionError",
    "TransitionDecision",
    "occurrence_id",
    "observation_id",
    "plan_regression",
    "plan_resolution",
    "problem_fingerprint",
    "problem_id",
    "reconciliation_idempotency_key",
    "validate_human_transition",
]
