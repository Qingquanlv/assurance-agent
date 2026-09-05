from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState

from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    InspectionOutcomeV1,
    ReportOutcomeV1,
    ReportPurpose,
)
from assurance_quality.contracts.decisions import CoverageState, FailureClassification


class QualityAssessPublicV1(FrozenModel):
    change_id: str
    coverage_state: CoverageState | None
    inspection_outcome: InspectionOutcomeV1
    evidence_refs: list[dict[str, str]]
    rounds_budget: int
    rounds_used: int


class QualityIssuePublicV1(FrozenModel):
    change_id: str
    classification: FailureClassification
    evidence_refs: list[dict[str, str]]
    fix_eligible: bool
    rounds_budget: int
    rounds_used: int


class QualityReportPublicV1(FrozenModel):
    change_id: str
    coverage_state: CoverageState | None
    purpose: ReportPurpose
    status: Literal["reported", "diagnostic"]
    report_outcome: ReportOutcomeV1 | None = None
    report_refs: tuple[EvidenceArtifactRefV1, ...]
    report_receipt: ReceiptRef

    @model_validator(mode="after")
    def _normal_success_requires_a_closed_outcome(self) -> Self:
        if self.purpose == "normal":
            if self.status != "reported" or self.coverage_state != "satisfied":
                raise ValueError("normal report requires a satisfied reported result")
            if self.report_outcome is None:
                raise ValueError("normal report requires a committed report outcome")
            if (
                self.report_outcome.change_id != self.change_id
                or self.report_outcome.report_refs != self.report_refs
                or self.report_outcome.report_receipt != self.report_receipt
            ):
                raise ValueError("normal report projection does not match its outcome")
        elif self.status != "diagnostic" or self.report_outcome is not None:
            raise ValueError("diagnostic report cannot publish a normal success outcome")
        return self


class QualityState(CheckpointBridgeState, total=False):
    change_id: str
    batch_id: str
    coverage_epoch: int
    reviewed_case: ReviewedCaseV1
    generation_result: GenerationCycleResultV1
    execution_result: ExecutionCycleResultV1
    policy_resource_id: str
    policy_sha256: str
    execution_at: str
    healing_ref: EvidenceArtifactRefV1 | None
    issue_ref: EvidenceArtifactRefV1 | None
    assessment_inputs: AssessmentInputsV1
    fact_baseline_ref: EvidenceArtifactRefV1
    inspection_outcome: InspectionOutcomeV1
    capability_leafs: list[str]
    allowed_artifact_paths: list[str]
    evidence_refs: list[dict[str, str]]
    execution_status: Literal["failed", "passed"]
    budgets: dict[str, int]
    rounds_budget: int
    rounds_used: int
    activation: dict[str, str]
    coverage_state: CoverageState
    classification: FailureClassification
    fix_eligible: bool
    report_refs: list[dict[str, str]]
    report_receipt: ReceiptRef | None
    report_outcome: ReportOutcomeV1
    report_purpose: ReportPurpose
    execution_digest: str
    healing_digest: str | None
    trace_digest: str
    coverage_digest: str
    metrics_digest: str
    case_digest: str
    plan_digest: str
    mapping_digest: str
    issue_digest: str | None
    status: str
    attempt_failure: dict[str, object]


__all__ = [
    "QualityAssessPublicV1",
    "QualityIssuePublicV1",
    "QualityReportPublicV1",
    "QualityState",
]
