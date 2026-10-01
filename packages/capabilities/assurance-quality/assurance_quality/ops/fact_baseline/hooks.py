"""Authenticate the reviewed case, then seal the staged fact baseline."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, OutputError, PrepareContext

from assurance_quality.contracts.agent import FactBaselineResultV1
from assurance_quality.contracts.assessment import FactBaselineSkillInputV1, FinalizedFactBaselineV1
from assurance_quality.operations.agent_skills import authenticate_fact_baseline_input, staged_agent_document

PATH = "qa/results/facts/fact-baseline.json"


def before(ctx: PrepareContext, business: FactBaselineSkillInputV1) -> FactBaselineSkillInputV1:
    authenticate_fact_baseline_input(business, ctx.project_root)
    return business


def after(
    ctx: FinalizeContext, business: FactBaselineSkillInputV1, result: FactBaselineResultV1
) -> FinalizedFactBaselineV1:
    authenticate_fact_baseline_input(business, ctx.project_root)
    if result.change_id != business.change_id:
        raise OutputError("fact baseline change_id does not match the locked Reviewed Case")
    _, baseline_ref = staged_agent_document(
        context=ctx,
        relative=PATH,
        result=result,
        model=FactBaselineResultV1,
    )
    return FinalizedFactBaselineV1(
        agent_result=result,
        reviewed_case=business.reviewed_case,
        fact_baseline_ref=baseline_ref,
    )
