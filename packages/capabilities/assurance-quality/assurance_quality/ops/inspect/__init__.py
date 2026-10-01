"""Inspect: classify the current execution batch against the locked assessment."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, OutputError, Prepare

from assurance_quality.contracts.agent import InspectionResultV1
from assurance_quality.contracts.assessment import AssessmentSkillInputV1, FinalizedInspectionV1
from assurance_quality.ops import router
from assurance_quality.ops.inspect import hooks

op = router.agent(
    "inspect",
    input=AssessmentSkillInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-reviewer",
        skill="aa-inspect",
        result=InspectionResultV1,
        writes=("qa/results/inspect/inspection.json",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=FinalizedInspectionV1,
)

__all__ = ["op"]
