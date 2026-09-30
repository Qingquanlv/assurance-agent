"""Prepare the explore Agent request."""

from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, InputError, run_prepare
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import ExploreInputV1
from assurance_intake.operations.explore_context import build_explore_context
from assurance_intake.operations.prepare import (
    EXPLORE_PERSONA,
    EXPLORE_RESULT_ID,
    EXPLORE_SKILL,
    explore_outputs,
    prepare_request,
)


def _build(business: ExploreInputV1, binding: AgentBindingDataV1, context: TaskContext) -> AgentRunRequest:
    document = build_explore_context(
        context.project_root,
        change_id=business.change_id,
        capability_leafs=business.capability_leafs,
    )
    relative = "qa/results/explore/context.json"
    path = context.write_root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(document.model_dump(mode="json")) + b"\n")
    return prepare_request(
        skill_path=EXPLORE_SKILL,
        persona_path=EXPLORE_PERSONA,
        business=business,
        binding=binding,
        result_schema_id=EXPLORE_RESULT_ID,
        context=context,
        allowed_outputs=explore_outputs(business.change_id),
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(
        request,
        context,
        input_model=ExploreInputV1,
        build=_build,
        input_errors=(InputError, ValueError),
    )
