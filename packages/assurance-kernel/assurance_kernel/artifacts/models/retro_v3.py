"""Retro v3 artifact 模型：typed evidence slices、signal 判别联合、draft/canonical 双文档。

分层说明：共享的 window/source 模型（RetroWindow/RetroSelectionSnapshot/
RetroSourceDescriptor/RetroIntegrity）的唯一定义迁移到本模块（artifacts 层），
``retro/types.py`` 仅以 re-export 保持既有 import 路径（retro → artifacts 是
既有依赖方向，反向会破坏分层并成环）。

draft/canonical 双模型（spec 0.1 #3）：skill 产出 ``SignalDraftDocument`` /
``ImprovementCandidateDocumentDraftV3``——``extra="forbid"`` 使 digest 字段出现即
invalid；runtime 校验后回填 digest 得到 canonical 文档再冻结 write-set。
"""

from __future__ import annotations

from itertools import chain
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from assurance_kernel.artifacts.models.common import NonEmptyStr
from assurance_kernel.artifacts.models.improvements import (
    ImprovementCandidate,
    ImprovementSourceRefs,
)
from assurance_kernel.artifacts.models.retro_batch import RetroBatchScope

_FROZEN = ConfigDict(frozen=True, extra="forbid")


# ---- 共享 window/source 模型（唯一定义；retro/types.py re-export）----


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
    batch_scope: RetroBatchScope | None = None


class RetroSourceDescriptor(BaseModel):
    model_config = _FROZEN

    kind: Literal[
        "change_issue_ledger",
        "project_problem_ledger",
        "workflow_ledger",
        "eval_run",
        "batch_manifest",
        "retro_pipeline_failure",
        "discovery_projection",
        "coverage_gap_projection",
    ]
    change_id: str | None = None
    head_event_id: str | None = None
    sha256: str
    evidence_ids: tuple[str, ...] = ()


class RetroIntegrity(BaseModel):
    model_config = _FROZEN

    status: Literal["complete", "incomplete"]
    reasons: tuple[str, ...] = ()


# ---- evidence slices（每域独立 typed，spec C1 review#2）----


class AffectedSurface(BaseModel):
    model_config = _FROZEN

    kind: NonEmptyStr
    value: NonEmptyStr


class IssueEvidenceEntry(BaseModel):
    model_config = _FROZEN

    occurrence_id: NonEmptyStr
    problem_id: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    surface: AffectedSurface
    symptom: NonEmptyStr
    fingerprint: NonEmptyStr
    observed_at: NonEmptyStr
    classification_hint: NonEmptyStr | None = None
    head_event_id: NonEmptyStr | None = None


class _WorkflowEvidenceBase(BaseModel):
    model_config = _FROZEN

    evidence_id: NonEmptyStr
    change_id: NonEmptyStr


class GateVerdictEvidenceEntry(_WorkflowEvidenceBase):
    entry_kind: Literal["gate_verdict"] = "gate_verdict"
    gate_id: NonEmptyStr
    verdict: NonEmptyStr
    cause: NonEmptyStr | None = None
    reason: NonEmptyStr | None = None
    ts: NonEmptyStr
    seq: int | None = Field(default=None, ge=1)


class TaskFailureEvidenceEntry(_WorkflowEvidenceBase):
    entry_kind: Literal["task_failure"] = "task_failure"
    task_id: NonEmptyStr
    attempt_id: NonEmptyStr | None = None
    node_id: NonEmptyStr
    error_kind: NonEmptyStr
    message_fingerprint: NonEmptyStr
    recovered: bool
    ts: NonEmptyStr


class HealingOutcomeEvidenceEntry(_WorkflowEvidenceBase):
    entry_kind: Literal["healing_outcome"] = "healing_outcome"
    operation: NonEmptyStr
    outcome: NonEmptyStr
    ts: NonEmptyStr | None = None
    seq: int | None = Field(default=None, ge=1)


class SkillDriftEvidenceEntry(_WorkflowEvidenceBase):
    entry_kind: Literal["skill_drift"] = "skill_drift"
    phase: NonEmptyStr
    expected_skill: NonEmptyStr
    ts: NonEmptyStr | None = None


WorkflowEvidenceEntry = Annotated[
    GateVerdictEvidenceEntry
    | TaskFailureEvidenceEntry
    | HealingOutcomeEvidenceEntry
    | SkillDriftEvidenceEntry,
    Field(discriminator="entry_kind"),
]


