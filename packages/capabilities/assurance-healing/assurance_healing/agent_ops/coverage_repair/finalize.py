"""Finalize the coverage-repair Agent result."""

from __future__ import annotations

from typing import cast

from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError, OutputError, run_finalize
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.agent import CoverageRepairFinalizeInputV1
from assurance_healing.contracts.coverage_repair import CoverageRepairApplySummary
from assurance_healing.operations.agent import brief_locator_ids, structured, under_root, workspace_file

input_model = CoverageRepairFinalizeInputV1


def _commit(payload: CoverageRepairFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    if (
        payload.baseline_digest != payload.prepare.baseline_digest
        or payload.change_id != payload.prepare.change_id
        or payload.brief != payload.prepare.brief
    ):
        raise InputError("finalize digests do not match the locked prepare payload")
    try:
        summary = CoverageRepairApplySummary.model_validate(structured(payload.agent_result.result_payload))
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if summary.change_id != payload.change_id or summary.change_id != payload.brief.change_id:
        raise OutputError("coverage repair identity does not match the locked change")
    locators = brief_locator_ids(payload.brief)
    unknown = [item for item in summary.addressed_items if item not in locators]
    if unknown:
        raise OutputError(f"addressed item is not a brief locator: {unknown[0]}")
    allowed = set(payload.brief.allowed_test_files)
    for path in summary.files_modified:
        if path not in allowed or not under_root(path, payload.allowed_roots):
            raise OutputError(f"undeclared target file: {path}")
        workspace_file(context.project_root, path)
    for path in payload.artifact_paths:
        workspace_file(context.project_root, path)
    return TaskOutcome.succeeded(cast(JSONValue, summary.model_dump(mode="json")))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=CoverageRepairFinalizeInputV1, commit=_commit)
