"""Retro synthesis: turn locked domain signals into improvement candidates."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Prepare

from assurance_improvement.contracts.agent import RetroAnalysisResultV3, RetroSynthesisInputV1
from assurance_improvement.ops import router
from assurance_improvement.ops.retro import hooks

op = router.agent(
    "retro",
    input=RetroSynthesisInputV1,
    prepare=Prepare(),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-retro",
        result=RetroAnalysisResultV3,
        writes=("qa/results/retro/retro.json",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=RetroAnalysisResultV3,
)

__all__ = ["op"]
