"""Issue analysis: turn the authenticated observation bundle into candidates."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Out, OutputError, Prepare

from assurance_healing.contracts.issue_handoff import ISSUE_ANALYSIS_HANDOFF_PATH
from assurance_quality.contracts.agent import IssueAnalysisBoundInputV1, IssueAnalysisResultV1
from assurance_quality.contracts.decisions import IssueAnalysisPublishedV1
from assurance_quality.ops import router
from assurance_quality.ops.issue_analysis import hooks

op = router.agent(
    "issue-analysis",
    input=IssueAnalysisBoundInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-reporter",
        skill="aa-issue-analyzer",
        result=IssueAnalysisResultV1,
        writes=(Out("issue-analysis", hooks.PATH),),
    ),
    finalize=Finalize(
        hook=hooks.after,
        writes=(Out("issue-analysis-handoff", ISSUE_ANALYSIS_HANDOFF_PATH),),
        errors=(ValueError,),
        error_failure="output",
    ),
    output=IssueAnalysisPublishedV1,
)

__all__ = ["op"]
