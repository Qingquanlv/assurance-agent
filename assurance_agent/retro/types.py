"""Schema-v2 Retro evidence types (current-run only; no legacy proposal tracks)."""

from __future__ import annotations

from itertools import chain
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.models.improvements import ImprovementSourceRefs

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class RetroSelectionSnapshot(BaseModel):
    model_config = _FROZEN

    mode: Literal["change_ids", "time_range", "last"]
    requested_change_ids: tuple[str, ...] = ()
    requested_since: str | None = None
    requested_until: str | None = None
    requested_last: int | None = None


class RetroWindow(BaseModel):
    model_config = _FROZEN

    selection: RetroSelectionSnapshot
    change_ids: tuple[str, ...]
    since: str | None = None
    until: str | None = None
    project_event_through: str | None = None


class RetroSourceDescriptor(BaseModel):
    model_config = _FROZEN

    kind: Literal["change_issue_ledger", "project_problem_ledger", "workflow_ledger", "eval_run"]
    change_id: str | None = None
    head_event_id: str | None = None
    sha256: str
    evidence_ids: tuple[str, ...] = ()


class RetroSourceManifest(BaseModel):
    model_config = _FROZEN

    issue_slice_sha256: str
    issue_sources: tuple[RetroSourceDescriptor, ...]
    workflow_sources: tuple[RetroSourceDescriptor, ...]
    eval_sources: tuple[RetroSourceDescriptor, ...]

    def resolvable_ids(self) -> frozenset[str]:
        sources = (*self.issue_sources, *self.workflow_sources, *self.eval_sources)
        return frozenset(chain.from_iterable(source.evidence_ids for source in sources))


class RetroIntegrity(BaseModel):
    model_config = _FROZEN

    status: Literal["complete", "incomplete"]
    reasons: tuple[str, ...] = ()


class RetroSignal(BaseModel):
    model_config = _FROZEN

    signal_id: str
    source_refs: ImprovementSourceRefs
    metrics: dict[str, int | float | str]


class IssueRetroSignals(BaseModel):
    model_config = _FROZEN

    observation_distribution: tuple[RetroSignal, ...] = ()
    occurrence_trends: tuple[RetroSignal, ...] = ()
    assessment_corrections: tuple[RetroSignal, ...] = ()
    regressions: tuple[RetroSignal, ...] = ()
    review_decision_patterns: tuple[RetroSignal, ...] = ()
    resolution_outcomes: tuple[RetroSignal, ...] = ()
    repeated_not_an_issue: tuple[RetroSignal, ...] = ()


class WorkflowRetroSignals(BaseModel):
    model_config = _FROZEN

    gate_pushback: tuple[RetroSignal, ...] = ()
    healing_efficiency: tuple[RetroSignal, ...] = ()
    skill_execution_drift: tuple[RetroSignal, ...] = ()
    task_failures: tuple[RetroSignal, ...] = ()


class EvalRetroSignals(BaseModel):
    model_config = _FROZEN

    trends: tuple[RetroSignal, ...] = ()


class RetroSignalSet(BaseModel):
    model_config = _FROZEN

    issue: IssueRetroSignals
    workflow: WorkflowRetroSignals
    eval: EvalRetroSignals


class RetroContext(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["2"] = "2"
    retro_id: str
    generated_at: str
    window: RetroWindow
    source_manifest: RetroSourceManifest
    integrity: RetroIntegrity
    signals: RetroSignalSet
    signal_count: int = Field(ge=0)

    @property
    def allows_domain_knowledge(self) -> bool:
        """True only when Issue source integrity is complete.

        Incomplete Workflow/Eval sources do not forbid domain_knowledge; only
        Issue ``analysis_failed`` / ``project_sync_pending`` reasons do.
        Other ImprovementKinds remain eligible when this is false.
        """
        issue_blockers = frozenset({"analysis_failed", "project_sync_pending"})
        return not any(
            reason in issue_blockers or reason.startswith("issue_pipeline_failed:")
            for reason in self.integrity.reasons
        )
