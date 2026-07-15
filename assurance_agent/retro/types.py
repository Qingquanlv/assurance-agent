from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from assurance_agent.artifacts.models import ApplySummary, FailureAnalysis, Review, WorkflowState

EvidenceSource = Literal["archive", "unarchived"]


class ArchivedChange(BaseModel):
    change_id: str
    evidence_source: EvidenceSource
    path: str
    events: list[dict] = Field(default_factory=list)
    failure_analysis: FailureAnalysis | None = None
    reviews: dict[str, Review] = Field(default_factory=dict)
    apply_summaries: list[ApplySummary] = Field(default_factory=list)
    workflow_state: WorkflowState | None = None


class ChangeSource(BaseModel):
    change_id: str
    evidence_source: EvidenceSource
    path: str = ""


class FailureDistributionSignal(BaseModel):
    category: str
    count: int


class GatePushbackSignal(BaseModel):
    gate: str
    count: int


class HealingEfficiencySignal(BaseModel):
    attempts: int = 0
    applied: int = 0
    success_rate: float = 0.0


class ReclassificationSignal(BaseModel):
    change_id: str
    from_category: str
    to_category: str


class HumanDecisionSignal(BaseModel):
    change_id: str
    decision: str


class SkillExecutionSignal(BaseModel):
    skill: str
    count: int


class EvalTrendSignal(BaseModel):
    suite: str
    run_id: str
    verdict: str
    started_at: str


class RetroSignalSet(BaseModel):
    failure_distribution: list[FailureDistributionSignal] = Field(default_factory=list)
    gate_pushback: list[GatePushbackSignal] = Field(default_factory=list)
    healing_efficiency: HealingEfficiencySignal = Field(default_factory=HealingEfficiencySignal)
    human_decisions: list[HumanDecisionSignal] = Field(default_factory=list)
    reclassifications: list[ReclassificationSignal] = Field(default_factory=list)
    skill_execution: list[SkillExecutionSignal] = Field(default_factory=list)
    eval_trend: list[EvalTrendSignal] = Field(default_factory=list)


class RetroWindow(BaseModel):
    since: str | None = None
    change_count: int = 0
    change_ids: list[str] = Field(default_factory=list)
    change_sources: list[ChangeSource] = Field(default_factory=list)


class RetroContext(BaseModel):
    retro_id: str
    generated_at: str
    window: RetroWindow
    signals: RetroSignalSet
    signal_count: int = 0


class RetroProposal(BaseModel):
    id: str
    apply_kind: Literal["memory_append", "skill_edit", "other"] = "memory_append"
    eval_suite: str | None = None
    summary: str = ""
    body: str = ""


class RetroPromoteRecord(BaseModel):
    proposal_id: str
    decision: Literal["promoted", "rejected", "needs_rework"]
    decided_by: str
    decided_at: str
    rework_note: str | None = None
    eval_run_id: str | None = None
