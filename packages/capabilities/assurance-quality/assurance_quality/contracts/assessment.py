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
from assurance_quality.contracts.coverage import CoverageState
from assurance_quality.contracts.goal_policy import ActiveCoverageScopeV1, CoverageGoalPolicyV1

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
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("inspection reason_codes must be sorted and unique")
        return self


__all__ = [
    "AssessmentInputsV1",
    "InspectionDisposition",
    "InspectionOutcomeV1",
    "MaterializeAssessmentInputV1",
]
