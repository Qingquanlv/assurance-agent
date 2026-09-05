"""Deterministic per-Case evidence sufficiency facts."""

from __future__ import annotations

from datetime import datetime, timedelta

from assurance_quality.contracts.sufficiency import TraceInsufficientCase, TraceSufficiencyFacts
from assurance_quality.contracts.trace import TraceProjectionV2, TraceRow

_REASON_ORDER = (
    "not_in_current_batch",
    "uncovered",
    "never_run",
    "execution_stale",
    "fuzz_run_missing",
    "perf_run_missing",
    "no_pass",
    "pass_stale",
)


def _is_fresh(ts: datetime, *, as_of: datetime, recency_hours: int) -> bool:
    return ts <= as_of and as_of - ts <= timedelta(hours=recency_hours)


def _case_reasons(row: TraceRow, *, batch_id: str, as_of: datetime, recency_hours: int) -> tuple[str, ...]:
    if not row.automation_required:
        return ()
    reasons: set[str] = set()
    execution = row.latest_execution
    passed = row.freshest_pass
    if row.presence_in_current_batch != "executed" or (
        execution is not None and execution.batch_id != batch_id
    ):
        reasons.add("not_in_current_batch")
    if row.coverage_state != "covered":
        reasons.add("uncovered")
    if execution is None:
        reasons.add("never_run")
    elif not _is_fresh(execution.ts, as_of=as_of, recency_hours=recency_hours):
        reasons.add("execution_stale")
    expected_target = row.case_type.lower()
    if row.case_type == "Fuzz" and (
        execution is None or execution.target != expected_target or execution.batch_id != batch_id
    ):
        reasons.add("fuzz_run_missing")
    if row.case_type == "Performance" and (
        execution is None or execution.target != expected_target or execution.batch_id != batch_id
    ):
        reasons.add("perf_run_missing")
    if passed is None:
        reasons.add("no_pass")
    elif not _is_fresh(passed.ts, as_of=as_of, recency_hours=recency_hours):
        reasons.add("pass_stale")
    return tuple(item for item in _REASON_ORDER if item in reasons)


def build_sufficiency_facts(
    projection: TraceProjectionV2,
    *,
    policy_digest: str,
    as_of: datetime,
    recency_hours: int,
) -> TraceSufficiencyFacts:
    if not policy_digest:
        raise ValueError("policy_digest must not be empty")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if recency_hours <= 0:
        raise ValueError("recency_hours must be positive")
    insufficient = tuple(
        TraceInsufficientCase(case_id=row.case_id, reason_codes=reasons)  # type: ignore[arg-type]
        for row in projection.rows
        if (
            reasons := _case_reasons(
                row,
                batch_id=projection.authoritative_batch_id,
                as_of=as_of,
                recency_hours=recency_hours,
            )
        )
    )
    integrity_blocks = projection.integrity == "incomplete"
    return TraceSufficiencyFacts(
        schema_version="1",
        change_id=projection.change_id,
        authoritative_batch_id=projection.authoritative_batch_id,
        policy_digest=policy_digest,
        as_of=as_of,
        integrity=projection.integrity,
        integrity_blocks_routing=integrity_blocks,
        sufficient=not insufficient and not integrity_blocks,
        has_open_problems=any(row.open_problem_ids for row in projection.rows),
        error_code=None,
        insufficient_cases=insufficient,
        gap_codes=tuple(sorted({gap.code for gap in projection.gaps})),
    )


__all__ = ["build_sufficiency_facts"]
