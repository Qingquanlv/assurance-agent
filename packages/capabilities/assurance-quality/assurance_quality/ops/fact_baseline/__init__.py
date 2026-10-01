"""Fact baseline: record source-proven facts for the reviewed change."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, OutputError, Prepare

from assurance_quality.contracts.agent import FactBaselineResultV1
from assurance_quality.contracts.assessment import FactBaselineSkillInputV1, FinalizedFactBaselineV1
from assurance_quality.ops import router
from assurance_quality.ops.fact_baseline import hooks

op = router.agent(
    "fact-baseline",
    input=FactBaselineSkillInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-fact-baseline",
        result=FactBaselineResultV1,
        writes=("qa/results/facts/fact-baseline.json",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=FinalizedFactBaselineV1,
)

__all__ = ["op"]
