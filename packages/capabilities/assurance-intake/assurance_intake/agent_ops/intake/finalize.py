"""Finalize the intake Agent result."""

from __future__ import annotations

from typing import cast

import yaml
from pydantic import ValidationError

from agent_runtime_contracts.ops import OutputError, run_finalize
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import AgentFinalizeInputV1
from assurance_intake.contracts.cases import IntakeQaV1
from assurance_intake.contracts.explore import REQUIREMENT_PATH
from assurance_intake.operations.finalize import (
    authenticate_files,
    case_change_id,
    file_digest,
    finalize_artifact_list,
    read_regular_bytes,
)

input_model = AgentFinalizeInputV1


def _commit(payload: AgentFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    artifacts = finalize_artifact_list(payload, context.write_root)
    requirement = authenticate_files(
        context.write_root,
        (REQUIREMENT_PATH,),
        payload.artifact_paths + (REQUIREMENT_PATH,),
    )
    marker_ref = next((item for item in artifacts if item["path"] == "qa/.qa.yaml"), None)
    if marker_ref is None:
        raise OutputError("intake receipt must include qa/.qa.yaml")
    change_id = case_change_id(payload.change_id)
    marker_bytes = read_regular_bytes(context.write_root, "qa/.qa.yaml", kind="intake marker")
    if file_digest(marker_bytes) != marker_ref["digest"]:
        raise OutputError("qa/.qa.yaml changed during finalization")
    try:
        marker = IntakeQaV1.model_validate(yaml.safe_load(marker_bytes))
    except (yaml.YAMLError, UnicodeError, ValidationError) as error:
        raise OutputError(f"invalid qa/.qa.yaml: {error}") from error
    if marker.change_id != change_id:
        raise OutputError("qa/.qa.yaml change_id does not match locked change_id")
    merged = {item["path"]: item for item in (*artifacts, *requirement)}
    return TaskOutcome.succeeded(cast(JSONValue, {"artifacts": [merged[path] for path in sorted(merged)]}))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=AgentFinalizeInputV1, commit=_commit)
