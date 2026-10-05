"""Retro workflow analysis: record signals from the locked workflow evidence slice."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, ArtifactHandle, Finalize, Out, Prepare

from assurance_improvement.contracts.agent import RetroAnalysisInputV1, RetroAnalysisResultV3
from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_improvement.ops import router
from assurance_improvement.ops.retro_workflow_analysis import hooks

_SLICE = TASK_ATTEMPT_CONTRACTS["assurance.improvement.retro-build-slices"].artifact(
    "workflow", slot="evidence_slice_ref"
)

op = router.agent(
    "retro-workflow-analysis",
    input=RetroAnalysisInputV1,
    prepare=Prepare(
        hook=hooks.before,
        depends=(ArtifactHandle(ledger_key=_SLICE.ledger_key, slot=_SLICE.slot),),
    ),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-retro-workflow-analysis",
        result=RetroAnalysisResultV3,
        writes=(Out("workflow-analysis", "qa/results/retro/retro-workflow-analysis.json"),),
        strict_files=True,
    ),
    finalize=Finalize(hook=hooks.after),
    output=RetroAnalysisResultV3,
)

__all__ = ["op"]
