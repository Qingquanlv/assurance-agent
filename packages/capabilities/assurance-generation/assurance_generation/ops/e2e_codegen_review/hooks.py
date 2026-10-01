"""Build the review request, then seal history under the family review directory."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ValidationError

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, FinalizeContext, InputError, PrepareContext
from agent_runtime_contracts.wire.models import ResultContract

from assurance_generation.contracts.agent import AgentFinalizeInputV1, CodegenInputV1
from assurance_generation.contracts.families import LayerName
from assurance_generation.contracts.reviews import PlanReviewAuthoring
from assurance_generation.operations.review import assemble_review_request, commit_review

FAMILY: LayerName = "e2e"


def request(
    ctx: PrepareContext,
    business: BaseModel,
    binding: AgentBindingDataV1,
    allowed: tuple[str, ...],
    result: ResultContract,
    skill_text: str,
) -> AgentRunRequest:
    del allowed, result
    return assemble_review_request(
        family=FAMILY,
        business=business,
        binding=binding,
        context=ctx,
        skill_text=skill_text,
    )


def after(ctx: FinalizeContext, business: CodegenInputV1, result: PlanReviewAuthoring) -> dict[str, object]:
    del business, result
    return commit_review(FAMILY, _finalize_input(ctx), ctx)


def _finalize_input(ctx: FinalizeContext) -> AgentFinalizeInputV1:
    prepared = ctx.prepared if isinstance(ctx.prepared, Mapping) else {}
    fields = set(AgentFinalizeInputV1.model_fields) - {"agent_result"}
    data = {key: prepared[key] for key in fields if key in prepared}
    data["agent_result"] = ctx.agent_result.model_dump(mode="json")
    try:
        return AgentFinalizeInputV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error
