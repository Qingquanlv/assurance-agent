from __future__ import annotations

from typing import Literal, Self

from agent_runtime_contracts import AgentExecutionContract
from pydantic import BaseModel, ConfigDict, model_validator

from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

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
CoverageState = Literal["exhausted", "inconclusive", "needs_human", "repair_required", "satisfied"]
COVERAGE_STATES: tuple[CoverageState, ...] = (
    "exhausted",
    "inconclusive",
    "needs_human",
    "repair_required",
    "satisfied",
)


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


WORKFLOW_MODULE_ID = "assurance.quality.workflow"
WORKFLOW_RESOURCE_ID = "assurance.quality.workflow.module.v1"
WORKFLOW_EXPORTS: tuple[str, ...] = (
    "assess",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "report",
)
AGENT_SLOT_PHASES: tuple[str, ...] = ("prepare", "execute", "finalize")

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
    "AgentExecutionContract",
    "COVERAGE_STATES",
    "CoverageAssessmentPublicV1",
    "CoverageState",
    "FIX_ELIGIBLE_CLASSIFICATIONS",
    "FailureClassification",
    "IssueAnalysisPublicV1",
    "OUTPUT_ROUTE_TEMPLATES",
    "WORKFLOW_EXPORTS",
    "WORKFLOW_MODULE_ID",
    "WORKFLOW_RESOURCE_ID",
]
