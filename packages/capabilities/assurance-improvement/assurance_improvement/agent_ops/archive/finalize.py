"""Finalize the archive Agent result."""

from __future__ import annotations

from typing import cast

from agent_runtime_contracts.ops import InputError, OutputError, run_finalize, validate_output
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.agent import AgentFinalizeInputV1, ArchiveResultV1
from assurance_improvement.contracts.delivery import artifact_digest, digest_hex
from assurance_improvement.operations.agent import structured
from assurance_improvement.operations.common import validate_input

input_model = AgentFinalizeInputV1


def _commit(payload: AgentFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    del context
    document = validate_output(ArchiveResultV1, structured(payload))
    if document.change_id != payload.change_id:
        raise OutputError("archive identity does not match the locked change")
    if document.invocation_id != payload.invocation_id:
        raise OutputError("archive invocation is not locked")
    if document.archive_digest != payload.archive_digest:
        raise OutputError("archive digest is not closed against the locked tree")
    if payload.quality_report is None:
        raise InputError("archive finalize requires the authenticated quality report")
    if digest_hex(artifact_digest(payload.quality_report)) != payload.quality_report_digest:
        raise OutputError("quality report digest does not match")
    if payload.quality_report.change_id != payload.change_id:
        raise OutputError("quality report change_id does not match")
    issues = payload.quality_report.issues
    locked_risk = issues.issue_risk if issues is not None else None
    locked_rationale = issues.issue_risk_rationale if issues is not None else None
    if document.issue_risk != locked_risk:
        raise OutputError("archive issue_risk does not match the locked quality report")
    if document.issue_risk_rationale != locked_rationale:
        raise OutputError("archive issue_risk_rationale does not match the locked quality report")
    locked_paths = frozenset(payload.artifact_paths)
    if any(path not in locked_paths for path in document.artifact_paths):
        raise OutputError("archive artifact path is outside the locked manifest")
    if document.issue_risk not in {None, "clear"} and document.archive_status != "archived_with_warnings":
        raise OutputError("non-clear issue risk requires archived_with_warnings")
    return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(
        request, context, input_model=AgentFinalizeInputV1, commit=_commit, validate=validate_input
    )
