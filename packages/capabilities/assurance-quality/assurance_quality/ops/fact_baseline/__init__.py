"""Fact baseline: record source-proven facts for the reviewed change."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Out, OutputError, Prepare

from assurance_quality.contracts.agent import FactBaselineResultV1
from assurance_quality.contracts.assessment import FactBaselineBoundInputV1, FinalizedFactBaselineV1
from assurance_quality.ops import router
from assurance_quality.ops.fact_baseline import hooks

op = router.agent(
    "fact-baseline",
    input=FactBaselineBoundInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-doc-author",
        skill="aa-fact-baseline",
        result=FactBaselineResultV1,
        writes=(Out("baseline", hooks.PATH, model=FactBaselineResultV1, format="json"),),
        strict_files=True,
    ),
    finalize=Finalize(hook=hooks.after, same=("change_id",)),
    output=FinalizedFactBaselineV1,
)

__all__ = ["op"]
