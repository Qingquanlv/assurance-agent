"""Immutable query and evidence-slice models for the IssueHistoryReader seam."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.models.common import NonEmptyStr
from assurance_agent.artifacts.models.issues import IssueOccurrence, Observation, Problem
from assurance_agent.workflow.issues.events import ChangeIssueEvent, ProblemEvent

_FROZEN = ConfigDict(frozen=True, extra="forbid")

IssueSourceKind = Literal["change_issue_ledger", "project_problem_ledger"]
IssueIntegrityStatus = Literal["complete", "incomplete"]
IssueIntegrityReason = Literal["analysis_failed", "project_sync_pending"]


@dataclass(frozen=True)
class IssueWindowSelection:
    """Frozen query for a Retro Issue evidence window."""

    change_ids: tuple[str, ...]
    project_event_through: str | None = None
    event_since: str | None = None
    event_until: str | None = None
    include_late_review_closure: bool = False


@dataclass(frozen=True)
class IssueTypedEvents:
    """In-memory ledger bundle shared by adapter contract tests."""

    change_events: Mapping[str, Sequence[ChangeIssueEvent]]
    problem_events: Sequence[ProblemEvent]
    change_ledger_bytes: Mapping[str, bytes]
    problem_ledger_bytes: bytes


class IssueSourceDescriptor(BaseModel):
    model_config = _FROZEN

    kind: IssueSourceKind
    change_id: str | None = None
    head_event_id: str | None = None
    sha256: NonEmptyStr


class IssueHistoryIntegrity(BaseModel):
    model_config = _FROZEN

    status: IssueIntegrityStatus
    reasons: tuple[IssueIntegrityReason, ...] = ()


class IssueEvidenceSlice(BaseModel):
    """Digest-pinned, read-only Issue evidence for one Retro window."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    selection: IssueWindowSelection
    sources: tuple[IssueSourceDescriptor, ...]
    integrity: IssueHistoryIntegrity
    observations: tuple[Observation, ...] = ()
    occurrences: tuple[IssueOccurrence, ...] = ()
    problem_snapshots: tuple[Problem, ...] = ()
    problem_events: tuple[ProblemEvent, ...] = Field(default_factory=tuple)

    def resolvable_ids(self) -> frozenset[str]:
        ids: set[str] = set()
        for observation in self.observations:
            ids.add(observation.observation_id)
        for occurrence in self.occurrences:
            ids.add(occurrence.occurrence_id)
            ids.update(occurrence.observation_ids)
            ids.add(occurrence.problem_id)
        for problem in self.problem_snapshots:
            ids.add(problem.problem_id)
            ids.update(problem.occurrences)
        for event in self.problem_events:
            ids.add(event.event_id)
            ids.add(event.problem_id)
        return frozenset(ids)

    def canonical_bytes(self) -> bytes:
        data = self.model_dump(mode="json")
        return (
            json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()
