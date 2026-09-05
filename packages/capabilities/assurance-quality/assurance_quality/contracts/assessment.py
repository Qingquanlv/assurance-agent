"""Closed contracts at the deterministic Inspect boundary."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, computed_field, model_validator
from pydantic.types import AwareDatetime

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_quality.contracts.agent import FactBaselineResultV1, InspectionResultV1
from assurance_quality.contracts.coverage import CoverageState
from assurance_quality.contracts.goal_policy import ActiveCoverageScopeV1, CoverageGoalPolicyV1
from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts

InspectionDisposition = Literal[
    "satisfied",
    "coverage_insufficient",
    "repairable_execution_failure",
    "needs_human",
    "blocked",
]


class MaterializeAssessmentInputV1(FrozenModel):
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
        return self


class AssessmentInputsV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    scope: ActiveCoverageScopeV1
    policy: CoverageGoalPolicyV1
    trace_ref: EvidenceArtifactRefV1
    gaps_ref: EvidenceArtifactRefV1
    metrics_ref: EvidenceArtifactRefV1
    sufficiency_ref: EvidenceArtifactRefV1
    execution_ref: EvidenceArtifactRefV1
    healing_ref: EvidenceArtifactRefV1 | None = None
    issue_ref: EvidenceArtifactRefV1 | None = None

    @model_validator(mode="after")
    def _scope_identity_matches(self) -> Self:
        if self.scope.change_id != self.change_id:
            raise ValueError("assessment scope change_id must match")
        if self.scope.coverage_epoch != self.coverage_epoch:
            raise ValueError("assessment scope epoch must match")
        return self


class AssessmentSkillInputV1(FrozenModel):
    """The authenticated projection shown to fact-baseline and Inspect agents."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
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
        return self


class FinalizedFactBaselineV1(FrozenModel):
    agent_result: FactBaselineResultV1
    assessment: AssessmentInputsV1
    fact_baseline_ref: EvidenceArtifactRefV1

    @model_validator(mode="after")
    def _baseline_matches_assessment(self) -> Self:
        if self.agent_result.change_id != self.assessment.change_id:
            raise ValueError("fact baseline change_id must match assessment")
        expected = f"qa/changes/{self.assessment.change_id}/facts/fact-baseline.json"
        if self.fact_baseline_ref.path != expected:
            raise ValueError("fact baseline ref must use the current change path")
        return self


class FailureClassificationFactsV1(FrozenModel):
    identity_valid: bool
    blocking_failure: bool
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
        if self.disposition == "satisfied" and self.coverage_state != "satisfied":
            raise ValueError("satisfied inspection requires satisfied coverage")
        if self.disposition == "coverage_insufficient" and self.coverage_state not in {
            "repair_required",
            "exhausted",
        }:
            raise ValueError("coverage insufficiency requires an insufficient coverage state")
        if (
            self.disposition in {"repairable_execution_failure", "needs_human"}
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


__all__ = [
    "AssessmentInputsV1",
    "AssessmentSkillInputV1",
    "FailureClassificationFactsV1",
    "FinalizedFactBaselineV1",
    "FinalizedInspectionV1",
    "InspectionDisposition",
    "InspectionOutcomeV1",
    "MaterializeAssessmentInputV1",
]
