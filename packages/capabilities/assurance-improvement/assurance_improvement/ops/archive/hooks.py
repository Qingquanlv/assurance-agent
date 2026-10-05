"""Seal the archive receipt against the locked quality report."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError

from assurance_improvement.contracts.agent import (
    ArchivePublishedV1,
    ArchiveResultV1,
    ImprovementSkillInputV1,
)
from assurance_improvement.contracts.delivery import artifact_digest, digest_hex
from assurance_quality.contracts.report import QualityReport


def after(
    ctx: FinalizeContext, business: ImprovementSkillInputV1, result: ArchiveResultV1
) -> ArchivePublishedV1:
    report_raw = ctx.prepared.get("quality_report")
    if report_raw is None:
        raise InputError("archive finalize requires the authenticated quality report")
    report = QualityReport.model_validate(report_raw)
    if digest_hex(artifact_digest(report)) != business.quality_report_digest:
        raise OutputError("quality report digest does not match")
    if report.change_id != business.change_id:
        raise OutputError("quality report change_id does not match")
    issues = report.issues
    locked_risk = issues.issue_risk if issues is not None else None
    locked_rationale = issues.issue_risk_rationale if issues is not None else None
    if result.issue_risk != locked_risk:
        raise OutputError("archive issue_risk does not match the locked quality report")
    if result.issue_risk_rationale != locked_rationale:
        raise OutputError("archive issue_risk_rationale does not match the locked quality report")
    locked_paths = frozenset(business.artifact_paths)
    if any(path not in locked_paths for path in result.artifact_paths):
        raise OutputError("archive artifact path is outside the locked manifest")
    if result.issue_risk not in {None, "clear"} and result.archive_status != "archived_with_warnings":
        raise OutputError("non-clear issue risk requires archived_with_warnings")
    return ArchivePublishedV1(archive_status=result.archive_status, result=result)
