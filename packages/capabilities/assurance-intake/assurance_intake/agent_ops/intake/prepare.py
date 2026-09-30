"""Prepare the intake Agent request."""

from __future__ import annotations

import yaml

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import IntakeInputV1
from assurance_intake.contracts.explore import REQUIREMENT_PATH, RUN_SPEC_SNAPSHOT_PATH
from assurance_intake.operations.prepare import (
    INTAKE_PERSONA,
    INTAKE_RESULT_ID,
    INTAKE_SKILL,
    intake_outputs,
    materialize_requirement,
    prepare_request,
    write_prepare_file,
)


def _build(business: IntakeInputV1, binding: AgentBindingDataV1, context: TaskContext) -> AgentRunRequest:
    write_prepare_file(context.write_root, REQUIREMENT_PATH, materialize_requirement(business.requirement))
    snapshot = yaml.safe_dump(
        {"candidate_test_families": list(business.candidate_test_families)},
        sort_keys=True,
    ).encode("utf-8")
    write_prepare_file(context.write_root, RUN_SPEC_SNAPSHOT_PATH, snapshot)
    return prepare_request(
        skill_path=INTAKE_SKILL,
        persona_path=INTAKE_PERSONA,
        business=business,
        binding=binding,
        result_schema_id=INTAKE_RESULT_ID,
        context=context,
        allowed_outputs=intake_outputs(business.change_id),
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(request, context, input_model=IntakeInputV1, build=_build)
