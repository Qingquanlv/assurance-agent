"""Commit the host's organized runtime evidence through the kernel workspace."""

from __future__ import annotations

from pydantic import ValidationError

from graph_engine.artifacts import stage_json_artifact
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.retro import WorkflowRuntimeEvidenceV2
from assurance_improvement.contracts.runtime_snapshot import (
    RetroRuntimeSnapshotInputV1,
    pre_retro_evidence_path,
)
from assurance_improvement.operations.common import succeeded, validate_input

_MISSING = "runtime evidence port is not configured"


class RetroRuntimeSnapshotHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        if context.runtime_evidence is None:
            return TaskOutcome.failed("configuration", _MISSING, retryable=False)
        try:
            payload = validate_input(RetroRuntimeSnapshotInputV1, request.input)
            document = WorkflowRuntimeEvidenceV2.model_validate(await context.runtime_evidence())
            if document.invocation_id != request.invocation_id:
                raise ValueError("runtime evidence invocation does not match this attempt")
            if document.change_id != payload.change_id:
                raise ValueError("runtime evidence change does not match the flow input")
            relative, _encoded = pre_retro_evidence_path(document)
            ref = stage_json_artifact(context.write_root, relative, document)
            return succeeded({"evidence_ref": {"path": ref.path, "digest": ref.digest}})
        except (ValidationError, ValueError, OSError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)


__all__ = ["RetroRuntimeSnapshotHandler"]
