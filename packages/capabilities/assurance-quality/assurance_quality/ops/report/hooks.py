"""Authenticate the inspection chain, then bind the staged report bytes."""

from __future__ import annotations

import hashlib

from agent_runtime_contracts.ops import FinalizeContext, OutputError, PrepareContext

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.agent import ReportResultV1
from assurance_quality.contracts.assessment import FinalizedReportV1, ReportSkillInputV1
from assurance_quality.operations.agent_skills import authenticate_report_input, canonical_file

PATH = "qa/results/report/report.md"


def before(ctx: PrepareContext, business: ReportSkillInputV1) -> ReportSkillInputV1:
    authenticate_report_input(business, ctx.project_root)
    return business


def after(ctx: FinalizeContext, business: ReportSkillInputV1, result: ReportResultV1) -> FinalizedReportV1:
    authenticate_report_input(business, ctx.project_root)
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
        "case_digest": result.case_digest,
        "plan_digest": result.plan_digest,
        "mapping_digest": result.mapping_digest,
        "execution_digest": result.execution_digest,
        "healing_digest": result.healing_digest,
        "trace_digest": result.trace_digest,
        "coverage_digest": result.coverage_digest,
        "issue_digest": result.issue_digest,
        "metrics_digest": result.metrics_digest,
    }
    for key, locked in expected.items():
        if actual[key] != locked:
            raise OutputError(f"report {key} is not closed against the locked projection")
    if result.change_id != business.change_id or result.batch_id != business.batch_id:
        raise OutputError("report identity does not match the locked change")
    if result.purpose != business.purpose:
        raise OutputError("report purpose does not match the locked request")
    if result.report_files != (PATH,):
        raise OutputError("report files must exactly match the declared report write set")
    staged = {
        path.relative_to(ctx.write_root).as_posix()
        for path in ctx.write_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if staged != set(result.report_files):
        raise OutputError("report result does not match the actual candidate write set")
    refs = tuple(
        EvidenceArtifactRefV1(
            path=relative,
            digest=hashlib.sha256(canonical_file(ctx.write_root, relative).read_bytes()).hexdigest(),
        )
        for relative in result.report_files
    )
    return FinalizedReportV1(
        change_id=business.change_id,
        coverage_epoch=business.coverage_epoch,
        batch_id=business.batch_id,
        purpose=business.purpose,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
        inspection_receipt=business.inspection.inspection_receipt,
        report_refs=refs,
    )
