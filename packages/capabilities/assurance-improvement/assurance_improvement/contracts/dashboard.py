"""Read-only view model for the retro dashboard exposed by `aa retro show`.

This module owns display projection only. It has no bearing on retro
execution semantics — those live in `contracts/retro.py`,
`contracts/improvements.py`, and `operations/retro.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_intake.contracts import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

DashboardDomainName = Literal["issue", "workflow", "eval", "discovery", "coverage_gap"]
DashboardDomainStatus = Literal["ok", "failed", "skipped", "absent"]


class DashboardStages(BaseModel):
    model_config = _FROZEN

    analyses: bool
    synthesis: bool
    reconcile: bool


class DashboardMetric(BaseModel):
    model_config = _FROZEN

    label: NonEmptyStr
    value: str


class DashboardSourceRefs(BaseModel):
    model_config = _FROZEN

    problem_ids: tuple[str, ...] = ()
    occurrence_ids: tuple[str, ...] = ()
    issue_event_ids: tuple[str, ...] = ()
    workflow_evidence_ids: tuple[str, ...] = ()
    eval_run_ids: tuple[str, ...] = ()


class DashboardSignal(BaseModel):
    model_config = _FROZEN

    signal_id: NonEmptyStr
    signal_type: NonEmptyStr
    domain: DashboardDomainName
    summary: NonEmptyStr
    occurrence_count: int = Field(ge=1)
    confidence: Literal["high", "medium", "low"]
    recommended_change: NonEmptyStr
    metrics: tuple[DashboardMetric, ...] = ()
    source_refs: DashboardSourceRefs
    cited_by_candidate_ids: tuple[str, ...] = ()


class DashboardDomain(BaseModel):
    model_config = _FROZEN

    domain: DashboardDomainName
    status: DashboardDomainStatus
    failure_reason: str | None = None
    signal_count: int | None = None
    source_count: int = 0
    source_kinds: dict[str, int] = Field(default_factory=dict)
    slice_sha256: str | None = None


class DashboardVerification(BaseModel):
    model_config = _FROZEN

    suites: tuple[str, ...] = ()
    required_cases: tuple[str, ...] = ()
    success_criteria: NonEmptyStr


class DashboardCandidate(BaseModel):
    model_config = _FROZEN

    candidate_id: NonEmptyStr
    kind: NonEmptyStr
    delivery: NonEmptyStr
    target: NonEmptyStr
    rationale: NonEmptyStr
    proposed_change: NonEmptyStr
    risk: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    signal_ids: tuple[str, ...] = ()
    verification: DashboardVerification
    source_refs: DashboardSourceRefs
    has_knowledge_delta: bool = False
    supersedes: str | None = None
    improvement_id: str | None = None
    improvement_state: str | None = None
    improvement_version: int | None = None


class DashboardRun(BaseModel):
    model_config = _FROZEN

    result: str | None = None
    integrity_status: Literal["complete", "incomplete"] | None = None
    integrity_reasons: tuple[str, ...] = ()
    signal_count: int = 0
    candidate_count: int = 0
    window_change_ids: tuple[str, ...] = ()
    selection_mode: str | None = None
    batch_id: str | None = None
    failure_ids: tuple[str, ...] = ()
    improvement_ids: tuple[str, ...] = ()


class RetroDashboardV1(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: str | None = None
    retro_id: str | None = None
    generated_at: str | None = None
    dry_run: bool = False
    stages: DashboardStages
    run: DashboardRun
    domains: tuple[DashboardDomain, ...] = ()
    signals: tuple[DashboardSignal, ...] = ()
    candidates: tuple[DashboardCandidate, ...] = ()
