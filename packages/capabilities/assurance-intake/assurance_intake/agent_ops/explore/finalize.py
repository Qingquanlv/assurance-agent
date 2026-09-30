"""Finalize the explore Agent result."""

from __future__ import annotations

from typing import cast

from agent_runtime_contracts.ops import InputError, OutputError, run_finalize
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import AgentFinalizeInputV1
from assurance_intake.contracts.explore import (
    EXPLORATION_PATH,
    EXPLORE_AGENT_OUTPUT_PATHS,
    EXPLORE_OFFICIAL_OUTPUT_PATHS,
    ExploreAdvisoryV1,
)
from assurance_intake.operations.finalize import (
    artifact_list,
    case_change_id,
    file_digest,
    finalize_artifact_list,
    leafs,
    seal_official_exploration,
    validate_explore_outputs,
)

input_model = AgentFinalizeInputV1


def _commit(payload: AgentFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    if not payload.artifact_paths:
        raise InputError("artifact_paths must lock the expected output files")
    document = artifact_list(payload)
    change_id = case_change_id(payload.change_id)
    if set(document.output_files) != set(EXPLORE_AGENT_OUTPUT_PATHS):
        raise OutputError(
            "explore receipt must declare exactly exploration-draft.json and impact-inventory.json"
        )
    artifacts = finalize_artifact_list(payload, context.write_root)
    validate_explore_outputs(
        context.write_root,
        document.output_files,
        change_id=change_id,
        capability_leafs=leafs(payload.capability_leafs),
    )
    advisory = ExploreAdvisoryV1.model_validate_json(
        context.write_root.joinpath(*EXPLORE_AGENT_OUTPUT_PATHS[0].split("/")).read_bytes()
    )
    official, official_bytes = seal_official_exploration(
        context.write_root,
        advisory,
        candidate_families=frozenset(),
        policy_required=frozenset(),
    )
    del official
    artifacts = [item for item in artifacts if item["path"] != EXPLORE_AGENT_OUTPUT_PATHS[0]]
    artifacts.append({"path": EXPLORATION_PATH, "digest": file_digest(official_bytes)})
    official_paths = {item["path"] for item in artifacts}
    if not set(EXPLORE_OFFICIAL_OUTPUT_PATHS) <= official_paths:
        raise OutputError("finalize must return official exploration and inventory refs")
    artifacts.sort(key=lambda item: item["path"])
    return TaskOutcome.succeeded(cast(JSONValue, {"artifacts": artifacts}))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=AgentFinalizeInputV1, commit=_commit)
