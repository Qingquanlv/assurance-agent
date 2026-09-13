"""Closed contracts at the deterministic Inspect boundary."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic.types import AwareDatetime
from agent_runtime_contracts import AgentRunResult

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
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


class FactBaselineSkillInputV1(FrozenModel):
    """The authenticated projection shown to the fact-baseline agent after case-review."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    reviewed_case: ReviewedCaseV1

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


class AssessmentSkillInputV1(FrozenModel):
    """The authenticated projection shown to Inspect agents."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    assessment: AssessmentInputsV1
    reviewed_case: ReviewedCaseV1
    mapping_ref: EvidenceArtifactRefV1
    fact_baseline_ref: EvidenceArtifactRefV1 | None = None

    @model_validator(mode="after")
    def _identity_is_closed(self) -> Self:
        if self.assessment.change_id != self.change_id:
            raise ValueError("assessment change_id must match the skill input")
        if self.assessment.coverage_epoch != self.coverage_epoch:
            raise ValueError("assessment epoch must match the skill input")
        if self.assessment.batch_id != self.batch_id:
            raise ValueError("assessment batch_id must match the skill input")
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
        if self.reviewed_case.change_id != self.change_id:
            raise ValueError("inspection Reviewed Case change_id must match")
        if self.reviewed_case.coverage_epoch != self.coverage_epoch:
            raise ValueError("inspection Reviewed Case epoch must match")
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case.plan_digest,
            self.reviewed_case.plan_ref,
        )
        if self.disposition == "satisfied" and self.coverage_state != "satisfied":
            raise ValueError("satisfied inspection requires satisfied coverage")
        if self.disposition == "coverage_insufficient" and self.coverage_state not in {
            "repair_required",
            "exhausted",
        }:
            raise ValueError("coverage insufficiency requires an insufficient coverage state")
        if (
            self.disposition in {"repairable_execution_failure", "analysis_required", "needs_human"}
            and self.coverage_state is not None
        ):
            raise ValueError("execution failure disposition cannot also publish a coverage state")
        ordered_refs = tuple(sorted(self.assessment_refs, key=lambda item: (item.path, item.digest)))
        if self.assessment_refs != ordered_refs or len(set(self.assessment_refs)) != len(
            self.assessment_refs
        ):
            raise ValueError("inspection assessment_refs must be sorted and unique")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("inspection reason_codes must be sorted and unique")
        return self


def _canonical_report_refs(
    values: tuple[EvidenceArtifactRefV1, ...],
) -> tuple[EvidenceArtifactRefV1, ...]:
    if not values:
        raise ValueError("report refs must not be empty")
    ordered = tuple(sorted(values, key=lambda item: (item.path, item.digest)))
    if values != ordered or len({item.path for item in values}) != len(values):
        raise ValueError("report refs must be sorted with one digest per path")
    return values


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


class ReportOutcomeV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    inspection_receipt: ReceiptRef
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    report_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    report_receipt: ReceiptRef

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


__all__ = [
    "AssessmentInputsV1",
    "AssessmentSkillInputV1",
    "FailureClassificationFactsV1",
    "FinalizedFactBaselineV1",
    "FinalizedInspectionV1",
    "FinalizedReportV1",
    "InspectionDisposition",
    "InspectionOutcomeV1",
    "MaterializeAssessmentInputV1",
    "ReportOutcomeV1",
    "ReportPurpose",
    "ReportSkillInputV1",
]
