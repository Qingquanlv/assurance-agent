from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, Self

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.plugin_api import ResourceClaimTemplate
from pydantic import BaseModel, ConfigDict, model_validator

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

_DOC_AUTHOR = "assurance-v1-doc-author"
_REPORTER = "assurance-v1-reporter"
_REVIEWER = "assurance-v1-reviewer"


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(base: str, skill_id: str, agent_profile: str, outputs: tuple[str, ...]) -> AgentExecutionContract:
    return AgentExecutionContract(
        contract_id=f"assurance.quality.agent.{base}.v1",
        skill_id=skill_id,
        agent_profile=agent_profile,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=_paths(*outputs),
        ),
    )


_JOBS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("fact-baseline", "aa-fact-baseline", _DOC_AUTHOR, ("facts/fact-baseline.json",)),
    ("inspect", "aa-inspect", _REVIEWER, ("inspect/inspection.json",)),
    ("issue-analysis", "aa-issue-analyzer", _REPORTER, ("inspect/issue-analysis.json",)),
    ("issue-triage", "aa-issue-triage-advisor", _REPORTER, ("inspect/issue-triage.json",)),
    ("report", "aa-report-generator", _REPORTER, ("report/report.md",)),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {base: _job(base, skill_id, agent_profile, outputs) for base, skill_id, agent_profile, outputs in _JOBS}
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill_id, _profile, outputs in _JOBS}
)

__all__ = [
    "AGENT_JOB_CONTRACTS",
    "AGENT_SLOT_PHASES",
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
