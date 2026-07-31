"""Pure four-layer Trace fact summary and execution/reconciled phase-pair validation."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence

from assurance_agent.artifacts.models.assurance import CASE_TYPES, LAYER_NAMES, CaseType, LayerName
from assurance_agent.artifacts.models.sufficiency import (
    EXECUTION_STATES,
    ExecutionState,
    LayerSufficiencyCounts,
    SufficiencyBindingError,
    SufficiencyReasonCode,
    SufficiencyReportV2,
    TraceLayerSufficiencySummary,
)
from assurance_agent.artifacts.models.trace import (
    TraceGapAggregate,
    TraceGapV1,
    TraceGapV2,
    TraceIntegrity,
    TraceLayerFacts,
    TraceLayerFactSummary,
    TraceProjectionLike,
    TraceProjectionV1,
    TraceProjectionV2,
    TraceRow,
)
from assurance_agent.evidence.digests import projection_digest

_INTEGRITY_RANK: dict[TraceIntegrity, int] = {
    "complete": 0,
    "degraded": 1,
    "incomplete": 2,
}

_RECONCILED_ONLY_GAP_CODES: frozenset[str] = frozenset(
    {
        "failure_analysis_missing",
        "issues_snapshot_missing",
        "problems_snapshot_missing",
        "problem_alias_invalid",
        "failure_analysis_identity_mismatch",
        "issues_snapshot_identity_mismatch",
        "issue_analysis_failed",
        "project_sync_pending",
        "issue_reconcile_failed",
        "issue_reconciliation_unavailable",
    }
)

_AUTHORITY_SOURCE_FILENAMES: frozenset[str] = frozenset(
    {
        "inspect/failure-analysis.json",
        "inspect/issue-evidence-manifest.json",
        "inspect/observations.json",
        "inspect/issue-candidates.json",
        "inspect/issue-reconcile-status.json",
        "issues/snapshot.json",
        "issues/events.jsonl",
        "qa/issues/problems.json",
        "qa/issues/events.jsonl",
    }
)

_MANIFEST_SOURCE_PREFIXES: tuple[str, ...] = (
    "execution/",
    "cases/",
    "facts/",
    "review/",
    "healing/",
    "codegen/",
)


class TraceLayerSummaryError(ValueError):
    """A projection cannot produce a conserving four-layer summary."""


class TracePhasePairError(ValueError):
    """Execution and reconciled projections violate enrichment-only monotonicity."""


def derive_trace_integrity(
    rows: Sequence[TraceRow],
    gaps: Sequence[TraceGapV1 | TraceGapV2],
) -> TraceIntegrity:
    if not rows:
        return "incomplete"
    if not gaps:
        return "complete"
    if {gap.code for gap in gaps} == {"mapped_test_missing_from_tree"}:
        return "degraded"
    return "incomplete"


def _aggregate_gaps(gaps: Sequence[TraceGapV1 | TraceGapV2]) -> TraceGapAggregate:
    counts: Counter[str] = Counter(gap.code for gap in gaps)
    by_code = {code: counts[code] for code in sorted(counts)}
    return TraceGapAggregate.model_validate({"total": len(gaps), "by_code": by_code})


def _summarize_layer(
    rows: Sequence[TraceRow],
    gaps: Sequence[TraceGapV1 | TraceGapV2],
    layer: LayerName,
    case_type: CaseType,
) -> TraceLayerFacts:
    covered = sum(1 for row in rows if row.coverage_state == "covered")
    uncovered = sum(1 for row in rows if row.coverage_state == "uncovered")
    not_required = sum(1 for row in rows if row.coverage_state == "not_required")
    automated = sum(1 for row in rows if row.automation_required)
    current_executed = sum(1 for row in rows if row.presence_in_current_batch == "executed")
    current_not_present = sum(1 for row in rows if row.presence_in_current_batch == "not_in_current_batch")
    target_not_selected = sum(1 for row in rows if row.presence_in_current_batch == "target_not_selected")
    latest_passed = sum(
        1 for row in rows if row.latest_execution is not None and row.latest_execution.status == "passed"
    )
    latest_failed = sum(
        1 for row in rows if row.latest_execution is not None and row.latest_execution.status == "failed"
    )
    latest_skipped = sum(
        1 for row in rows if row.latest_execution is not None and row.latest_execution.status == "skipped"
    )
    never_run = sum(1 for row in rows if row.latest_execution is None)
    failure_rows = sum(1 for row in rows if row.failures)
    failure_links = sum(len(row.failures) for row in rows)
    open_problem_rows = sum(1 for row in rows if row.open_problem_ids)
    open_problem_links = sum(len(row.open_problem_ids) for row in rows)
    unique_open_problems = len({pid for row in rows for pid in row.open_problem_ids})
    return TraceLayerFacts(
        layer=layer,
        case_type=case_type,
        total=len(rows),
        automated=automated,
        covered=covered,
        uncovered=uncovered,
        not_required=not_required,
        current_executed=current_executed,
        current_not_present=current_not_present,
        target_not_selected=target_not_selected,
        latest_passed=latest_passed,
        latest_failed=latest_failed,
        latest_skipped=latest_skipped,
        never_run=never_run,
        failure_rows=failure_rows,
        failure_links=failure_links,
        open_problem_rows=open_problem_rows,
        open_problem_links=open_problem_links,
        unique_open_problems=unique_open_problems,
        gaps=_aggregate_gaps(gaps),
    )


def _validate_fact_arithmetic(
    summary: TraceLayerFactSummary,
    projection: TraceProjectionLike,
) -> None:
    if sum(layer.total for layer in summary.layers) != len(projection.rows):
        raise TraceLayerSummaryError("layer totals do not conserve projection rows")
    gap_total = sum(layer.gaps.total for layer in summary.layers) + summary.global_gaps.total
    if gap_total != len(projection.gaps):
        raise TraceLayerSummaryError("layer/global gaps do not conserve projection gaps")


def join_layer_sufficiency(
    projection: TraceProjectionLike,
    facts: TraceLayerFactSummary,
    report: SufficiencyReportV2,
    *,
    expected_policy_digest: str,
) -> TraceLayerSufficiencySummary:
    """Join a bound V2 sufficiency report onto four-layer facts without re-evaluating policy."""
    if not isinstance(report, SufficiencyReportV2):
        raise SufficiencyBindingError("join requires SufficiencyReportV2")

    proj_case_ids: list[str] = []
    seen_proj: set[str] = set()
    rows_by_id: dict[str, TraceRow] = {}
    for row in projection.rows:
        if row.case_id in seen_proj:
            raise SufficiencyBindingError(f"duplicate projection case_id: {row.case_id}")
        seen_proj.add(row.case_id)
        proj_case_ids.append(row.case_id)
        rows_by_id[row.case_id] = row

    verdict_ids = [verdict.case_id for verdict in report.verdicts]
    if len(set(verdict_ids)) != len(verdict_ids):
        raise SufficiencyBindingError("duplicate sufficiency verdict case_id")
    if set(verdict_ids) != set(proj_case_ids):
        raise SufficiencyBindingError("projection and sufficiency case sets must be equal")

    digest = projection_digest(projection)
    if report.source_projection_digest != digest:
        raise SufficiencyBindingError("report source_projection_digest mismatch")
    if facts.source_projection_digest != digest:
        raise SufficiencyBindingError("facts source_projection_digest mismatch")
    if (
        facts.change_id != projection.change_id
        or facts.phase != projection.phase
        or facts.authoritative_batch_id != projection.authoritative_batch_id
    ):
        raise SufficiencyBindingError("facts identity does not match projection")
    if report.source_policy_digest != expected_policy_digest:
        raise SufficiencyBindingError("report source_policy_digest mismatch")
    if report.semantics != "evidence_sufficiency/v2":
        raise SufficiencyBindingError("report semantics must be evidence_sufficiency/v2")
    if report.require_current_batch is not True:
        raise SufficiencyBindingError("join requires require_current_batch=True")

    verdict_by_id = {verdict.case_id: verdict for verdict in report.verdicts}
    layers: list[LayerSufficiencyCounts] = []
    for layer_name, case_type, fact_layer in zip(LAYER_NAMES, CASE_TYPES, facts.layers, strict=True):
        if fact_layer.layer != layer_name or fact_layer.case_type != case_type:
            raise SufficiencyBindingError("facts layer/case_type assignment mismatch")
        layer_rows = [row for row in projection.rows if row.case_type == case_type]
        sufficient = 0
        insufficient = 0
        reason_counter: Counter[SufficiencyReasonCode] = Counter()
        state_counter: Counter[ExecutionState] = Counter()
        for row in layer_rows:
            verdict = verdict_by_id[row.case_id]
            if verdict.sufficient:
                sufficient += 1
            else:
                insufficient += 1
            reason_counter.update(verdict.reason_codes)
            state_counter[verdict.execution_state] += 1
        if sufficient + insufficient != fact_layer.total:
            raise SufficiencyBindingError(
                f"layer {layer_name} sufficiency counts do not conserve facts.total"
            )
        reason_counts: dict[SufficiencyReasonCode, int] = {
            code: reason_counter[code]
            for code in sorted(reason_counter)
            if reason_counter[code] > 0
        }
        execution_state_counts: dict[ExecutionState, int] = {
            state: state_counter.get(state, 0) for state in EXECUTION_STATES
        }
        layers.append(
            LayerSufficiencyCounts(
                layer=layer_name,
                case_type=case_type,
                sufficient=sufficient,
                insufficient=insufficient,
                reason_counts=reason_counts,
                execution_state_counts=execution_state_counts,
            )
        )

    if sum(layer.sufficient for layer in layers) != sum(1 for v in report.verdicts if v.sufficient):
        raise SufficiencyBindingError("sufficient totals do not conserve report verdicts")
    if sum(layer.insufficient for layer in layers) != sum(
        1 for v in report.verdicts if not v.sufficient
    ):
        raise SufficiencyBindingError("insufficient totals do not conserve report verdicts")

    return TraceLayerSufficiencySummary(
        schema_version="1",
        source_projection_digest=digest,
        source_policy_digest=report.source_policy_digest,
        semantics="evidence_sufficiency/v2",
        require_current_batch=True,
        as_of=report.as_of,
        recency_hours=report.recency_hours,
        layers=tuple(layers),
    )


def summarize_projection_by_layer(
    projection: TraceProjectionLike,
) -> TraceLayerFactSummary:
    seen_cases: set[str] = set()
    for row in projection.rows:
        if row.case_id in seen_cases:
            raise TraceLayerSummaryError(f"duplicate case_id: {row.case_id}")
        seen_cases.add(row.case_id)

    known_targets = set(LAYER_NAMES)
    for gap in projection.gaps:
        if gap.target is not None and gap.target not in known_targets:
            raise TraceLayerSummaryError(f"unknown gap target: {gap.target!r}")

    rows_by_type = {
        case_type: tuple(row for row in projection.rows if row.case_type == case_type)
        for case_type in CASE_TYPES
    }
    layer_rows = tuple(
        _summarize_layer(
            rows_by_type[case_type],
            tuple(gap for gap in projection.gaps if gap.target == layer),
            layer,
            case_type,
        )
        for layer, case_type in zip(LAYER_NAMES, CASE_TYPES, strict=True)
    )
    summary = TraceLayerFactSummary(
        schema_version="1",
        change_id=projection.change_id,
        phase=projection.phase,
        authoritative_batch_id=projection.authoritative_batch_id,
        source_projection_digest=projection_digest(projection),
        projection_integrity=projection.integrity,
        layers=layer_rows,
        global_gaps=_aggregate_gaps(tuple(gap for gap in projection.gaps if gap.target is None)),
    )
    if summary.projection_integrity != derive_trace_integrity(
        projection.rows,
        projection.gaps,
    ):
        raise TraceLayerSummaryError("projection integrity does not match rows/gaps")
    _validate_fact_arithmetic(summary, projection)
    return summary


def _gap_dump_key(gap: TraceGapV1 | TraceGapV2) -> str:
    return json.dumps(gap.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _row_execution_facts(row: TraceRow) -> dict[str, object]:
    return row.model_dump(mode="json", exclude={"failures", "open_problem_ids"})


def _is_allowed_reconciled_source(path: str) -> bool:
    if path in _AUTHORITY_SOURCE_FILENAMES:
        return True
    if path.startswith("/") or "\\" in path or ".." in path.split("/"):
        return False
    return any(path.startswith(prefix) for prefix in _MANIFEST_SOURCE_PREFIXES)


def validate_trace_phase_pair(
    execution: TraceProjectionV1 | TraceProjectionV2,
    reconciled: TraceProjectionV1 | TraceProjectionV2,
) -> None:
    """Raise TracePhasePairError when the phase pair is not monotonic."""
    if execution.phase != "execution" or reconciled.phase != "reconciled":
        raise TracePhasePairError("phases must be execution then reconciled")
    if execution.change_id != reconciled.change_id:
        raise TracePhasePairError("change_id mismatch")
    if execution.authoritative_batch_id != reconciled.authoritative_batch_id:
        raise TracePhasePairError("authoritative_batch_id mismatch")

    exec_cases = {row.case_id: row for row in execution.rows}
    rec_cases = {row.case_id: row for row in reconciled.rows}
    if set(exec_cases) != set(rec_cases):
        raise TracePhasePairError("case set mismatch")
    for case_id, exec_row in exec_cases.items():
        if _row_execution_facts(exec_row) != _row_execution_facts(rec_cases[case_id]):
            raise TracePhasePairError(f"shared row execution facts drifted: {case_id}")

    if execution.unmapped_tests != reconciled.unmapped_tests:
        raise TracePhasePairError("unmapped_tests mismatch")

    exec_sources = {source.path: source for source in execution.sources}
    rec_sources = {source.path: source for source in reconciled.sources}
    for path, source in exec_sources.items():
        if path not in rec_sources:
            raise TracePhasePairError(f"execution source missing from reconciled: {path}")
        if rec_sources[path] != source:
            raise TracePhasePairError(f"execution source rewritten: {path}")
    for path in set(rec_sources) - set(exec_sources):
        if not _is_allowed_reconciled_source(path):
            raise TracePhasePairError(f"unauthorized reconciled source: {path}")

    exec_gap_counts = Counter(_gap_dump_key(gap) for gap in execution.gaps)
    rec_gap_counts = Counter(_gap_dump_key(gap) for gap in reconciled.gaps)
    if exec_gap_counts - rec_gap_counts:
        raise TracePhasePairError("execution gaps were deleted or rewritten")
    for key in rec_gap_counts - exec_gap_counts:
        sample = next(gap for gap in reconciled.gaps if _gap_dump_key(gap) == key)
        if sample.code not in _RECONCILED_ONLY_GAP_CODES:
            raise TracePhasePairError(f"unauthorized reconciled gap: {sample.code}")

    if reconciled.integrity != derive_trace_integrity(reconciled.rows, reconciled.gaps):
        raise TracePhasePairError("reconciled integrity does not match rows/gaps")
    if execution.integrity != derive_trace_integrity(execution.rows, execution.gaps):
        raise TracePhasePairError("execution integrity does not match rows/gaps")
    if _INTEGRITY_RANK[reconciled.integrity] < _INTEGRITY_RANK[execution.integrity]:
        raise TracePhasePairError("integrity improved across phases")
