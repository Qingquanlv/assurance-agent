"""Compatibility facade over issue event schemas and authority replay readers.

Mutation callers continue to use ``read_*`` which return an empty list when the
ledger path is missing. Authority loaders in ``evidence.issue_replay`` raise
``IssueLedgerMissingError`` instead.
"""

from __future__ import annotations

from pathlib import Path

from assurance_agent.artifacts.models.issue_events import (
    CHANGE_ISSUE_EVENT_ADAPTER,
    PROBLEM_EVENT_ADAPTER,
    ChangeIssueEvent,
    IssueAnalysisCompletedEvent,
    IssueAnalysisFailedEvent,
    ObservationRecordedEvent,
    OccurrenceDetectedEvent,
    OccurrenceLinkedEvent,
    ProblemAssessmentConfirmedEvent,
    ProblemDetectedEvent,
    ProblemEvent,
    ProblemMarkedNotAnIssueEvent,
    ProblemMergedEvent,
    ProblemMergeSuggestedEvent,
    ProblemOccurrenceLinkedEvent,
    ProblemRegressedEvent,
    ProblemReopenedEvent,
    ProblemResolvedEvent,
    ProblemRiskAcceptedEvent,
    ProblemVerificationRequestedEvent,
    ProblemWorkStartedEvent,
    ProjectSyncPendingEvent,
)
from assurance_agent.evidence.issue_replay import (
    IssueLedgerIntegrityError,
    IssueLedgerMissingError,
    load_change_issue_ledger,
    load_problem_ledger,
)

# Historical name retained for mutation-store callers and existing tests.
LedgerIntegrityError = IssueLedgerIntegrityError

__all__ = [
    "CHANGE_ISSUE_EVENT_ADAPTER",
    "PROBLEM_EVENT_ADAPTER",
    "ChangeIssueEvent",
    "IssueAnalysisCompletedEvent",
    "IssueAnalysisFailedEvent",
    "IssueLedgerIntegrityError",
    "IssueLedgerMissingError",
    "LedgerIntegrityError",
    "ObservationRecordedEvent",
    "OccurrenceDetectedEvent",
    "OccurrenceLinkedEvent",
    "ProblemAssessmentConfirmedEvent",
    "ProblemDetectedEvent",
    "ProblemEvent",
    "ProblemMarkedNotAnIssueEvent",
    "ProblemMergedEvent",
    "ProblemMergeSuggestedEvent",
    "ProblemOccurrenceLinkedEvent",
    "ProblemRegressedEvent",
    "ProblemReopenedEvent",
    "ProblemResolvedEvent",
    "ProblemRiskAcceptedEvent",
    "ProblemVerificationRequestedEvent",
    "ProblemWorkStartedEvent",
    "ProjectSyncPendingEvent",
    "read_change_issue_events",
    "read_problem_events",
]


def read_change_issue_events(path: Path) -> list[ChangeIssueEvent]:
    """Read a Change Issue ledger; missing path is an empty ledger for writers."""
    if not path.is_file():
        return []
    return list(load_change_issue_ledger(path))


def read_problem_events(path: Path) -> list[ProblemEvent]:
    """Read a Project Problem ledger; missing path is an empty ledger for writers."""
    if not path.is_file():
        return []
    return list(load_problem_ledger(path))
