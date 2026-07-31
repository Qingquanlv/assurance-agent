"""Pure evidence sufficiency evaluation against a TraceProjection and Policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from assurance_agent.artifacts.models.policy import EvidenceKind, PlanCheckAction, Policy
from assurance_agent.artifacts.models.sufficiency import (
    ExecutionState,
    SufficiencyReasonCode,
    SufficiencyReportV2,
    SufficiencyRowVerdictV2,
)
from assurance_agent.artifacts.models.trace import TraceProjectionLike, TraceRow
from assurance_agent.artifacts.policy import policy_digest
from assurance_agent.evidence.digests import projection_digest

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class RowVerdict(BaseModel):
    """Legacy V1 per-row verdict retained for old fixtures/readers."""

    model_config = _FROZEN

    case_id: str
    sufficient: bool
    missing_kinds: tuple[EvidenceKind, ...]
    reason_codes: tuple[str, ...]
    execution_state: ExecutionState


class SufficiencyReport(BaseModel):
    """Legacy V1 unbound report retained for old fixtures/readers."""

    model_config = _FROZEN

    as_of: datetime
    recency_hours: int
    verdicts: tuple[RowVerdict, ...]

    @property
    def all_sufficient(self) -> bool:
        return all(verdict.sufficient for verdict in self.verdicts)


SufficiencyReportLike = SufficiencyReport | SufficiencyReportV2

EvidenceCoverageErrorCode = Literal["evidence_projection_missing", "policy_error"]


@dataclass(frozen=True)
class EvidenceCoverageEvaluation:
    report: SufficiencyReportLike | None
    action: PlanCheckAction | None
    error_code: EvidenceCoverageErrorCode | None

    def __post_init__(self) -> None:
        success = self.report is not None and self.action is not None and self.error_code is None
        failure = self.report is None and self.action is None and self.error_code is not None
        if not (success or failure):
            raise ValueError(
                "EvidenceCoverageEvaluation must be success (report+action, no error) "
                "or failure (no report/action, has error)"
            )


def build_evidence_coverage_evaluation(
    projection: TraceProjectionLike | None,
    policy: Policy | None,
    *,
    as_of: datetime,
) -> EvidenceCoverageEvaluation:
    if projection is None:
        return EvidenceCoverageEvaluation(
            report=None,
            action=None,
            error_code="evidence_projection_missing",
        )
    if policy is None:
        return EvidenceCoverageEvaluation(
            report=None,
            action=None,
            error_code="policy_error",
        )
    report = evaluate_sufficiency(
        projection,
        policy,
        as_of=as_of,
        require_current_batch=True,
    )
    return EvidenceCoverageEvaluation(
        report=report,
        action=policy.evidence_sufficiency.on_insufficient,
        error_code=None,
    )


def evaluate_sufficiency(
    projection: TraceProjectionLike,
    policy: Policy,
    *,
    as_of: datetime,
    require_current_batch: bool = False,
) -> SufficiencyReportV2:
    if as_of.tzinfo is None:
        raise TypeError("as_of must be timezone-aware")

    recency_hours = policy.evidence_sufficiency.recency_hours
    verdicts = tuple(
        _evaluate_row(
            row,
            policy,
            as_of=as_of,
            recency_hours=recency_hours,
            require_current_batch=require_current_batch,
        )
        for row in projection.rows
    )
    return SufficiencyReportV2(
        source_projection_digest=projection_digest(projection),
        source_policy_digest=policy_digest(policy),
        require_current_batch=require_current_batch,
        as_of=as_of,
        recency_hours=recency_hours,
        verdicts=verdicts,
    )


def _evaluate_row(
    row: TraceRow,
    policy: Policy,
    *,
    as_of: datetime,
    recency_hours: int,
    require_current_batch: bool,
) -> SufficiencyRowVerdictV2:
    execution_state = _execution_state(row, as_of=as_of, recency_hours=recency_hours)
    required = policy.evidence_sufficiency.required_kinds[row.case_type]
    missing: list[EvidenceKind] = []
    reasons: list[SufficiencyReasonCode] = []

    for kind in required:
        if _kind_satisfied(
            row,
            kind,
            as_of=as_of,
            recency_hours=recency_hours,
            require_current_batch=require_current_batch,
        ):
            continue
        missing.append(kind)
        reasons.append(
            _reason_for_missing(
                kind,
                row,
                execution_state,
                require_current_batch=require_current_batch,
            )
        )

    return SufficiencyRowVerdictV2(
        case_id=row.case_id,
        sufficient=not missing,
        missing_kinds=tuple(missing),
        reason_codes=tuple(reasons),
        execution_state=execution_state,
    )


def _execution_state(row: TraceRow, *, as_of: datetime, recency_hours: int) -> ExecutionState:
    latest = row.latest_execution
    if latest is None:
        return "never_run"
    if _within_recency(latest.ts, as_of=as_of, recency_hours=recency_hours):
        return "fresh"
    return "stale"


def _kind_satisfied(
    row: TraceRow,
    kind: EvidenceKind,
    *,
    as_of: datetime,
    recency_hours: int,
    require_current_batch: bool,
) -> bool:
    if require_current_batch and kind in ("execution_recent", "pass_status", "fuzz_run", "perf_run"):
        if row.presence_in_current_batch != "executed":
            return False
    if kind == "covered":
        return row.coverage_state == "covered"
    if kind == "execution_recent":
        latest = row.latest_execution
        return latest is not None and _within_recency(latest.ts, as_of=as_of, recency_hours=recency_hours)
    if kind == "fuzz_run":
        return "fuzz_run" in row.atemporal_kinds_present
    if kind == "perf_run":
        return "perf_run" in row.atemporal_kinds_present
    if kind == "pass_status":
        freshest = row.freshest_pass
        return freshest is not None and _within_recency(freshest.ts, as_of=as_of, recency_hours=recency_hours)
    raise AssertionError(f"unknown evidence kind: {kind!r}")


def _within_recency(ts: datetime, *, as_of: datetime, recency_hours: int) -> bool:
    cutoff = as_of - timedelta(hours=recency_hours)
    return ts >= cutoff


def _reason_for_missing(
    kind: EvidenceKind,
    row: TraceRow,
    execution_state: ExecutionState,
    *,
    require_current_batch: bool,
) -> SufficiencyReasonCode:
    if require_current_batch and kind in ("execution_recent", "pass_status", "fuzz_run", "perf_run"):
        if row.presence_in_current_batch != "executed":
            return "not_in_current_batch"
    if kind == "covered":
        return "uncovered"
    if kind == "execution_recent":
        return "never_run" if execution_state == "never_run" else "execution_stale"
    if kind == "fuzz_run":
        return "fuzz_run_missing"
    if kind == "perf_run":
        return "perf_run_missing"
    if kind == "pass_status":
        if row.freshest_pass is None:
            return "no_pass"
        return "pass_stale"
    raise AssertionError(f"unknown evidence kind: {kind!r}")
