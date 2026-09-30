"""Finalize the fix-proposal Agent result."""

from __future__ import annotations

import json
from typing import cast

from pydantic import ValidationError

from agent_runtime_contracts.ops import OutputError, run_finalize
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.agent import FixProposalFinalizeInputV1, FixProposalResultV1
from assurance_healing.operations.agent import require_prepare_lock, structured, under_root, workspace_file

input_model = FixProposalFinalizeInputV1


def _commit(business: FixProposalFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    require_prepare_lock(business)
    agent_result = business.agent_result
    unknown = [item for item in business.claimed_capabilities if item not in business.capability_leafs]
    if unknown:
        raise OutputError(f"unknown capability: {unknown[0]}")
    try:
        proposal = FixProposalResultV1.model_validate(structured(agent_result.result_payload))
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if proposal.change_id != business.change_id:
        raise OutputError("proposal change_id does not match the locked change")
    allowed = set(business.allowed_paths)
    for item in proposal.proposals:
        if not item.eligible:
            continue
        if not item.files_to_modify:
            raise OutputError("eligible proposal requires files_to_modify")
        for path in item.files_to_modify:
            if path not in allowed or not under_root(path, business.allowed_roots):
                raise OutputError(f"undeclared target file: {path}")
            workspace_file(context.project_root, path)
    relative = "qa/results/healing/fix-proposal.json"
    staged = workspace_file(context.write_root, relative)
    try:
        staged_proposal = FixProposalResultV1.model_validate(json.loads(staged.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise OutputError("staged fix proposal is not the typed agent result") from error
    if staged_proposal != proposal:
        raise OutputError("staged fix proposal differs from the typed agent result")
    expected = canonical_json_bytes(cast(JSONValue, proposal.model_dump(mode="json"))) + b"\n"
    if staged.read_bytes() != expected:
        raise OutputError("staged fix proposal must use canonical JSON bytes")
    return TaskOutcome.succeeded(cast(JSONValue, proposal.model_dump(mode="json")))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=FixProposalFinalizeInputV1, commit=_commit)