class _SignalBase(BaseModel):
    model_config = _FROZEN

    signal_id: NonEmptyStr
    summary: NonEmptyStr
    occurrence_count: int = Field(ge=1)
    recommended_change: NonEmptyStr
    source_refs: ImprovementSourceRefs
    confidence: Literal["high", "medium", "low"]

    @model_validator(mode="after")
    def _refs_nonempty(self) -> Self:
        if not self.source_refs.all_ids():
            raise ValueError("signal requires at least one source ref")
        return self


class BatchMemberEvidenceGapSignal(_SignalBase):
    signal_type: Literal["batch_member_evidence_gap"] = "batch_member_evidence_gap"
    change_id: NonEmptyStr
    execution_status: NonEmptyStr
    domain: Literal["issue", "workflow", "eval", "discovery", "coverage_gap"]
    reason_code: Literal[
        "workspace_missing",
        "non_terminal",
        "ledger_missing",
        "ledger_corrupt",
        "digest_drift",
        "projection_missing",
        "projection_corrupt",
    ]


class DomainEvidenceGapSignal(_SignalBase):
    """Typed gap for an optional Retro domain (discovery / coverage_gap)."""

    signal_type: Literal["domain_evidence_gap"] = "domain_evidence_gap"
    domain: Literal["discovery", "coverage_gap"]
    reason_code: Literal[
        "projection_missing",
        "projection_corrupt",
        "ledger_missing",
        "ledger_corrupt",
    ]
    change_id: NonEmptyStr | None = None


class ConfirmedEscapeSignal(_SignalBase):
    signal_type: Literal["confirmed_escape"] = "confirmed_escape"
    problem_id: NonEmptyStr
    change_id: NonEmptyStr | None = None
    missed_obligation_ids: tuple[NonEmptyStr, ...] = ()


class LowPromotionRateSignal(_SignalBase):
    signal_type: Literal["low_promotion_rate"] = "low_promotion_rate"
    rate: float
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=1)


class LowReplayStabilitySignal(_SignalBase):
    signal_type: Literal["low_replay_stability"] = "low_replay_stability"
    rate: float
    success: int = Field(ge=0)
    attempts: int = Field(ge=1)


class ReopenedCoverageGapSignal(_SignalBase):
    signal_type: Literal["reopened_coverage_gap"] = "reopened_coverage_gap"
    gap_kind: NonEmptyStr
    locator_fingerprint: NonEmptyStr
    change_id: NonEmptyStr
    case_id: NonEmptyStr | None = None
    constraint_key: NonEmptyStr | None = None
    cell: NonEmptyStr | None = None
    cluster_key: NonEmptyStr | None = None


class RetroPipelineFailureSignal(_SignalBase):
    signal_type: Literal["retro_pipeline_failure"] = "retro_pipeline_failure"
    failure_id: NonEmptyStr
    stage: NonEmptyStr
    error_kind: NonEmptyStr


class EvalEvidenceEntry(BaseModel):
    model_config = _FROZEN

    run_id: NonEmptyStr
    suite: NonEmptyStr
    verdict: NonEmptyStr
    failure_signature: NonEmptyStr | None = None
    started_at: NonEmptyStr
    # legacy 缺失（None）= 无关联信息；空 tuple = 显式无关联。
    source_change_ids: tuple[str, ...] | None = None
    sample_ids: tuple[str, ...] = ()


class TaskFailureSignal(_SignalBase):
    signal_type: Literal["task_failure"] = "task_failure"
    node_id: NonEmptyStr
    error_kind: NonEmptyStr
    message_fingerprint: NonEmptyStr


DeterministicSliceSignal = (
    BatchMemberEvidenceGapSignal
    | RetroPipelineFailureSignal
    | TaskFailureSignal
    | DomainEvidenceGapSignal
    | ConfirmedEscapeSignal
    | LowPromotionRateSignal
    | LowReplayStabilitySignal
    | ReopenedCoverageGapSignal
)


