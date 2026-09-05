from __future__ import annotations

from typing import Literal

from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState

from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_quality.contracts.assessment import AssessmentInputsV1, InspectionOutcomeV1
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
    coverage_state: CoverageState
    report_refs: list[dict[str, str]]


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
