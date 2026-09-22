from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from assurance_quality.contracts.assessment import (
    FailureClassificationFactsV1,
    InspectionDisposition,
)
from assurance_quality.contracts.coverage import COVERAGE_STATES, CoverageState
from assurance_quality.contracts.agent import IssueAnalysisResultV1
from assurance_quality.contracts.obligations import ObligationGateDecision

FailureClassification = Literal[
    "environment_failure",
    "failed",
    "infrastructure_failure",
    "pending",
    "product_bug",
    "test",
    "test-data",
    "unknown",
]
FIX_ELIGIBLE_CLASSIFICATIONS: frozenset[str] = frozenset({"test", "test-data"})


class IssueAnalysisPublicV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: FailureClassification
    fix_eligible: bool

    @model_validator(mode="after")
    def _fix_eligible_only_for_test_kinds(self) -> Self:
        if self.fix_eligible and self.classification not in FIX_ELIGIBLE_CLASSIFICATIONS:
            raise ValueError("fix_eligible is only valid for test or test-data classification")
        return self


class CoverageAssessmentPublicV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coverage_state: CoverageState
    rounds_budget: int
    rounds_used: int


def classify_issue_candidates(result: IssueAnalysisResultV1) -> IssueAnalysisPublicV1:
    """Only an entirely test-owned batch can authorize a test repair."""
    kinds = {candidate.proposed.classification for candidate in result.candidates}
    classification: FailureClassification = "unknown"
    if result.status == "completed" and kinds and "unknown" not in kinds:
        if kinds <= {"test_bug", "test_data_issue"}:
            classification = "test" if "test_bug" in kinds else "test-data"
        else:
            for kind, target in (
                ("product_bug", "product_bug"),
                ("workflow_issue", "infrastructure_failure"),
                ("environment_issue", "environment_failure"),
                ("performance_issue", "failed"),
                ("coverage_gap", "failed"),
            ):
                if kind in kinds:
                    classification = target  # type: ignore[assignment]
                    break
    return IssueAnalysisPublicV1(
        classification=classification,
        fix_eligible=classification in FIX_ELIGIBLE_CLASSIFICATIONS,
    )


def classify_inspection_disposition(
    *,
    facts: FailureClassificationFactsV1,
    coverage_state: CoverageState | None,
) -> InspectionDisposition:
    if not facts.identity_valid:
        return "blocked"
    if facts.analysis_required:
        return "analysis_required"
    if facts.blocking_failure:
        return "blocked"
    if facts.needs_human:
        return "needs_human"
    if facts.repairable_failure:
        return "repairable_execution_failure"
    if coverage_state == "repair_required":
        return "coverage_insufficient"
    if coverage_state == "satisfied":
        return "satisfied"
    return "blocked"


def merge_obligation_disposition(
    disposition: InspectionDisposition,
    obligation_decision: ObligationGateDecision,
) -> InspectionDisposition:
    """A coverage/test repair cannot override an obligation stop condition."""
    if disposition in {"blocked", "analysis_required"}:
        return disposition
    if obligation_decision == "blocked":
        return "blocked"
    if disposition == "needs_human" or obligation_decision == "needs_human":
        return "needs_human"
    if disposition == "repairable_execution_failure":
        return disposition
    if disposition == "coverage_insufficient" or obligation_decision == "repair_required":
        return "coverage_insufficient"
    return "satisfied"


__all__ = [
    "COVERAGE_STATES",
    "CoverageAssessmentPublicV1",
    "CoverageState",
    "FIX_ELIGIBLE_CLASSIFICATIONS",
    "FailureClassification",
    "IssueAnalysisPublicV1",
    "classify_inspection_disposition",
    "merge_obligation_disposition",
]
