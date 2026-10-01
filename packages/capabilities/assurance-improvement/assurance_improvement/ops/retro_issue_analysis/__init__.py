"""Retro issue analysis: record signals from the locked issue evidence slice."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Prepare

from assurance_improvement.contracts.agent import RetroAnalysisInputV1, RetroAnalysisResultV3
from assurance_improvement.ops import router
from assurance_improvement.ops.retro_issue_analysis import hooks

op = router.agent(
    "retro-issue-analysis",
    input=RetroAnalysisInputV1,
    prepare=Prepare(),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-retro-issue-analysis",
        result=RetroAnalysisResultV3,
        writes=("qa/results/retro/retro-issue-analysis.json",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=RetroAnalysisResultV3,
)

__all__ = ["op"]
