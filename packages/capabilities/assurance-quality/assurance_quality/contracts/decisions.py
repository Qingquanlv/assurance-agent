from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from assurance_quality.contracts.coverage import COVERAGE_STATES, CoverageState

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


__all__ = [
    "COVERAGE_STATES",
    "CoverageAssessmentPublicV1",
    "CoverageState",
    "FIX_ELIGIBLE_CLASSIFICATIONS",
    "FailureClassification",
    "IssueAnalysisPublicV1",
]
