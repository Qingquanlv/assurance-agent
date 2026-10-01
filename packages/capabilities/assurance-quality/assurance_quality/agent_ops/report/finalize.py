"""Finalize the report Agent result."""

from __future__ import annotations

import hashlib
from typing import cast

from agent_runtime_contracts.ops import OutputError, run_finalize, validate_output
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.agent import ReportResultV1
from assurance_quality.contracts.assessment import FinalizedReportV1, ReportFinalizeInputV1
from assurance_quality.operations.agent_skills import (
    QUALITY_OUTPUTS,
    REPORT_RESULT_ID,
    authenticate_report_input,
    canonical_file,
)

input_model = ReportFinalizeInputV1


def _commit(business: ReportFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    agent_run = business.agent_result
    document = validate_output(ReportResultV1, thaw_json(agent_run.result_payload))
    authenticate_report_input(business, context.project_root)
    expected = {
        "case_digest": business.case_digest,
        "plan_digest": business.plan_digest,
        "mapping_digest": business.mapping_digest,
        "execution_digest": business.execution_digest,
        "healing_digest": business.healing_digest,
        "trace_digest": business.trace_digest,
        "coverage_digest": business.coverage_digest,
        "issue_digest": business.issue_digest,
        "metrics_digest": business.metrics_digest,
    }
    actual = {
        "case_digest": document.case_digest,
        "plan_digest": document.plan_digest,
        "mapping_digest": document.mapping_digest,
        "execution_digest": document.execution_digest,
        "healing_digest": document.healing_digest,
        "trace_digest": document.trace_digest,
        "coverage_digest": document.coverage_digest,
        "issue_digest": document.issue_digest,
        "metrics_digest": document.metrics_digest,
    }
    for key, locked in expected.items():
        if actual[key] != locked:
            raise OutputError(f"report {key} is not closed against the locked projection")
    if document.change_id != business.change_id or document.batch_id != business.batch_id:
        raise OutputError("report identity does not match the locked change")
    if document.purpose != business.purpose:
        raise OutputError("report purpose does not match the locked request")
    allowed = QUALITY_OUTPUTS[REPORT_RESULT_ID](business.change_id)
    if document.report_files != allowed:
        raise OutputError("report files must exactly match the declared report write set")
    staged = {
        path.relative_to(context.write_root).as_posix()
        for path in context.write_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if staged != set(document.report_files):
        raise OutputError("report result does not match the actual candidate write set")
    refs = tuple(
        EvidenceArtifactRefV1(
            path=relative,
            digest=hashlib.sha256(canonical_file(context.write_root, relative).read_bytes()).hexdigest(),
        )
        for relative in document.report_files
    )
    finalized = FinalizedReportV1(
        change_id=business.change_id,
        coverage_epoch=business.coverage_epoch,
        batch_id=business.batch_id,
        purpose=business.purpose,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
        inspection_receipt=business.inspection.inspection_receipt,
        report_refs=refs,
    )
    return TaskOutcome.succeeded(cast(JSONValue, finalized.model_dump(mode="json")))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=ReportFinalizeInputV1, commit=_commit)
