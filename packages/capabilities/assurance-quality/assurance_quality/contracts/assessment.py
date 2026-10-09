"""Closed contracts at the deterministic Inspect boundary."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic.types import AwareDatetime
from agent_runtime_contracts import AgentRunResult

from graph_engine.attempts.models.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts import PolicyResourceV1
from assurance_intake.contracts.workflow import (
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
    require_same_plan,
)
from assurance_quality.contracts.agent import (
    FactBaselineResultV1,
    InspectionResultV1,
    QualitySkillInputV1,
)
from assurance_quality.contracts.coverage import CoverageState
from assurance_quality.contracts.obligations import ObligationGateFactsV1
from assurance_quality.contracts.goal_policy import ActiveCoverageScopeV1, CoverageGoalPolicyV1
from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts

InspectionDisposition = Literal[
    "satisfied",
    "coverage_insufficient",
    "repairable_execution_failure",
    "analysis_required",
    "needs_human",
    "blocked",
]
ReportPurpose = Literal["normal", "diagnostic"]


class MaterializeAssessmentInputV1(FrozenModel):
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    reviewed_case: ReviewedCaseV1
    generation: GenerationCycleResultV1
    execution: ExecutionCycleResultV1
    policy_resource_id: str = Field(min_length=1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_at: AwareDatetime
    healing_ref: EvidenceArtifactRefV1 | None = None
    issue_ref: EvidenceArtifactRefV1 | None = None
    repair_round: int = Field(default=0, ge=0)

    @computed_field
    @property
    def coverage_epoch_token(self) -> str:
        return str(self.reviewed_case.coverage_epoch)

    @model_validator(mode="after")
    def _cycle_identity_is_closed(self) -> Self:
        if self.generation.reviewed_case != self.reviewed_case:
            raise ValueError("generation must bind the current Reviewed Case")
        if self.execution.change_id != self.reviewed_case.change_id:
            raise ValueError("execution change_id must match the current Reviewed Case")
        if self.execution.coverage_epoch != self.reviewed_case.coverage_epoch:
            raise ValueError("execution epoch must match the current Reviewed Case")
        if self.execution.mapping_ref != self.generation.mapping_ref:
            raise ValueError("execution mapping must match the generation cycle")
        if self.execution_at != self.execution.executed_at:
            raise ValueError("assessment time must match the committed execution time")
        for digest, ref in (
            (self.reviewed_case.plan_digest, self.reviewed_case.plan_ref),
            (self.generation.plan_digest, self.generation.plan_ref),
            (self.execution.plan_digest, self.execution.plan_ref),
        ):
            require_same_plan(self.plan_digest, self.plan_ref, digest, ref)
        return self


class MaterializeAssessmentBoundV1(FrozenModel):
    """Refs the materialize task opens. The loaded documents stay inside the handler."""

    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    reviewed_case_ref: EvidenceArtifactRefV1
    generation_ref: EvidenceArtifactRefV1
    execution_ref: EvidenceArtifactRefV1
    execution_receipt: ReceiptRef
    product_policy: PolicyResourceV1
    repair_round: int = Field(ge=0)
    coverage_epoch: int = Field(ge=0)
    healing_ref: EvidenceArtifactRefV1 | None = None
    issue_ref: EvidenceArtifactRefV1 | None = None

    @computed_field
    @property
    def coverage_epoch_token(self) -> str:
        return str(self.coverage_epoch)

    @computed_field
    @property
    def repair_round_token(self) -> str:
        return str(self.repair_round)


class AssessmentInputsV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    scope: ActiveCoverageScopeV1
    policy: CoverageGoalPolicyV1
    trace_ref: EvidenceArtifactRefV1
    gaps_ref: EvidenceArtifactRefV1
    metrics_ref: EvidenceArtifactRefV1
    sufficiency_ref: EvidenceArtifactRefV1
    execution_ref: EvidenceArtifactRefV1
    observations_ref: EvidenceArtifactRefV1
    obligation_assessment_ref: EvidenceArtifactRefV1
    obligation_gate_facts: ObligationGateFactsV1
    issue_evidence_manifest_ref: EvidenceArtifactRefV1
    owned_evidence_ids: tuple[str, ...]
    evidence_bundle_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    healing_ref: EvidenceArtifactRefV1 | None = None
    issue_ref: EvidenceArtifactRefV1 | None = None

    @model_validator(mode="after")
    def _scope_identity_matches(self) -> Self:
        if self.scope.change_id != self.change_id:
            raise ValueError("assessment scope change_id must match")
        if self.scope.coverage_epoch != self.coverage_epoch:
            raise ValueError("assessment scope epoch must match")
        return self


class FactBaselineBoundInputV1(FrozenModel):
    """What the fact-baseline flow can project before prepare opens the reviewed case."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    reviewed_case_ref: EvidenceArtifactRefV1
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)


