"""Bind this run's test files, then seal the authored suite."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ValidationError

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import (
    AgentBindingDataV1,
    FinalizeContext,
    InputError,
    OutputError,
    PrepareContext,
)
from assurance_generation.operations.resolve_inputs import reviewed_case_field
from agent_runtime_contracts.wire.models import ResultContract

from assurance_generation.contracts.agent import CodegenFinalizeInputV1, CodegenInputV1
from assurance_generation.contracts.codegen import CodegenAuthoringV1, CodegenResultV1
from assurance_generation.contracts.families import LayerName
from assurance_generation.operations.codegen import (
    assemble_codegen_request,
    codegen_prepare_state,
    commit_codegen,
)

FAMILY: LayerName = "e2e"


def before(ctx: PrepareContext, business: CodegenInputV1) -> CodegenInputV1:
    validated, paths = codegen_prepare_state(FAMILY, business, ctx)
    ctx.bind("qa/tests", paths)
    return validated


def request(
    ctx: PrepareContext,
    business: BaseModel,
    binding: AgentBindingDataV1,
    allowed: tuple[str, ...],
    result: ResultContract,
    skill_text: str,
) -> AgentRunRequest:
    del allowed, result
    if not isinstance(business, CodegenInputV1):
        raise InputError("codegen business input is not CodegenInputV1")
    return assemble_codegen_request(
        family=FAMILY,
        business=business,
        binding=binding,
        context=ctx,
        skill_text=skill_text,
    )


def after(ctx: FinalizeContext, business: CodegenInputV1, result: CodegenAuthoringV1) -> CodegenResultV1:
    del business, result
    payload = _finalize_input(ctx)
    return commit_codegen(FAMILY, payload, ctx)


def _finalize_input(ctx: FinalizeContext) -> CodegenFinalizeInputV1:
    prepared = ctx.prepared if isinstance(ctx.prepared, Mapping) else {}
    fields = set(CodegenFinalizeInputV1.model_fields) - {"agent_result"}
    data = {key: prepared[key] for key in fields if key in prepared}
    data.update(reviewed_case_field(ctx.project_root, prepared, OutputError))
    data["agent_result"] = ctx.agent_result.model_dump(mode="json")
    try:
        return CodegenFinalizeInputV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error
