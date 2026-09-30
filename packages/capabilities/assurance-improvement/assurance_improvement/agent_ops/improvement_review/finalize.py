"""Finalize the improvement-review Agent result."""

from __future__ import annotations

from typing import cast

from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError, OutputError, run_finalize
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.agent import AgentFinalizeInputV1, ImprovementReviewResultV1
from assurance_improvement.contracts.delivery import artifact_digest, digest_hex
from assurance_improvement.operations.agent import structured

input_model = AgentFinalizeInputV1


def _commit(payload: AgentFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    del context
    try:
        document = ImprovementReviewResultV1.model_validate(structured(payload))
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if payload.subject is None or payload.projection is None:
        raise InputError("review finalize requires the authenticated subject and projection")
    if payload.subject.improvement_id != payload.improvement_id:
        raise OutputError("review subject improvement_id does not match")
    if payload.projection.improvement_id != payload.improvement_id:
        raise OutputError("review projection improvement_id does not match")
    if payload.projection.version != payload.expected_improvement_version:
        raise OutputError("review version does not match the current improvement")
    if digest_hex(artifact_digest(payload.subject)) != payload.subject_digest:
        raise OutputError("review subject digest does not match")
    if document.decision == "pass" and document.evidence_traceability != "complete":
        raise OutputError("pass review requires complete evidence traceability")
    return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=AgentFinalizeInputV1, commit=_commit)
