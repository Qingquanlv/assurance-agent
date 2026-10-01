"""Issue triage: recommend one declared action for the locked evidence."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize

from assurance_quality.contracts.agent import IssueTriageResultV1, QualitySkillInputV1
from assurance_quality.ops import router
from assurance_quality.ops.issue_triage import hooks

op = router.agent(
    "issue-triage",
    input=QualitySkillInputV1,
    agent=Agent(
        profile="assurance-v1-reporter",
        skill="aa-issue-triage-advisor",
        result=IssueTriageResultV1,
        writes=("qa/results/inspect/issue-triage.json",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=IssueTriageResultV1,
)

__all__ = ["op"]