class FactBaselineSkillInputV1(FrozenModel):
    """The authenticated projection shown to the fact-baseline agent after case-review."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    reviewed_case: ReviewedCaseV1
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)

    @model_validator(mode="after")
    def _identity_is_closed(self) -> Self:
        if self.reviewed_case.change_id != self.change_id:
            raise ValueError("Reviewed Case change_id must match the skill input")
        if self.reviewed_case.coverage_epoch != self.coverage_epoch:
            raise ValueError("Reviewed Case epoch must match the skill input")
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case.plan_digest,
            self.reviewed_case.plan_ref,
        )
        return self


class FactBaselineFinalizeInputV1(FactBaselineSkillInputV1):
    agent_result: AgentRunResult


ASSESSMENT_INPUTS_PATH = "qa/results/inspect/assessment-inputs.json"


class InspectBoundInputV1(FrozenModel):
    """What the assess flow can project before Inspect opens the producer files."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()
    product_policy: PolicyResourceV1
    assessment_ref: EvidenceArtifactRefV1
    reviewed_case_ref: EvidenceArtifactRefV1
    generation_ref: EvidenceArtifactRefV1
    execution_ref: EvidenceArtifactRefV1
    execution_receipt: ReceiptRef
    fact_baseline_ref: EvidenceArtifactRefV1 | None = None
    repair_round: int = Field(default=0, ge=0)
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)


