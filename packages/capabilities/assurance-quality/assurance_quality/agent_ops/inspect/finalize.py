"""Finalize the inspect Agent result."""

from __future__ import annotations

from typing import cast

from agent_runtime_contracts.ops import InputError, OutputError, run_finalize, validate_output
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_quality.contracts.agent import InspectionResultV1
from assurance_quality.contracts.assessment import AssessmentFinalizeInputV1, FinalizedInspectionV1
from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
from assurance_quality.operations.agent_skills import (
    authenticate_assessment_input,
    load_json_ref,
    staged_agent_document,
)
from assurance_quality.operations.inspect import build_failure_classification_facts

input_model = AssessmentFinalizeInputV1


def _commit(business: AssessmentFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    agent_run = business.agent_result
    document = validate_output(InspectionResultV1, thaw_json(agent_run.result_payload))
    authenticate_assessment_input(business, context.project_root)
    if business.fact_baseline_ref is None:
        raise InputError("Inspect requires an authenticated fact baseline")
    relative = "qa/results/inspect/inspection.json"
    staged_agent_document(
        context=context,
        relative=relative,
        result=document,
        model=InspectionResultV1,
    )
    expected = {
        "execution_digest": business.assessment.execution_ref.digest,
        "healing_digest": (
            business.assessment.healing_ref.digest if business.assessment.healing_ref is not None else None
        ),
        "trace_digest": business.assessment.trace_ref.digest,
        "coverage_digest": business.assessment.gaps_ref.digest,
        "metrics_digest": business.assessment.metrics_ref.digest,
    }
    actual = {
        "execution_digest": document.execution_digest,
        "healing_digest": document.healing_digest,
        "trace_digest": document.trace_digest,
        "coverage_digest": document.coverage_digest,
        "metrics_digest": document.metrics_digest,
    }
    for key, locked in expected.items():
        if actual[key] != locked:
            raise OutputError(f"inspection {key} is not closed against the locked projection")
    if document.change_id != business.change_id or document.batch_id != business.batch_id:
        raise OutputError("inspection identity does not match the locked change")
    metrics = load_json_ref(context.project_root, business.assessment.metrics_ref, MetricsDocument)
    sufficiency = load_json_ref(
        context.project_root,
        business.assessment.sufficiency_ref,
        TraceSufficiencyFacts,
    )
    execution = load_json_ref(
        context.project_root,
        business.assessment.execution_ref,
        ExecutionEvidenceV1,
    )
    failure_facts, reason_codes = build_failure_classification_facts(
        execution,
        metrics,
    )
    has_failures = any(item.status == "failed" for item in execution.results)
    if not document.classification_performed:
        raise OutputError("inspection did not complete authenticated classification")
    expected_status = "analyzed" if has_failures else "no_failures"
    if document.status != expected_status:
        raise OutputError(f"inspection status must be {expected_status} for the authenticated execution")
    finalized = FinalizedInspectionV1(
        agent_result=document,
        assessment=business.assessment,
        reviewed_case=business.reviewed_case,
        mapping_ref=business.mapping_ref,
        metrics=metrics,
        sufficiency=sufficiency,
        failure_facts=failure_facts,
        fact_baseline_ref=business.fact_baseline_ref,
        reason_codes=reason_codes,
    )
    return TaskOutcome.succeeded(cast(JSONValue, finalized.model_dump(mode="json")))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=AssessmentFinalizeInputV1, commit=_commit)
