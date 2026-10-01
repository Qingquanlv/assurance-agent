"""Issue analysis: turn the authenticated observation bundle into candidates."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, OutputError, Prepare

from assurance_quality.contracts.agent import (
    FinalizedIssueAnalysisV1,
    IssueAnalysisResultV1,
    QualitySkillInputV1,
)
from assurance_quality.ops import router
from assurance_quality.ops.issue_analysis import hooks

op = router.agent(
    "issue-analysis",
    input=QualitySkillInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-reporter",
        skill="aa-issue-analyzer",
        result=IssueAnalysisResultV1,
        writes=("qa/results/inspect/issue-analysis.json",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=FinalizedIssueAnalysisV1,
)

__all__ = ["op"]
