"""Authenticate the reviewed case, then seal the staged fact baseline."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext

from assurance_intake.contracts.workflow import ReviewedCaseV1
from assurance_quality.contracts.agent import FactBaselineResultV1
from assurance_quality.contracts.assessment import (
    FactBaselineBoundInputV1,
    FactBaselineSkillInputV1,
    FinalizedFactBaselineV1,
)
from assurance_quality.operations.agent_skills import (
    authenticate_fact_baseline_input,
    captured_agent_document,
    open_quality_artifact,
)

PATH = "qa/results/facts/fact-baseline.json"


def _skill(
    ctx: PrepareContext | FinalizeContext,
    business: FactBaselineBoundInputV1,
    *,
    error: type[InputError] | type[OutputError],
) -> FactBaselineSkillInputV1:
    reviewed = open_quality_artifact(ctx.project_root, business.reviewed_case_ref, ReviewedCaseV1, error)
    return FactBaselineSkillInputV1(
        change_id=business.change_id,
        coverage_epoch=business.coverage_epoch,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
        capability_leafs=business.capability_leafs,
        artifact_paths=business.artifact_paths,
        reviewed_case=reviewed,
        validation_error=business.validation_error,
    )


def before(ctx: PrepareContext, business: FactBaselineBoundInputV1) -> FactBaselineSkillInputV1:
    skill = _skill(ctx, business, error=InputError)
    authenticate_fact_baseline_input(skill, ctx.project_root)
    return skill


def after(
    ctx: FinalizeContext, business: FactBaselineBoundInputV1, result: FactBaselineResultV1
) -> FinalizedFactBaselineV1:
    skill = _skill(ctx, business, error=OutputError)
    authenticate_fact_baseline_input(skill, ctx.project_root)
    baseline_ref = captured_agent_document(ctx, PATH, result)
    return FinalizedFactBaselineV1(
        agent_result=result,
        reviewed_case=skill.reviewed_case,
        fact_baseline_ref=baseline_ref,
    )