class AssessmentSkillInputV1(FrozenModel):
    """The authenticated projection shown to Inspect agents."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assessment_ref: EvidenceArtifactRefV1 | None = None
    assessment: AssessmentInputsV1 | None = None
    reviewed_case: ReviewedCaseV1
    mapping_ref: EvidenceArtifactRefV1
    fact_baseline_ref: EvidenceArtifactRefV1 | None = None
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)

    @model_validator(mode="after")
    def _identity_is_closed(self) -> Self:
        if self.assessment is None:
            if self.assessment_ref is None:
                raise ValueError("assessment is missing")
            return self
        if self.assessment.change_id != self.change_id:
            raise ValueError("assessment change_id must match the skill input")
        if self.assessment.coverage_epoch != self.coverage_epoch:
            raise ValueError("assessment epoch must match the skill input")
        if self.assessment.batch_id != self.batch_id:
            raise ValueError("assessment batch_id must match the skill input")
        if self.policy_sha256 != self.assessment.scope.policy_digest:
            raise ValueError("inspection identity no longer matches the active assessment cycle")
        if self.reviewed_case.change_id != self.change_id:
            raise ValueError("Reviewed Case change_id must match the skill input")
        if self.reviewed_case.coverage_epoch != self.coverage_epoch:
            raise ValueError("Reviewed Case epoch must match the skill input")
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case.plan_digest,
            self.reviewed_case.plan_ref,
        )
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.assessment.plan_digest,
            self.assessment.plan_ref,
        )
        return self


class AssessmentFinalizeInputV1(AssessmentSkillInputV1):
    agent_result: AgentRunResult


class FinalizedFactBaselineV1(FrozenModel):
    agent_result: FactBaselineResultV1
    reviewed_case: ReviewedCaseV1
    fact_baseline_ref: EvidenceArtifactRefV1

    @model_validator(mode="after")
    def _baseline_matches_reviewed_case(self) -> Self:
        if self.agent_result.change_id != self.reviewed_case.change_id:
            raise ValueError("fact baseline change_id must match the Reviewed Case")
        expected = "qa/results/facts/fact-baseline.json"
        if self.fact_baseline_ref.path != expected:
            raise ValueError("fact baseline ref must use the current change path")
        return self


class FailureClassificationFactsV1(FrozenModel):
    identity_valid: bool
    blocking_failure: bool
    analysis_required: bool
    needs_human: bool
    repairable_failure: bool


class FinalizedInspectionV1(FrozenModel):
    agent_result: InspectionResultV1
    assessment: AssessmentInputsV1
    reviewed_case: ReviewedCaseV1
    mapping_ref: EvidenceArtifactRefV1
    metrics: MetricsDocument
    sufficiency: TraceSufficiencyFacts
    failure_facts: FailureClassificationFactsV1
    fact_baseline_ref: EvidenceArtifactRefV1
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def _closed_identity_and_reasons(self) -> Self:
        if self.agent_result.change_id != self.assessment.change_id:
            raise ValueError("inspection result change_id must match assessment")
        if self.agent_result.batch_id != self.assessment.batch_id:
            raise ValueError("inspection result batch_id must match assessment")
        if self.reviewed_case.change_id != self.assessment.change_id:
            raise ValueError("inspection Reviewed Case change_id must match assessment")
        if self.reviewed_case.coverage_epoch != self.assessment.coverage_epoch:
            raise ValueError("inspection Reviewed Case epoch must match assessment")
        if self.metrics.change_id != self.assessment.change_id:
            raise ValueError("inspection metrics change_id must match assessment")
        if self.sufficiency.change_id != self.assessment.change_id:
            raise ValueError("inspection sufficiency change_id must match assessment")
        if self.metrics.policy_digest != self.assessment.scope.policy_digest:
            raise ValueError("inspection metrics policy must match assessment")
        if self.sufficiency.policy_digest != self.assessment.scope.policy_digest:
            raise ValueError("inspection sufficiency policy must match assessment")
        if self.sufficiency.authoritative_batch_id != self.assessment.batch_id:
            raise ValueError("inspection sufficiency batch must match assessment")
        digest_pairs = {
            "execution": (self.agent_result.execution_digest, self.assessment.execution_ref.digest),
            "healing": (
                self.agent_result.healing_digest,
                self.assessment.healing_ref.digest if self.assessment.healing_ref is not None else None,
            ),
            "trace": (self.agent_result.trace_digest, self.assessment.trace_ref.digest),
            "coverage": (self.agent_result.coverage_digest, self.assessment.gaps_ref.digest),
            "metrics": (self.agent_result.metrics_digest, self.assessment.metrics_ref.digest),
        }
        for name, (actual, expected) in digest_pairs.items():
            if actual != expected:
                raise ValueError(f"inspection {name} digest must match assessment")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("inspection reason_codes must be sorted and unique")
        return self


def _close_inspection(document: InspectionDocumentV1 | InspectionOutcomeV1) -> None:
    if document.reviewed_case.change_id != document.change_id:
        raise ValueError("inspection Reviewed Case change_id must match")
    if document.reviewed_case.coverage_epoch != document.coverage_epoch:
        raise ValueError("inspection Reviewed Case epoch must match")
    require_same_plan(
        document.plan_digest,
        document.plan_ref,
        document.reviewed_case.plan_digest,
        document.reviewed_case.plan_ref,
    )
    if document.disposition == "satisfied" and document.coverage_state != "satisfied":
        raise ValueError("satisfied inspection requires satisfied coverage")
    if document.disposition == "coverage_insufficient" and document.coverage_state not in {
        "repair_required",
        "exhausted",
    }:
        raise ValueError("coverage insufficiency requires an insufficient coverage state")
    if (
        document.disposition in {"repairable_execution_failure", "analysis_required", "needs_human"}
        and document.coverage_state is not None
    ):
        raise ValueError("execution failure disposition cannot also publish a coverage state")
    ordered_refs = tuple(sorted(document.assessment_refs, key=lambda item: (item.path, item.digest)))
    if document.assessment_refs != ordered_refs or len(set(document.assessment_refs)) != len(
        document.assessment_refs
    ):
        raise ValueError("inspection assessment_refs must be sorted and unique")
    if tuple(sorted(set(document.reason_codes))) != document.reason_codes:
        raise ValueError("inspection reason_codes must be sorted and unique")


INSPECTION_OUTCOME_PATH = "qa/results/inspect/inspection-outcome.json"


class InspectionDocumentV1(FrozenModel):
    """Sealed inspection facts. The commit receipt is attached by the parent."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    disposition: InspectionDisposition
    reviewed_case: ReviewedCaseV1
    mapping_ref: EvidenceArtifactRefV1
    assessment_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...]
    coverage_state: CoverageState | None = None

    @model_validator(mode="after")
    def _identity_and_disposition_are_closed(self) -> Self:
        _close_inspection(self)
        return self


