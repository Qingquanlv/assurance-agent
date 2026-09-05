"""Retro window, evidence slices, signals, context, and candidate documents."""

from __future__ import annotations

from datetime import datetime
from itertools import chain
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_serializer, model_validator

from assurance_improvement.contracts.improvements import ImprovementCandidateV3, ImprovementSourceRefs
from assurance_intake.contracts import EvidenceArtifactRefV1, NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

RetroExecutionStatus = Literal[
    "completed",
    "failed",
    "stopped",
    "hard_timeout",
    "cancelled",
    "running",
    "not_started",
]
EvidenceAvailability = Literal["complete", "partial", "absent"]
PersistedRetroResult = Literal["completed", "completed_with_gaps", "pending_reconcile"]
RetroInvocationResultValue = Literal[
    "completed", "completed_with_gaps", "pending_reconcile", "technical_failure"
]


class RetroBatchMember(BaseModel):
    model_config = _FROZEN

    change_id: str = Field(min_length=1)
    execution_status: RetroExecutionStatus
    evidence_availability: EvidenceAvailability


class RetroBatchScope(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    batch_id: str = Field(min_length=1)
    status: Literal["complete", "incomplete"]
    members: tuple[RetroBatchMember, ...] = ()

    @model_validator(mode="after")
    def validate_members(self) -> Self:
        change_ids = tuple(member.change_id for member in self.members)
        if change_ids != tuple(sorted(change_ids)):
            raise ValueError("batch members must use canonical change_id ordering")
        if len(set(change_ids)) != len(change_ids):
            raise ValueError("batch members must be unique by change_id")
        if self.status == "complete" and any(
            member.evidence_availability != "complete" for member in self.members
        ):
            raise ValueError("complete batch requires complete evidence for every member")
        return self


class RetroPipelineFailure(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    failure_id: str = Field(min_length=1)
    retro_id: str = Field(min_length=1)
    batch_id: str | None = None
    stage: str = Field(min_length=1)
    node_id: str | None = None
    error_kind: str = Field(min_length=1)
    message_fingerprint: str = Field(min_length=1)
    runtime_event_id: str | None = None
    occurred_at: datetime


class RetroPipelineFailureDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    retro_id: str = Field(min_length=1)
    failures: tuple[RetroPipelineFailure, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_failures(self) -> Self:
        failure_ids = tuple(failure.failure_id for failure in self.failures)
        if failure_ids != tuple(sorted(failure_ids)) or len(set(failure_ids)) != len(failure_ids):
            raise ValueError("pipeline failures must be sorted and unique by failure_id")
        if any(failure.retro_id != self.retro_id for failure in self.failures):
            raise ValueError("pipeline failure retro_id must match its document")
        return self


class RetroRunStatus(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    retro_id: str = Field(min_length=1)
    batch_id: str | None = None
    result: PersistedRetroResult
    improvement_ids: tuple[str, ...] = ()
    outbox_id: str | None = None
    failure_ids: tuple[str, ...] = ()


class RetroInvocationResult(BaseModel):
    model_config = _FROZEN

    status: RetroRunStatus | None
    result: RetroInvocationResultValue

    @model_validator(mode="after")
    def validate_status_binding(self) -> Self:
        if self.result == "technical_failure":
            if self.status is not None:
                raise ValueError("technical_failure cannot have a persisted Retro status")
            return self
        if self.status is None or self.status.result != self.result:
            raise ValueError("successful invocation result must match persisted Retro status")
        return self


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


class LoopRoundEvidenceEntry(_WorkflowEvidenceBase):
    entry_kind: Literal["loop_round"] = "loop_round"
    coverage_epoch: int = Field(ge=0)
    loop_kind: Literal[
        "coverage",
        "case_review",
        "plan_review",
        "codegen_fix",
        "implementation_repair",
    ]
    family: Literal["api", "e2e", "fuzz", "performance"] | None = None
    round_index: int = Field(ge=0)
    outcome: NonEmptyStr
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_family_and_sources(self) -> Self:
        family_loop = self.loop_kind in {"plan_review", "codegen_fix"}
        if family_loop != (self.family is not None):
            raise ValueError("family is required only for family-specific generation loops")
        ordered = tuple(sorted(self.source_refs, key=lambda item: (item.path, item.digest)))
        if self.source_refs != ordered or len({item.path for item in self.source_refs}) != len(
            self.source_refs
        ):
            raise ValueError("source_refs must be sorted and unique by path")
        return self


WorkflowEvidenceEntry = Annotated[
    GateVerdictEvidenceEntry
    | TaskFailureEvidenceEntry
    | HealingOutcomeEvidenceEntry
    | SkillDriftEvidenceEntry
    | LoopRoundEvidenceEntry,
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


class SignalDraftDocument(BaseModel):
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
    slice_sha256: NonEmptyStr


class RetroSourceManifestV3(BaseModel):
    model_config = _FROZEN

    issue_slice_sha256: NonEmptyStr
    workflow_slice_sha256: NonEmptyStr
    eval_slice_sha256: NonEmptyStr
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
    model_config = _FROZEN

    issue: DomainAnalysisStatus
    workflow: DomainAnalysisStatus
    eval: DomainAnalysisStatus
    discovery: DomainAnalysisStatus | None = None
    coverage_gap: DomainAnalysisStatus | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_domains(self, handler: Any) -> dict[str, Any]:
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
        return self.integrity.status == "complete"


class ImprovementCandidateDocumentDraftV3(BaseModel):
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
    context_sha256: NonEmptyStr

    @model_validator(mode="after")
    def _authenticated_source_membership(self, info: ValidationInfo) -> Self:
        context = info.context or {}
        manifest = context.get("retro_manifest")
        if not isinstance(manifest, RetroSourceManifestV3):
            raise ValueError("candidate source is outside the retro manifest")
        allowed = manifest.resolvable_ids()
        for candidate in self.candidates:
            if any(item not in allowed for item in candidate.source_refs.all_ids()):
                raise ValueError("candidate source is outside the retro manifest")
        return self
