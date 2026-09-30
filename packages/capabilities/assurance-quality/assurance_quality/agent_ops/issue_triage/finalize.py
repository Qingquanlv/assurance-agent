"""Finalize the issue-triage Agent result."""

from __future__ import annotations

from typing import cast

from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError, OutputError, run_finalize
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.agent import AgentFinalizeInputV1, IssueTriageResultV1
from assurance_quality.operations.agent_skills import TRIAGE_ACTIONS, structured

input_model = AgentFinalizeInputV1


def _commit(payload: AgentFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    del context
    try:
        document = IssueTriageResultV1.model_validate(structured(payload))
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if document.recommended_action not in TRIAGE_ACTIONS:
        raise OutputError(f"issue triage recommended_action is not declared: {document.recommended_action}")
    locked = payload.locked_evidence_digests
    if not locked:
        raise InputError("locked_evidence_digests must authenticate triage evidence")
    if dict(document.evidence_digests) != dict(locked):
        raise OutputError("issue triage evidence_digests must equal locked_evidence_digests")
    return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=AgentFinalizeInputV1, commit=_commit)