class InspectionOutcomeV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    disposition: InspectionDisposition
    inspection_receipt: ReceiptRef
    reviewed_case: ReviewedCaseV1
    mapping_ref: EvidenceArtifactRefV1
    assessment_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...]
    coverage_state: CoverageState | None = None

    @model_validator(mode="after")
    def _identity_and_disposition_are_closed(self) -> Self:
        _close_inspection(self)
        return self


class InspectPublishedV1(FrozenModel):
    """Inspect's routed result. The commit receipt is exported beside this document."""

    disposition: InspectionDisposition
    coverage_state: CoverageState | None = None
    inspection_outcome: InspectionDocumentV1
    evidence_refs: tuple[EvidenceArtifactRefV1, ...]
    observations_ref: EvidenceArtifactRefV1
    issue_evidence_manifest_ref: EvidenceArtifactRefV1
    owned_evidence_ids: tuple[str, ...]
    evidence_bundle_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    assessment: AssessmentInputsV1
    finalized: FinalizedInspectionV1


def _canonical_report_refs(
    values: tuple[EvidenceArtifactRefV1, ...],
) -> tuple[EvidenceArtifactRefV1, ...]:
    if not values:
        raise ValueError("report refs must not be empty")
    ordered = tuple(sorted(values, key=lambda item: (item.path, item.digest)))
    if values != ordered or len({item.path for item in values}) != len(values):
        raise ValueError("report refs must be sorted with one digest per path")
    return values


