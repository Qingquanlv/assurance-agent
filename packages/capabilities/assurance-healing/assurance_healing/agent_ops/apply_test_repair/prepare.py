"""Prepare the apply-test-repair Agent request."""

from __future__ import annotations

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, InstructionPart, prompt_model_json
from agent_runtime_contracts.ops import (
    AgentBindingDataV1,
    InputError,
    OutputError,
    agent_run_request,
    run_prepare,
)
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.application import ApplyTestRepairInputV1
from assurance_healing.operations.agent import result_contract
from assurance_healing.operations.application import (
    APPLICATION_RESULT_FILE,
    APPLICATION_RESULT_ID,
    APPLICATION_SKILL,
    approved_sources,
)
from assurance_healing.resource_loader import resource_text


def _build(
    business: ApplyTestRepairInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    approved_paths = tuple(approved_sources(business, context.project_root))
    payload = prompt_model_json(business)
    payload["allowed_test_paths"] = list(approved_paths)
    return agent_run_request(
        instructions=(
            InstructionPart.text("text/plain", resource_text(APPLICATION_SKILL)),
            InstructionPart.from_json(payload),
        ),
        validation_error=business.validation_error,
        result=result_contract(APPLICATION_RESULT_ID, APPLICATION_RESULT_FILE),
        binding=binding,
        roots=context,
        allowed_outputs=approved_paths,
        scope_id=business.change_id,
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(
        request,
        context,
        input_model=ApplyTestRepairInputV1,
        build=_build,
        input_errors=(ValidationError, InputError, OutputError),
    )
