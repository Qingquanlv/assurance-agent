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
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr
from assurance_agent.artifacts.models.improvements import (
    ImprovementCandidate,
    ImprovementSourceRefs,
)
from assurance_agent.artifacts.models.retro_batch import RetroBatchScope

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
    domain: Literal["issue", "workflow", "eval"]
    reason_code: Literal[
        "workspace_missing",
        "non_terminal",
        "ledger_missing",
        "ledger_corrupt",
        "digest_drift",
        "projection_missing",
        "projection_corrupt",
    ]


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


class _SliceBase(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["3"] = "3"
    retro_id: NonEmptyStr
    window: RetroWindow
    sources: tuple[RetroSourceDescriptor, ...] = ()
    integrity: RetroIntegrity = Field(default_factory=lambda: RetroIntegrity(status="complete"))
    deterministic_signals: tuple[BatchMemberEvidenceGapSignal | RetroPipelineFailureSignal, ...] = ()

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


class TaskFailureSignal(_SignalBase):
    signal_type: Literal["task_failure"] = "task_failure"
    node_id: NonEmptyStr
    error_kind: NonEmptyStr
    message_fingerprint: NonEmptyStr


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
    domain: Literal["issue", "workflow", "eval"]
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
    issue_sources: tuple[RetroSourceDescriptor, ...] = ()
    workflow_sources: tuple[RetroSourceDescriptor, ...] = ()
    eval_sources: tuple[RetroSourceDescriptor, ...] = ()

    def resolvable_ids(self) -> frozenset[str]:
        sources = (*self.issue_sources, *self.workflow_sources, *self.eval_sources)
        return frozenset(chain.from_iterable(source.evidence_ids for source in sources))


class DomainAnalysisStatus(BaseModel):
    model_config = _FROZEN

    status: Literal["ok", "failed"]
    failure_reason: NonEmptyStr | None = None


class DomainStatuses(BaseModel):
    model_config = _FROZEN

    issue: DomainAnalysisStatus
    workflow: DomainAnalysisStatus
    eval: DomainAnalysisStatus


class ContextSignalSet(BaseModel):
    model_config = _FROZEN

    issue: tuple[Signal, ...] = ()
    workflow: tuple[Signal, ...] = ()
    eval: tuple[Signal, ...] = ()


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

    model_config = _FROZEN

    schema_version: Literal["3"] = "3"
    retro_id: NonEmptyStr
    candidates: tuple[ImprovementCandidateV3, ...] = ()


class ImprovementCandidateDocumentV3(ImprovementCandidateDocumentDraftV3):
    """canonical candidates 文档 = draft + runtime 回填的 ``context_sha256``。"""

    context_sha256: NonEmptyStr
