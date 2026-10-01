"""Finalize the fact-baseline Agent result."""

from __future__ import annotations

from typing import cast

from agent_runtime_contracts.ops import OutputError, run_finalize, validate_output
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.agent import FactBaselineResultV1
from assurance_quality.contracts.assessment import FactBaselineFinalizeInputV1, FinalizedFactBaselineV1
from assurance_quality.operations.agent_skills import authenticate_fact_baseline_input, staged_agent_document

input_model = FactBaselineFinalizeInputV1


def _commit(business: FactBaselineFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    agent_run = business.agent_result
    document = validate_output(FactBaselineResultV1, thaw_json(agent_run.result_payload))
    authenticate_fact_baseline_input(business, context.project_root)
    if document.change_id != business.change_id:
        raise OutputError("fact baseline change_id does not match the locked Reviewed Case")
    relative = "qa/results/facts/fact-baseline.json"
    _, baseline_ref = staged_agent_document(
        context=context,
        relative=relative,
        result=document,
        model=FactBaselineResultV1,
    )
    finalized = FinalizedFactBaselineV1(
        agent_result=document,
        reviewed_case=business.reviewed_case,
        fact_baseline_ref=baseline_ref,
    )
    return TaskOutcome.succeeded(cast(JSONValue, finalized.model_dump(mode="json")))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=FactBaselineFinalizeInputV1, commit=_commit)
