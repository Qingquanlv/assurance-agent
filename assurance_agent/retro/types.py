from __future__ import annotations

from itertools import chain
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_agent.artifacts.models import ApplySummary, FailureAnalysis, Review, WorkflowState
from assurance_agent.artifacts.models.data_knowledge import DataKnowledgeProposal
from assurance_agent.artifacts.models.improvements import ImprovementSourceRefs
from assurance_agent.artifacts.models.issues import ChangeIssueSnapshot
from assurance_agent.identifiers import UnsafeIdentifierError, assert_path_segment_safe
from assurance_agent.workflow.issues.events import ChangeIssueEvent

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
    issue_events: list[ChangeIssueEvent] = Field(default_factory=list)
    issue_snapshot: ChangeIssueSnapshot | None = None
    issue_read_error: str | None = None


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


class OccurrenceTrendSignal(BaseModel):
    classification: str
    count: int
    changes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class IssueRegressionSignal(BaseModel):
    problem_id: str
    change_id: str
    evidence_ids: list[str] = Field(default_factory=list)


class ProblemDecisionSignal(BaseModel):
    problem_id: str
    action: str
    evidence_ids: list[str] = Field(default_factory=list)


class ProblemResolutionSignal(BaseModel):
    problem_id: str
    outcome: str
    change_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class NotAnIssuePatternSignal(BaseModel):
    classification: str
    count: int
    problem_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class LegacyRetroSignalSet(BaseModel):
    failure_distribution: list[FailureDistributionSignal] = Field(default_factory=list)
    gate_pushback: list[GatePushbackSignal] = Field(default_factory=list)
    healing_efficiency: HealingEfficiencySignal = Field(default_factory=HealingEfficiencySignal)
    human_decisions: list[HumanDecisionSignal] = Field(default_factory=list)
    reclassifications: list[ReclassificationSignal] = Field(default_factory=list)
    skill_execution: list[SkillExecutionSignal] = Field(default_factory=list)
    eval_trend: list[EvalTrendSignal] = Field(default_factory=list)
    occurrence_trends: list[OccurrenceTrendSignal] = Field(default_factory=list)
    issue_regressions: list[IssueRegressionSignal] = Field(default_factory=list)
    problem_decisions: list[ProblemDecisionSignal] = Field(default_factory=list)
    problem_resolutions: list[ProblemResolutionSignal] = Field(default_factory=list)
    not_an_issue_patterns: list[NotAnIssuePatternSignal] = Field(default_factory=list)


class LegacyRetroWindow(BaseModel):
    since: str | None = None
    change_count: int = 0
    change_ids: list[str] = Field(default_factory=list)
    change_sources: list[ChangeSource] = Field(default_factory=list)
    issue_evidence_errors: dict[str, str] = Field(default_factory=dict)


class LegacyRetroContext(BaseModel):
    retro_id: str
    generated_at: str
    window: LegacyRetroWindow
    signals: LegacyRetroSignalSet
    signal_count: int = 0


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
        return not any(reason in issue_blockers for reason in self.integrity.reasons)


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
