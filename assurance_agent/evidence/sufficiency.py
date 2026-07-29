"""Pure evidence sufficiency evaluation against a TraceProjection and Policy."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from assurance_agent.artifacts.models.policy import EvidenceKind, Policy
from assurance_agent.artifacts.models.trace import TraceProjection, TraceRow

_FROZEN = ConfigDict(frozen=True, extra="forbid")

ExecutionState = Literal["never_run", "stale", "fresh"]


class RowVerdict(BaseModel):
    model_config = _FROZEN

    case_id: str
    sufficient: bool
    missing_kinds: tuple[EvidenceKind, ...]
    reason_codes: tuple[str, ...]
    execution_state: ExecutionState


class SufficiencyReport(BaseModel):
    model_config = _FROZEN

    as_of: datetime
    recency_hours: int
    verdicts: tuple[RowVerdict, ...]

    @property
    def all_sufficient(self) -> bool:
        return all(verdict.sufficient for verdict in self.verdicts)


def evaluate_sufficiency(
    projection: TraceProjection,
    policy: Policy,
    *,
    as_of: datetime,
) -> SufficiencyReport:
    if as_of.tzinfo is None:
        raise TypeError("as_of must be timezone-aware")

    recency_hours = policy.evidence_sufficiency.recency_hours
    verdicts = tuple(
        _evaluate_row(row, policy, as_of=as_of, recency_hours=recency_hours) for row in projection.rows
    )
    return SufficiencyReport(
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
) -> RowVerdict:
    execution_state = _execution_state(row, as_of=as_of, recency_hours=recency_hours)
    required = policy.evidence_sufficiency.required_kinds[row.case_type]
    missing: list[EvidenceKind] = []
    reasons: list[str] = []

    for kind in required:
        if _kind_satisfied(row, kind, as_of=as_of, recency_hours=recency_hours):
            continue
        missing.append(kind)
        reasons.append(_reason_for_missing(kind, row, execution_state))

    return RowVerdict(
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
) -> bool:
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
) -> str:
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
