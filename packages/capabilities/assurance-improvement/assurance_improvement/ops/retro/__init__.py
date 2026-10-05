"""Retro synthesis: turn locked domain signals into improvement candidates."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, ArtifactHandle, Finalize, Out, Prepare

from assurance_improvement.contracts.agent import RetroAnalysisResultV3, RetroSynthesisInputV1
from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_improvement.contracts.handoff import CANDIDATES
from assurance_improvement.ops import router
from assurance_improvement.ops.retro import hooks

_CONTEXT = TASK_ATTEMPT_CONTRACTS["assurance.improvement.retro-synthesize"].artifact(
    "context", slot="context_ref"
)

op = router.agent(
    "retro",
    input=RetroSynthesisInputV1,
    prepare=Prepare(
        hook=hooks.before,
        depends=(ArtifactHandle(ledger_key=_CONTEXT.ledger_key, slot=_CONTEXT.slot),),
    ),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-retro",
        result=RetroAnalysisResultV3,
        writes=("qa/results/retro/retro.json",),
        strict_files=True,
    ),
    finalize=Finalize(hook=hooks.after, writes=(Out("candidates", CANDIDATES),)),
    output=RetroAnalysisResultV3,
)

__all__ = ["op"]
