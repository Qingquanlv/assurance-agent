from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_agent.artifacts.models import ApplySummary, FailureAnalysis, Review, WorkflowState
from assurance_agent.artifacts.models.data_knowledge import DataKnowledgeProposal
from assurance_agent.identifiers import UnsafeIdentifierError, assert_path_segment_safe

EvidenceSource = Literal["archive", "unarchived"]
FindingKind = Literal["prompt_rule", "workflow_bug", "domain_knowledge"]
ApplyKind = Literal["memory_append", "issue_export", "knowledge_delta"]

FINDING_TO_APPLY: dict[FindingKind, ApplyKind] = {
    "prompt_rule": "memory_append",
    "workflow_bug": "issue_export",
    "domain_knowledge": "knowledge_delta",
}

APPLY_TO_FINDING: dict[ApplyKind, FindingKind] = {v: k for k, v in FINDING_TO_APPLY.items()}


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
    verdict: str
    count: int
    top_reasons: list[str] = Field(default_factory=list)
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
    phase: str
    count: int
    changes: list[str] = Field(default_factory=list)
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


class MemoryBodyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str


class IssueDraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    target: str
    severity: Literal["low", "medium", "high"]
    evidence_ids: list[str] = Field(default_factory=list)
    proposed_change: str


def _first_nonempty(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _legacy_payload(data: dict, finding_kind: str) -> dict | None:
    """Synthesize a ``payload`` for pre-three-track proposals, or None if impossible.

    Proposals written before the three-track schema carry the routing information
    as prose (``problem`` / ``proposed_change``) plus ``target``, with no
    ``payload`` object. The memory and issue tracks are fully recoverable from
    those fields; ``domain_knowledge`` is not, because L1 leaves cannot be
    reconstructed from prose — those must fail loudly rather than be guessed.
    """
    if finding_kind == "prompt_rule":
        body = _first_nonempty(data, "proposed_change", "body", "problem")
        return {"body": body} if body else None
    if finding_kind == "workflow_bug":
        title = _first_nonempty(data, "summary", "problem")
        target = _first_nonempty(data, "target")
        proposed_change = _first_nonempty(data, "proposed_change", "body")
        if not (title and target and proposed_change):
            return None
        risk = data.get("risk")
        return {
            "title": title,
            "target": target,
            "severity": risk if risk in ("low", "medium", "high") else "medium",
            "evidence_ids": data.get("evidence_ids") or [],
            "proposed_change": proposed_change,
        }
    return None


class RetroProposal(BaseModel):
    """A retro improvement proposal with discriminated structured payload (spec C6).

    ``finding_kind`` drives ``apply_kind`` and the ``payload`` schema. Natural
    language ``problem``/``proposed_change``/``body`` remain for human review
    queues; export and apply use ``payload`` fields.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    finding_kind: FindingKind
    apply_kind: ApplyKind
    payload: MemoryBodyPayload | IssueDraftPayload | DataKnowledgeProposal
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

    @model_validator(mode="before")
    @classmethod
    def _adapt_legacy_shape(cls, data: Any) -> Any:
        """Backfill the machine routing fields for pre-three-track proposals.

        ``finding_kind`` and ``payload`` became required when the three-track
        schema landed, but retro agents kept emitting the older shape
        (``apply_kind`` + ``target`` + prose). Since ``finding_kind`` and
        ``apply_kind`` are 1:1, the former is derivable; the payload is
        reconstructed from prose where that is lossless. Explicit values are
        never overwritten, so conforming proposals pass through untouched.
        """
        if not isinstance(data, dict):
            return data
        finding_kind = data.get("finding_kind")
        if not finding_kind:
            apply_kind = data.get("apply_kind")
            finding_kind = None
            if apply_kind in APPLY_TO_FINDING:
                finding_kind = APPLY_TO_FINDING[apply_kind]  # type: ignore[index]
            if finding_kind is None:
                return data
            data = {**data, "finding_kind": finding_kind}
        if data.get("payload") is None:
            payload = _legacy_payload(data, finding_kind)
            if payload is not None:
                data = {**data, "payload": payload}
        return data

    @model_validator(mode="after")
    def _validate_and_backfill(self) -> RetroProposal:
        expected_apply = FINDING_TO_APPLY[self.finding_kind]
        if self.apply_kind != expected_apply:
            raise ValueError(
                f"proposal {self.id}: finding_kind={self.finding_kind!r} requires "
                f"apply_kind={expected_apply!r}, got {self.apply_kind!r}"
            )
        try:
            assert_path_segment_safe(self.id, label="proposal id")
        except UnsafeIdentifierError as err:
            raise ValueError(str(err)) from err

        expected_payload = {
            "prompt_rule": MemoryBodyPayload,
            "workflow_bug": IssueDraftPayload,
            "domain_knowledge": DataKnowledgeProposal,
        }[self.finding_kind]
        if not isinstance(self.payload, expected_payload):
            raise ValueError(
                f"proposal {self.id}: payload must be {expected_payload.__name__} "
                f"for finding_kind={self.finding_kind!r}"
            )
        if self.finding_kind == "domain_knowledge":
            if not isinstance(self.payload, DataKnowledgeProposal) or self.payload.mode != "delta":
                raise ValueError(f"proposal {self.id}: knowledge_delta payload must have mode: delta")

        if not self.summary.strip() and self.problem.strip():
            self.summary = self.problem
        if isinstance(self.payload, MemoryBodyPayload):
            if not self.body.strip():
                self.body = self.payload.body
            if not self.proposed_change.strip():
                self.proposed_change = self.payload.body
        elif isinstance(self.payload, IssueDraftPayload):
            if not self.proposed_change.strip():
                self.proposed_change = self.payload.proposed_change
        return self


def memory_body_text(proposal: RetroProposal) -> str:
    """Deterministic memory block body for ``memory_append`` proposals."""
    if isinstance(proposal.payload, MemoryBodyPayload):
        return proposal.payload.body.strip()
    return (proposal.proposed_change or proposal.body or proposal.problem).strip()


class RetroPromoteRecord(BaseModel):
    proposal_id: str
    decision: Literal["promoted", "rejected", "needs_rework"]
    decided_by: str
    decided_at: str
    rework_note: str | None = None
    eval_run_id: str | None = None