class _SliceBase(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["3"] = "3"
    retro_id: NonEmptyStr
    window: RetroWindow
    sources: tuple[RetroSourceDescriptor, ...] = ()
    integrity: RetroIntegrity = Field(default_factory=lambda: RetroIntegrity(status="complete"))
    deterministic_signals: tuple[DeterministicSliceSignal, ...] = ()

    def resolvable_ids(self) -> frozenset[str]:
        """signal source_refs 的可解析命名空间（来自 slice manifest）。"""
        return frozenset(chain.from_iterable(source.evidence_ids for source in self.sources))


class IssueEvidenceSlice(_SliceBase):
    domain: Literal["issue"] = "issue"
    entries: tuple[IssueEvidenceEntry, ...] = ()


class WorkflowEvidenceSlice(_SliceBase):
    domain: Literal["workflow"] = "workflow"
    entries: tuple[WorkflowEvidenceEntry, ...] = ()


class EvalEvidenceSlice(_SliceBase):
    domain: Literal["eval"] = "eval"
    entries: tuple[EvalEvidenceEntry, ...] = ()


class DiscoveryEvidenceEntry(BaseModel):
    """Compact discovery projection row — IDs/digests/rates only (no CE bodies)."""

    model_config = _FROZEN

    evidence_id: NonEmptyStr
    change_id: NonEmptyStr
    campaign_id: NonEmptyStr
    counterexample_ids: tuple[NonEmptyStr, ...] = ()
    promotion_receipt_digests: tuple[NonEmptyStr, ...] = ()
    replay_success: int | None = Field(default=None, ge=0)
    replay_attempts: int | None = Field(default=None, ge=0)
    replay_rate: float | None = None
    problem_escape_refs: tuple[NonEmptyStr, ...] = ()
    promoted_count: int | None = Field(default=None, ge=0)
    total_counterexamples: int | None = Field(default=None, ge=0)


class DiscoveryEvidenceSlice(_SliceBase):
    domain: Literal["discovery"] = "discovery"
    entries: tuple[DiscoveryEvidenceEntry, ...] = ()


class CoverageGapEvidenceEntry(BaseModel):
    """Compact coverage-gap projection row or closed/opened/reopened event."""

    model_config = _FROZEN

    evidence_id: NonEmptyStr
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    projection_digest: NonEmptyStr
    document_digest: NonEmptyStr
    event_kind: Literal["current", "closed", "opened", "reopened"] = "current"
    gap_kind: NonEmptyStr | None = None
    locator_fingerprint: NonEmptyStr | None = None
    case_id: NonEmptyStr | None = None
    constraint_key: NonEmptyStr | None = None
    cell: NonEmptyStr | None = None
    cluster_key: NonEmptyStr | None = None


class CoverageGapEvidenceSlice(_SliceBase):
    domain: Literal["coverage_gap"] = "coverage_gap"
    entries: tuple[CoverageGapEvidenceEntry, ...] = ()


# ---- Signal：signal_type 判别联合（spec C1 review#1）----


class IssuePatternSignal(_SignalBase):
    signal_type: Literal["issue_pattern"] = "issue_pattern"
    pattern_kind: Literal["workflow_gap", "prompt_gap", "fixture_gap", "test_gap", "knowledge_gap"]
    affected_surface: AffectedSurface
    symptom: NonEmptyStr


class GatePushbackSignal(_SignalBase):
    signal_type: Literal["gate_pushback"] = "gate_pushback"
    gate_id: NonEmptyStr
    cause: NonEmptyStr


class HealingSignal(_SignalBase):
    signal_type: Literal["healing"] = "healing"
    operation: NonEmptyStr
    outcome: NonEmptyStr


class SkillDriftSignal(_SignalBase):
    signal_type: Literal["skill_drift"] = "skill_drift"
    phase: NonEmptyStr
    expected_skill: NonEmptyStr


class EvalTrendSignal(_SignalBase):
    signal_type: Literal["eval_trend"] = "eval_trend"
    suite: NonEmptyStr
    verdict: NonEmptyStr
    failure_signature: NonEmptyStr
    consecutive_count: int = Field(ge=0)
    sample_run_ids: tuple[str, ...] = ()
    source_change_ids: tuple[str, ...] = ()


Signal = Annotated[
    BatchMemberEvidenceGapSignal
    | RetroPipelineFailureSignal
    | DomainEvidenceGapSignal
    | ConfirmedEscapeSignal
    | LowPromotionRateSignal
    | LowReplayStabilitySignal
    | ReopenedCoverageGapSignal
    | IssuePatternSignal
    | GatePushbackSignal
    | TaskFailureSignal
    | HealingSignal
    | SkillDriftSignal
    | EvalTrendSignal,
    Field(discriminator="signal_type"),
]


# ---- 信号文档双模型（spec C1 review#3）----


class SignalDraftDocument(BaseModel):
    """skill 产出；``extra="forbid"`` 使 ``slice_sha256`` 出现即 invalid（digest 由 runtime 回填）。"""

    model_config = _FROZEN

    schema_version: Literal["3"] = "3"
    retro_id: NonEmptyStr
    domain: Literal["issue", "workflow", "eval", "discovery", "coverage_gap"]
    analysis_status: Literal["ok", "failed"]
    failure_reason: NonEmptyStr | None = None
    analyzer: NonEmptyStr
    signals: tuple[Signal, ...] = ()

    @model_validator(mode="after")
    def _status_reason_consistency(self) -> Self:
        if self.analysis_status == "failed":
            if self.failure_reason is None:
                raise ValueError("failure_reason is required when analysis_status=failed")
            if self.signals:
                raise ValueError("failed analysis must not carry signals")
        elif self.failure_reason is not None:
            raise ValueError("failure_reason must be null when analysis_status=ok")
        return self


class SignalDocumentV3(SignalDraftDocument):
    """canonical 信号文档 = draft + runtime 回填的 ``slice_sha256``。"""

    slice_sha256: NonEmptyStr


# ---- Context v3 ----


class RetroSourceManifestV3(BaseModel):
    model_config = _FROZEN

    issue_slice_sha256: NonEmptyStr
    workflow_slice_sha256: NonEmptyStr
    eval_slice_sha256: NonEmptyStr
    # Optional for compatibility with historical Retro artifacts. New runs pin
    # discovery/coverage_gap when their production readers materialize them.
    discovery_slice_sha256: NonEmptyStr | None = None
    coverage_gap_slice_sha256: NonEmptyStr | None = None
    issue_sources: tuple[RetroSourceDescriptor, ...] = ()
    workflow_sources: tuple[RetroSourceDescriptor, ...] = ()
    eval_sources: tuple[RetroSourceDescriptor, ...] = ()
    discovery_sources: tuple[RetroSourceDescriptor, ...] = ()
    coverage_gap_sources: tuple[RetroSourceDescriptor, ...] = ()

    def resolvable_ids(self) -> frozenset[str]:
        sources = (
            *self.issue_sources,
            *self.workflow_sources,
            *self.eval_sources,
            *self.discovery_sources,
            *self.coverage_gap_sources,
        )
        return frozenset(chain.from_iterable(source.evidence_ids for source in sources))


class DomainAnalysisStatus(BaseModel):
    model_config = _FROZEN

    status: Literal["ok", "failed", "skipped"]
    failure_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _status_reason_consistency(self) -> Self:
        if self.status == "failed":
            if self.failure_reason is None:
                raise ValueError("failure_reason is required when status=failed")
        elif self.failure_reason is not None and self.status != "skipped":
            raise ValueError("failure_reason must be null when status=ok")
        return self


class DomainStatuses(BaseModel):
    """Core three domains required; discovery/coverage_gap optional for old ledgers.

    Missing optional domains default to ``None`` (absent) so historical Retro
    fixtures continue to validate without inventing skipped statuses.
    """

    model_config = _FROZEN

    issue: DomainAnalysisStatus
    workflow: DomainAnalysisStatus
    eval: DomainAnalysisStatus
    discovery: DomainAnalysisStatus | None = None
    coverage_gap: DomainAnalysisStatus | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_domains(self, handler: Any) -> dict[str, Any]:
        """An absent domain is absent, not a null status.

        Consumers read this mapping's values as statuses, so emitting ``null``
        for a domain the run never had would make them read a missing key as a
        malformed status.
        """
        return {key: value for key, value in handler(self).items() if value is not None}


class ContextSignalSet(BaseModel):
    model_config = _FROZEN

    issue: tuple[Signal, ...] = ()
    workflow: tuple[Signal, ...] = ()
    eval: tuple[Signal, ...] = ()
    discovery: tuple[Signal, ...] = ()
    coverage_gap: tuple[Signal, ...] = ()


class RetroContextV3(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["3"] = "3"
    retro_id: NonEmptyStr
    generated_at: NonEmptyStr
    dry_run: bool = False
    window: RetroWindow
    source_manifest: RetroSourceManifestV3
    integrity: RetroIntegrity
    domain_status: DomainStatuses
    signals: ContextSignalSet
    signal_count: int = Field(ge=0)

    @property
    def allows_domain_knowledge(self) -> bool:
        """v3 domain knowledge requires all three evidence domains to be complete."""
        return self.integrity.status == "complete"


# ---- Candidates v3 ----


class ImprovementCandidateV3(ImprovementCandidate):
    """v3 Candidate = v2 字段 + 强制 signal_ids（提案 ← 分析结论的溯源链）。"""

    signal_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)


class ImprovementCandidateDocumentDraftV3(BaseModel):
    """proposer 产出；``extra="forbid"`` 使 ``context_sha256`` 出现即 invalid（runtime 回填）。"""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={
            "prompt_notes": [
                "omit context_sha256 because the runtime inserts it",
                "every candidate must include signal_ids",
                "never emit legacy intent_key",
            ]
        },
    )

    schema_version: Literal["3"] = "3"
    retro_id: NonEmptyStr
    candidates: tuple[ImprovementCandidateV3, ...] = ()


class ImprovementCandidateDocumentV3(ImprovementCandidateDocumentDraftV3):
    """canonical candidates 文档 = draft + runtime 回填的 ``context_sha256``。"""

    context_sha256: NonEmptyStr
