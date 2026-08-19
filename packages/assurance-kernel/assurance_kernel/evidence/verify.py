"""Reconciled-phase evidence verify verdict (pure domain; no CLI I/O)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from assurance_kernel.artifacts.models.policy import Policy
from assurance_kernel.artifacts.models.trace import TraceGap, TraceGapV2, TraceProjectionLike
from assurance_kernel.artifacts.policy import policy_digest
from assurance_kernel.evidence.digests import projection_digest
from assurance_kernel.evidence.sufficiency import SufficiencyReportLike

VerifyVerdict = Literal["pass", "fail", "needs_human"]

VERIFY_BLOCKING_GAP_CODES = frozenset(
    {
        "manifest_missing",
        "case_unreadable",
        "result_missing",
        "result_corrupt",
        "result_identity_mismatch",
        "batch_id_unparseable",
        "failure_analysis_missing",
        "failure_analysis_identity_mismatch",
        "issues_snapshot_missing",
        "issues_snapshot_identity_mismatch",
        "problems_snapshot_missing",
        "tests_tree_digest_mismatch",
        "problem_alias_invalid",
        "issue_analysis_failed",
        "project_sync_pending",
        "issue_reconcile_failed",
        "issue_reconciliation_unavailable",
    }
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class VerifyScope(BaseModel):
    model_config = _FROZEN

    cases: tuple[str, ...]
    batch: str
    policy_digest: str
    projection_digest: str


class VerifyGapItem(BaseModel):
    model_config = _FROZEN

    code: str
    source: str
    batch_id: str | None = None
    target: str | None = None
    detail: str = ""


class VerifyInsufficientCase(BaseModel):
    model_config = _FROZEN

    case_id: str
    missing_kinds: tuple[str, ...]
    reason_codes: tuple[str, ...]
    execution_state: str


class VerifyResult(BaseModel):
    model_config = _FROZEN

    verdict: VerifyVerdict
    change_id: str
    phase: Literal["reconciled"] = "reconciled"
    as_of: datetime
    policy_digest: str
    projection_digest: str
    scope: VerifyScope | None = None
    blocking_gaps: tuple[VerifyGapItem, ...] = ()
    open_problem_ids: tuple[str, ...] = ()
    insufficient: tuple[VerifyInsufficientCase, ...] = ()
    warnings: tuple[str, ...] = ()


def _gap_item(gap: TraceGap | TraceGapV2) -> VerifyGapItem:
    return VerifyGapItem(
        code=gap.code,
        source=gap.source,
        batch_id=gap.batch_id,
        target=gap.target,
        detail=gap.detail,
    )


def _insufficient_cases(report: SufficiencyReportLike) -> tuple[VerifyInsufficientCase, ...]:
    return tuple(
        VerifyInsufficientCase(
            case_id=verdict.case_id,
            missing_kinds=tuple(verdict.missing_kinds),
            reason_codes=verdict.reason_codes,
            execution_state=verdict.execution_state,
        )
        for verdict in report.verdicts
        if not verdict.sufficient
    )


def _collect_open_problem_ids(projection: TraceProjectionLike) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for row in projection.rows:
        for problem_id in row.open_problem_ids:
            if problem_id not in seen:
                seen.add(problem_id)
                ordered.append(problem_id)
    return tuple(ordered)


def _blocking_gaps(projection: TraceProjectionLike) -> tuple[VerifyGapItem, ...]:
    return tuple(_gap_item(gap) for gap in projection.gaps if gap.code in VERIFY_BLOCKING_GAP_CODES)


def evaluate_verify_verdict(
    projection: TraceProjectionLike,
    policy: Policy,
    report: SufficiencyReportLike,
    *,
    digest: str | None = None,
) -> VerifyResult:
    """Apply verify verdict ordering to a reconciled projection and sufficiency report."""
    pol_digest = digest if digest is not None else policy_digest(policy)
    proj_digest = projection_digest(projection)
    blocking = _blocking_gaps(projection)

    if blocking or projection.integrity == "incomplete":
        return VerifyResult(
            verdict="fail",
            change_id=projection.change_id,
            as_of=report.as_of,
            policy_digest=pol_digest,
            projection_digest=proj_digest,
            blocking_gaps=blocking,
        )

    open_ids = _collect_open_problem_ids(projection)
    if open_ids:
        return VerifyResult(
            verdict="fail",
            change_id=projection.change_id,
            as_of=report.as_of,
            policy_digest=pol_digest,
            projection_digest=proj_digest,
            open_problem_ids=open_ids,
        )

    insufficient = _insufficient_cases(report)
    if insufficient:
        action = policy.evidence_sufficiency.on_insufficient
        if action == "block":
            return VerifyResult(
                verdict="fail",
                change_id=projection.change_id,
                as_of=report.as_of,
                policy_digest=pol_digest,
                projection_digest=proj_digest,
                insufficient=insufficient,
            )
        if action == "require_human":
            return VerifyResult(
                verdict="needs_human",
                change_id=projection.change_id,
                as_of=report.as_of,
                policy_digest=pol_digest,
                projection_digest=proj_digest,
                insufficient=insufficient,
            )
        warnings = tuple(f"{item.case_id}: {','.join(item.reason_codes)}" for item in insufficient)
        return VerifyResult(
            verdict="pass",
            change_id=projection.change_id,
            as_of=report.as_of,
            policy_digest=pol_digest,
            projection_digest=proj_digest,
            scope=VerifyScope(
                cases=tuple(row.case_id for row in projection.rows),
                batch=projection.authoritative_batch_id,
                policy_digest=pol_digest,
                projection_digest=proj_digest,
            ),
            insufficient=insufficient,
            warnings=warnings,
        )

    return VerifyResult(
        verdict="pass",
        change_id=projection.change_id,
        as_of=report.as_of,
        policy_digest=pol_digest,
        projection_digest=proj_digest,
        scope=VerifyScope(
            cases=tuple(row.case_id for row in projection.rows),
            batch=projection.authoritative_batch_id,
            policy_digest=pol_digest,
            projection_digest=proj_digest,
        ),
    )