class ReportBoundInputV1(FrozenModel):
    """Refs the report op opens. The skill documents are assembled in prepare."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    purpose: ReportPurpose
    capability_leafs: tuple[str, ...]
    fact_baseline_ref: EvidenceArtifactRefV1
    issue_analysis_ref: EvidenceArtifactRefV1 | None = None
    artifact_paths: tuple[str, ...] = ()
    inspection_ref: EvidenceArtifactRefV1
    assessment_ref: EvidenceArtifactRefV1
    generation_ref: EvidenceArtifactRefV1
    execution_ref: EvidenceArtifactRefV1
    execution_receipt: ReceiptRef
    inspection_receipt: ReceiptRef


class ReportSkillInputV1(QualitySkillInputV1):
    coverage_epoch: int = Field(ge=0)
    purpose: ReportPurpose
    inspection: InspectionOutcomeV1
    assessment: AssessmentInputsV1
    generation: GenerationCycleResultV1
    fact_baseline_ref: EvidenceArtifactRefV1
    issue_analysis_ref: EvidenceArtifactRefV1 | None = None

    @model_validator(mode="after")
    def _bind_current_inspection_chain(self) -> Self:
        identity = (self.change_id, self.coverage_epoch, self.batch_id)
        if identity != (
            self.inspection.change_id,
            self.inspection.coverage_epoch,
            self.inspection.batch_id,
        ):
            raise ValueError("report input does not describe the current inspection")
        if identity != (
            self.assessment.change_id,
            self.assessment.coverage_epoch,
            self.assessment.batch_id,
        ):
            raise ValueError("report assessment does not describe the current inspection")
        if self.generation.reviewed_case != self.inspection.reviewed_case:
            raise ValueError("report generation does not use the inspected Reviewed Case")
        if self.generation.mapping_ref != self.inspection.mapping_ref:
            raise ValueError("report generation does not use the inspected mapping")
        expected_refs = tuple(
            sorted(
                (
                    self.assessment.trace_ref,
                    self.assessment.gaps_ref,
                    self.assessment.metrics_ref,
                    self.assessment.sufficiency_ref,
                    self.assessment.execution_ref,
                    self.assessment.observations_ref,
                    self.assessment.obligation_assessment_ref,
                    self.assessment.issue_evidence_manifest_ref,
                    *(() if self.assessment.healing_ref is None else (self.assessment.healing_ref,)),
                    *(() if self.assessment.issue_ref is None else (self.assessment.issue_ref,)),
                    self.fact_baseline_ref,
                ),
                key=lambda item: (item.path, item.digest),
            )
        )
        if self.inspection.assessment_refs != expected_refs:
            raise ValueError("report input does not carry the inspected assessment evidence chain")
        if self.execution_digest != self.assessment.execution_ref.digest:
            raise ValueError("report execution digest does not match the current assessment")
        if self.trace_digest != self.assessment.trace_ref.digest:
            raise ValueError("report trace digest does not match the current assessment")
        if self.coverage_digest != self.assessment.gaps_ref.digest:
            raise ValueError("report coverage digest does not match the current assessment")
        if self.metrics_digest != self.assessment.metrics_ref.digest:
            raise ValueError("report metrics digest does not match the current assessment")
        if self.mapping_digest != self.inspection.mapping_ref.digest:
            raise ValueError("report mapping digest does not match the current inspection")
        if self.case_digest != self.inspection.reviewed_case.review_ref.digest:
            raise ValueError("report case digest does not match the current Reviewed Case")
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.generation.plan_digest,
            self.generation.plan_ref,
        )
        healing_digest = (
            self.assessment.healing_ref.digest if self.assessment.healing_ref is not None else None
        )
        issue_digest = (
            self.issue_analysis_ref.digest
            if self.issue_analysis_ref is not None
            else self.assessment.issue_ref.digest
            if self.assessment.issue_ref is not None
            else None
        )
        if self.healing_digest != healing_digest or self.issue_digest != issue_digest:
            raise ValueError("report optional evidence digests do not match the current assessment")
        if self.purpose == "normal" and self.inspection.disposition != "satisfied":
            raise ValueError("normal report requires a satisfied inspection")
        if self.purpose == "normal" and self.issue_analysis_ref is not None:
            raise ValueError("normal report cannot bind diagnostic issue analysis")
        if self.purpose == "diagnostic" and self.inspection.disposition == "satisfied":
            raise ValueError("diagnostic report requires a non-success inspection")
        if self.purpose == "diagnostic" and self.issue_analysis_ref is None:
            raise ValueError("diagnostic report requires current issue analysis")
        return self


class ReportFinalizeInputV1(ReportSkillInputV1):
    agent_result: AgentRunResult


class FinalizedReportV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    purpose: ReportPurpose
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    inspection_receipt: ReceiptRef
    report_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)

    @field_validator("report_refs")
    @classmethod
    def _report_refs(cls, value: tuple[EvidenceArtifactRefV1, ...]) -> tuple[EvidenceArtifactRefV1, ...]:
        return _canonical_report_refs(value)

    @model_validator(mode="after")
    def _refs_belong_to_report(self) -> Self:
        prefix = "qa/results/report/"
        if any(not ref.path.startswith(prefix) for ref in self.report_refs):
            raise ValueError("report refs must belong to the current change report directory")
        return self


class ReportPublishedV1(FrozenModel):
    """Report flow output. The commit receipt is exported beside this document."""

    publication: Literal["reported", "diagnostic", "failed"]
    report_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    coverage_state: CoverageState | None = None
    report_outcome: dict[str, object] | None = None
    finalized: FinalizedReportV1 | None = None


REPORT_OUTCOME_PATH = "qa/results/report/report-outcome.json"


class ReportOutcomeDocumentV1(FrozenModel):
    """Published report outcome. This attempt's commit receipt stays on the ledger."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    inspection_receipt: ReceiptRef
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    report_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)

    @field_validator("report_refs")
    @classmethod
    def _report_refs(cls, value: tuple[EvidenceArtifactRefV1, ...]) -> tuple[EvidenceArtifactRefV1, ...]:
        return _canonical_report_refs(value)

    @model_validator(mode="after")
    def _refs_belong_to_report(self) -> Self:
        prefix = "qa/results/report/"
        if any(not ref.path.startswith(prefix) for ref in self.report_refs):
            raise ValueError("report refs must belong to the current change report directory")
        return self


class ReportOutcomeV1(ReportOutcomeDocumentV1):
    report_receipt: ReceiptRef


__all__ = [
    "AssessmentInputsV1",
    "AssessmentSkillInputV1",
    "FailureClassificationFactsV1",
    "FinalizedFactBaselineV1",
    "FinalizedInspectionV1",
    "FinalizedReportV1",
    "ReportPublishedV1",
    "ASSESSMENT_INPUTS_PATH",
    "InspectionDisposition",
    "INSPECTION_OUTCOME_PATH",
    "InspectionDocumentV1",
    "InspectionOutcomeV1",
    "InspectPublishedV1",
    "MaterializeAssessmentBoundV1",
    "MaterializeAssessmentInputV1",
    "REPORT_OUTCOME_PATH",
    "ReportOutcomeDocumentV1",
    "ReportOutcomeV1",
    "ReportPurpose",
    "ReportSkillInputV1",
]
