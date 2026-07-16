from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    changes: list[str] = Field(default_factory=list)
    top_modules: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class GatePushbackSignal(BaseModel):
    gate: str
    count: int
    evidence_ids: list[str] = Field(default_factory=list)


class HealingEfficiencySignal(BaseModel):
    attempts: int = 0
    applied: int = 0
    success_rate: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)


class ReclassificationSignal(BaseModel):
    change_id: str
    from_category: str
    to_category: str
    evidence_ids: list[str] = Field(default_factory=list)


class HumanDecisionSignal(BaseModel):
    change_id: str
    decision: str
    evidence_id: str | None = None


class SkillExecutionSignal(BaseModel):
    skill: str
    count: int
    evidence_ids: list[str] = Field(default_factory=list)


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
    """A retro improvement proposal.

    Mirrors the `aa-retro` skill's proposals.json schema (id / layer / target /
    problem / proposed_change / evidence_ids / apply_kind / eval_suite / risk /
    confidence / status). `summary`/`body` are retained for backward
    compatibility and auto-backfilled from `problem`/`proposed_change` so
    downstream consumers (`phase_d` review-queue rendering reads `summary`;
    `validate_retro_proposals` reads `body`) keep working with skill-authored
    proposals that only populate the rich fields.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    apply_kind: Literal["memory_append", "skill_edit", "other"] = "memory_append"
    eval_suite: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    layer: str | None = None
    target: str | None = None
    problem: str = ""
    proposed_change: str = ""
    risk: str | None = None
    confidence: str | None = None
    status: str = "proposed"
    summary: str = ""
    body: str = ""

    @model_validator(mode="after")
    def _backfill_summary_body(self) -> RetroProposal:
        if not self.summary.strip() and self.problem.strip():
            self.summary = self.problem
        if not self.body.strip() and self.proposed_change.strip():
            self.body = self.proposed_change
        return self


class RetroPromoteRecord(BaseModel):
    proposal_id: str
    decision: Literal["promoted", "rejected", "needs_rework"]
    decided_by: str
    decided_at: str
    rework_note: str | None = None
    eval_run_id: str | None = None
