"""Four-layer fact summary arithmetic and phase-pair monotonicity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.trace import (
    TraceExecution,
    TraceFailure,
    TraceGap,
    TraceGapAggregate,
    TraceGapV1,
    TraceGapV2,
    TraceLayerFacts,
    TraceLayerFactSummary,
    TraceProjectionV1,
    TraceProjectionV2,
    TraceRow,
    TraceSource,
    TraceTestRef,
    UnmappedTest,
)
from assurance_agent.evidence.layer_summary import (
    TraceLayerSummaryError,
    TracePhasePairError,
    derive_trace_integrity,
    summarize_projection_by_layer,
    validate_trace_phase_pair,
)

TS = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


def _exec(
    *,
    target: Literal["api", "e2e", "fuzz", "performance"] = "api",
    status: Literal["passed", "failed", "skipped"] = "passed",
    batch_id: str = "B1",
) -> TraceExecution:
    return TraceExecution(
        batch_id=batch_id,
        target=target,
        status=status,
        ts=TS,
        ts_source="executed_at",
    )


_Target = Literal["api", "e2e", "fuzz", "performance"]
_SENTINEL = cast(TraceExecution | None, object())


def _row(
    case_id: str,
    case_type: Literal["API", "E2E", "Fuzz", "Performance"],
    *,
    automation_required: bool = True,
    coverage_state: Literal["covered", "uncovered", "not_required"] | None = None,
    presence: Literal["executed", "not_in_current_batch", "target_not_selected"] = "executed",
    latest: TraceExecution | None = _SENTINEL,
    failures: tuple[TraceFailure, ...] = (),
    open_problem_ids: tuple[str, ...] = (),
) -> TraceRow:
    if coverage_state is None:
        coverage_state = "covered" if automation_required else "not_required"
    target = cast(
        _Target,
        {"API": "api", "E2E": "e2e", "Fuzz": "fuzz", "Performance": "performance"}[case_type],
    )
    if latest is _SENTINEL:
        latest_execution = (
            _exec(target=target) if automation_required and coverage_state == "covered" else None
        )
    else:
        latest_execution = latest
    covering = (
        (TraceTestRef(file="tests/api/test_x.py", test_name="test_x"),) if coverage_state == "covered" else ()
    )
    return TraceRow(
        case_id=case_id,
        module="system.dept",
        case_type=case_type,
        automation_required=automation_required,
        covering_tests=covering,
        coverage_state=coverage_state,
        latest_execution=latest_execution,
        freshest_pass=latest_execution if latest_execution and latest_execution.status == "passed" else None,
        presence_in_current_batch=presence,
        failures=failures,
        open_problem_ids=open_problem_ids,
    )


def _projection(
    rows: tuple[TraceRow, ...],
    *,
    phase: Literal["execution", "reconciled"] = "execution",
    gaps: tuple[TraceGapV1 | TraceGapV2, ...] = (),
    sources: tuple[TraceSource, ...] | None = None,
    unmapped: tuple[UnmappedTest, ...] = (),
    integrity: Literal["complete", "degraded", "incomplete"] | None = None,
    change_id: str = "CH-1",
    batch_id: str = "B1",
    schema: Literal["1", "2"] = "1",
) -> TraceProjectionV1 | TraceProjectionV2:
    resolved_integrity = integrity if integrity is not None else derive_trace_integrity(rows, gaps)
    if schema == "2":
        v2_gaps = tuple(
            gap if isinstance(gap, TraceGapV2) else TraceGapV2.model_validate(gap.model_dump())
            for gap in gaps
        )
        return TraceProjectionV2(
            schema_version="2",
            change_id=change_id,
            phase=phase,
            authoritative_batch_id=batch_id,
            sources=sources
            if sources is not None
            else (TraceSource(path="cases/dept/case.yaml", exists=True, sha256="aa"),),
            rows=rows,
            unmapped_tests=unmapped,
            gaps=v2_gaps,
            integrity=resolved_integrity,
        )
    v1_gaps = tuple(
        gap if isinstance(gap, TraceGapV1) else TraceGapV1.model_validate(gap.model_dump()) for gap in gaps
    )
    return TraceProjectionV1(
        schema_version="1",
        change_id=change_id,
        phase=phase,
        authoritative_batch_id=batch_id,
        sources=sources
        if sources is not None
        else (TraceSource(path="cases/dept/case.yaml", exists=True, sha256="aa"),),
        rows=rows,
        unmapped_tests=unmapped,
        gaps=v1_gaps,
        integrity=resolved_integrity,
    )


def _four_layer_rows() -> tuple[TraceRow, ...]:
    return (
        _row("API-1", "API", coverage_state="covered", presence="executed"),
        _row("E2E-1", "E2E", coverage_state="uncovered", presence="not_in_current_batch", latest=None),
        _row("FUZZ-1", "Fuzz", automation_required=False, presence="target_not_selected", latest=None),
        _row(
            "PERF-1",
            "Performance",
            coverage_state="covered",
            presence="executed",
            latest=_exec(target="performance", status="failed"),
        ),
    )


def test_summarize_four_layers_conserves_rows_and_gaps() -> None:
    gaps = (
        TraceGap(code="result_missing", source="execution/api-result.json", target="api"),
        TraceGap(code="manifest_missing", source="execution/execution-manifest.yaml"),
        TraceGap(code="mapped_test_missing_from_tree", source="tests/e2e/test_x.py", target="e2e"),
    )
    projection = _projection(_four_layer_rows(), gaps=gaps)
    summary = summarize_projection_by_layer(projection)
    assert [row.layer for row in summary.layers] == ["api", "e2e", "fuzz", "performance"]
    assert [row.case_type for row in summary.layers] == ["API", "E2E", "Fuzz", "Performance"]
    assert sum(row.total for row in summary.layers) == len(projection.rows)
    assert sum(row.gaps.total for row in summary.layers) + summary.global_gaps.total == len(projection.gaps)
    for aggregate in [*(row.gaps for row in summary.layers), summary.global_gaps]:
        assert aggregate.total == sum(aggregate.by_code.values())


def test_zero_row_layers_are_present() -> None:
    projection = _projection((_row("API-1", "API"),))
    summary = summarize_projection_by_layer(projection)
    assert len(summary.layers) == 4
    by_layer = {layer.layer: layer for layer in summary.layers}
    assert by_layer["api"].total == 1
    for empty in ("e2e", "fuzz", "performance"):
        assert by_layer[empty].total == 0
        assert by_layer[empty].gaps.total == 0
        assert by_layer[empty].gaps.by_code == {}


def test_coverage_current_latest_partitions() -> None:
    rows = (
        _row("API-1", "API", coverage_state="covered", presence="executed", latest=_exec(status="passed")),
        _row("API-2", "API", coverage_state="uncovered", presence="not_in_current_batch", latest=None),
        _row(
            "API-3",
            "API",
            automation_required=False,
            presence="target_not_selected",
            latest=None,
        ),
        _row(
            "API-4",
            "API",
            coverage_state="covered",
            presence="executed",
            latest=_exec(status="skipped"),
        ),
    )
    summary = summarize_projection_by_layer(_projection(rows))
    api = summary.layers[0]
    assert api.total == 4
    assert api.automated == 3
    assert api.covered == 2
    assert api.uncovered == 1
    assert api.not_required == 1
    assert api.current_executed == 2
    assert api.current_not_present == 1
    assert api.target_not_selected == 1
    assert api.latest_passed == 1
    assert api.latest_failed == 0
    assert api.latest_skipped == 1
    assert api.never_run == 2


def test_execution_phase_zeros_failure_and_problem_counts() -> None:
    summary = summarize_projection_by_layer(_projection(_four_layer_rows(), phase="execution"))
    for layer in summary.layers:
        assert layer.failure_rows == 0
        assert layer.failure_links == 0
        assert layer.open_problem_rows == 0
        assert layer.open_problem_links == 0
        assert layer.unique_open_problems == 0


def test_failure_and_problem_row_link_unique_counts() -> None:
    rows = (
        _row(
            "API-1",
            "API",
            failures=(
                TraceFailure(category="assertion", severity="high"),
                TraceFailure(category="timeout", severity="medium"),
            ),
            open_problem_ids=("PROB-1", "PROB-2"),
        ),
        _row(
            "API-2",
            "API",
            failures=(TraceFailure(category="assertion", severity="high"),),
            open_problem_ids=("PROB-1",),
        ),
        _row("API-3", "API"),
    )
    summary = summarize_projection_by_layer(_projection(rows, phase="reconciled"))
    api = summary.layers[0]
    assert api.failure_rows == 2
    assert api.failure_links == 3
    assert api.open_problem_rows == 2
    assert api.open_problem_links == 3
    assert api.unique_open_problems == 2


def test_target_and_global_gap_bucketing_and_lexical_by_code() -> None:
    gaps = (
        TraceGap(code="result_corrupt", source="a", target="api"),
        TraceGap(code="result_missing", source="b", target="api"),
        TraceGap(code="manifest_missing", source="m"),
        TraceGap(code="case_unreadable", source="c"),
    )
    summary = summarize_projection_by_layer(_projection((_row("API-1", "API"),), gaps=gaps))
    assert summary.layers[0].gaps.total == 2
    assert list(summary.layers[0].gaps.by_code) == ["result_corrupt", "result_missing"]
    assert summary.global_gaps.total == 2
    assert list(summary.global_gaps.by_code) == ["case_unreadable", "manifest_missing"]


def test_duplicate_case_id_raises() -> None:
    rows = (_row("API-1", "API"), _row("API-1", "API"))
    # V1 allows duplicate case ids; summary must reject.
    projection = TraceProjectionV1(
        change_id="CH-1",
        phase="execution",
        authoritative_batch_id="B1",
        rows=rows,
        integrity="complete",
    )
    with pytest.raises(TraceLayerSummaryError):
        summarize_projection_by_layer(projection)


def test_unknown_gap_target_raises() -> None:
    projection = TraceProjectionV1(
        change_id="CH-1",
        phase="execution",
        authoritative_batch_id="B1",
        rows=(_row("API-1", "API"),),
        gaps=(TraceGap(code="result_missing", source="x", target="unknown-layer"),),
        integrity="incomplete",
    )
    with pytest.raises(TraceLayerSummaryError):
        summarize_projection_by_layer(projection)


def test_gap_aggregate_rejects_zero_and_mismatched_breakdown() -> None:
    with pytest.raises(ValidationError):
        TraceGapAggregate(total=1, by_code={"result_missing": 0})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TraceGapAggregate(total=2, by_code={"result_missing": 1})


def test_layer_facts_reject_partition_break() -> None:
    with pytest.raises(ValidationError):
        TraceLayerFacts(
            layer="api",
            case_type="API",
            total=2,
            automated=1,
            covered=1,
            uncovered=0,
            not_required=0,  # total mismatch
            current_executed=2,
            current_not_present=0,
            target_not_selected=0,
            latest_passed=2,
            latest_failed=0,
            latest_skipped=0,
            never_run=0,
            failure_rows=0,
            failure_links=0,
            open_problem_rows=0,
            open_problem_links=0,
            unique_open_problems=0,
            gaps=TraceGapAggregate(total=0, by_code={}),
        )


def test_summary_rejects_wrong_layer_order() -> None:
    zero = TraceLayerFacts(
        layer="e2e",
        case_type="E2E",
        total=0,
        automated=0,
        covered=0,
        uncovered=0,
        not_required=0,
        current_executed=0,
        current_not_present=0,
        target_not_selected=0,
        latest_passed=0,
        latest_failed=0,
        latest_skipped=0,
        never_run=0,
        failure_rows=0,
        failure_links=0,
        open_problem_rows=0,
        open_problem_links=0,
        unique_open_problems=0,
        gaps=TraceGapAggregate(total=0, by_code={}),
    )

    # Build three more correct zeros then put e2e first → order fails.
    def z(layer: str, case_type: str) -> TraceLayerFacts:
        return zero.model_copy(update={"layer": layer, "case_type": case_type})

    with pytest.raises(ValidationError):
        TraceLayerFactSummary(
            change_id="CH-1",
            phase="execution",
            authoritative_batch_id="B1",
            source_projection_digest="abc",
            projection_integrity="complete",
            layers=(z("e2e", "E2E"), z("api", "API"), z("fuzz", "Fuzz"), z("performance", "Performance")),
            global_gaps=TraceGapAggregate(total=0, by_code={}),
        )


def test_optimistic_and_pessimistic_integrity_misstatement() -> None:
    rows = (_row("API-1", "API"),)
    with_gap = _projection(
        rows,
        gaps=(TraceGap(code="manifest_missing", source="execution/execution-manifest.yaml"),),
        integrity="complete",  # optimistic
    )
    with pytest.raises(TraceLayerSummaryError, match="integrity"):
        summarize_projection_by_layer(with_gap)

    clean = _projection(rows, gaps=(), integrity="incomplete")  # pessimistic
    with pytest.raises(TraceLayerSummaryError, match="integrity"):
        summarize_projection_by_layer(clean)


def test_derive_trace_integrity_rules() -> None:
    rows = (_row("API-1", "API"),)
    assert derive_trace_integrity((), ()) == "incomplete"
    assert derive_trace_integrity(rows, ()) == "complete"
    assert (
        derive_trace_integrity(
            rows,
            (TraceGap(code="mapped_test_missing_from_tree", source="tests/x.py"),),
        )
        == "degraded"
    )
    assert (
        derive_trace_integrity(
            rows,
            (TraceGap(code="manifest_missing", source="execution/execution-manifest.yaml"),),
        )
        == "incomplete"
    )


def test_canonical_byte_stability_of_gap_by_code_order() -> None:
    gaps_a = (
        TraceGap(code="result_missing", source="b", target="api"),
        TraceGap(code="result_corrupt", source="a", target="api"),
    )
    gaps_b = (
        TraceGap(code="result_corrupt", source="a", target="api"),
        TraceGap(code="result_missing", source="b", target="api"),
    )
    s_a = summarize_projection_by_layer(_projection((_row("API-1", "API"),), gaps=gaps_a))
    s_b = summarize_projection_by_layer(_projection((_row("API-1", "API"),), gaps=gaps_b))
    assert s_a.layers[0].gaps.model_dump(mode="json") == s_b.layers[0].gaps.model_dump(mode="json")
    assert list(s_a.layers[0].gaps.by_code) == ["result_corrupt", "result_missing"]


def valid_phase_pair() -> tuple[TraceProjectionV2, TraceProjectionV2]:
    exec_row = _row("API-1", "API")
    execution = cast(
        TraceProjectionV2,
        _projection(
            (exec_row,),
            phase="execution",
            sources=(
                TraceSource(path="cases/dept/case.yaml", exists=True, sha256="aa"),
                TraceSource(path="tests/#tree-digest", exists=True, sha256="tt"),
            ),
            gaps=(
                TraceGapV2(
                    code="mapped_test_missing_from_tree",
                    source="tests/api/test_x.py",
                    target="api",
                ),
            ),
            schema="2",
        ),
    )
    reconciled = TraceProjectionV2(
        schema_version="2",
        change_id=execution.change_id,
        phase="reconciled",
        authoritative_batch_id=execution.authoritative_batch_id,
        sources=(
            *execution.sources,
            TraceSource(path="inspect/failure-analysis.json", exists=True, sha256="ff"),
            TraceSource(path="issues/snapshot.json", exists=False, sha256=None),
            TraceSource(path="qa/issues/problems.json", exists=False, sha256=None),
        ),
        rows=(
            exec_row.model_copy(
                update={
                    "failures": (TraceFailure(category="assertion", severity="high"),),
                    "open_problem_ids": (),
                }
            ),
        ),
        unmapped_tests=execution.unmapped_tests,
        gaps=(
            *execution.gaps,
            TraceGapV2(code="issues_snapshot_missing", source="issues/snapshot.json"),
        ),
        integrity="incomplete",
    )
    return execution, reconciled


def mutate(reconciled: TraceProjectionV2, mutation: str) -> TraceProjectionV2:
    if mutation == "row_execution_fact":
        row = reconciled.rows[0].model_copy(update={"module": "mutated.module"})
        return reconciled.model_copy(update={"rows": (row,)})
    if mutation == "remove_execution_source":
        kept = tuple(s for s in reconciled.sources if s.path != "tests/#tree-digest")
        return reconciled.model_copy(update={"sources": kept})
    if mutation == "rewrite_execution_gap":
        rewritten = (
            TraceGapV2(
                code="mapped_test_missing_from_tree",
                source="tests/api/test_x.py",
                target="api",
                detail="rewritten",
            ),
            *(g for g in reconciled.gaps if g.code != "mapped_test_missing_from_tree"),
        )
        return reconciled.model_copy(update={"gaps": rewritten})
    if mutation == "change_unmapped_test":
        return reconciled.model_copy(
            update={"unmapped_tests": (UnmappedTest(file="tests/x.py", test_name="test_y"),)}
        )
    if mutation == "unauthorized_added_source":
        return reconciled.model_copy(
            update={
                "sources": (
                    *reconciled.sources,
                    TraceSource(path="secrets/token.txt", exists=True, sha256="evil"),
                )
            }
        )
    if mutation == "unauthorized_added_gap":
        return reconciled.model_copy(
            update={
                "gaps": (
                    *reconciled.gaps,
                    TraceGapV2(code="result_missing", source="execution/api-result.json", target="api"),
                ),
                "integrity": "incomplete",
            }
        )
    if mutation == "improve_integrity":
        # Drop all gaps and claim complete — improves rank from incomplete.
        return reconciled.model_copy(update={"gaps": (), "integrity": "complete"})
    raise AssertionError(mutation)


@pytest.mark.parametrize(
    "mutation",
    [
        "row_execution_fact",
        "remove_execution_source",
        "rewrite_execution_gap",
        "change_unmapped_test",
        "unauthorized_added_source",
        "unauthorized_added_gap",
        "improve_integrity",
    ],
)
def test_phase_pair_rejects_non_monotonic_mutation(mutation: str) -> None:
    execution, reconciled = valid_phase_pair()
    with pytest.raises(TracePhasePairError):
        validate_trace_phase_pair(execution, mutate(reconciled, mutation))


def test_phase_pair_accepts_legal_enrichment_and_approved_sources() -> None:
    execution, reconciled = valid_phase_pair()
    validate_trace_phase_pair(execution, reconciled)

    # Approved authority filenames + safe manifest-prefix sources.
    enriched = reconciled.model_copy(
        update={
            "sources": (
                *reconciled.sources,
                TraceSource(path="inspect/issue-evidence-manifest.json", exists=True, sha256="mm"),
                TraceSource(path="inspect/observations.json", exists=True, sha256="oo"),
                TraceSource(path="inspect/issue-candidates.json", exists=True, sha256="cc"),
                TraceSource(path="inspect/issue-reconcile-status.json", exists=True, sha256="rr"),
                TraceSource(path="issues/events.jsonl", exists=True, sha256="ee"),
                TraceSource(path="qa/issues/events.jsonl", exists=True, sha256="pe"),
                TraceSource(path="execution/api-result.json", exists=True, sha256="ar"),
                TraceSource(path="facts/baseline.json", exists=True, sha256="fb"),
                TraceSource(path="review/plan-review.json", exists=True, sha256="rv"),
                TraceSource(path="healing/fix.json", exists=True, sha256="hh"),
                TraceSource(path="codegen/plan.md", exists=True, sha256="cg"),
            )
        }
    )
    validate_trace_phase_pair(execution, enriched)


def test_phase_pair_accepts_problem_enrichment() -> None:
    execution, reconciled = valid_phase_pair()
    with_problems = reconciled.model_copy(
        update={"rows": (reconciled.rows[0].model_copy(update={"open_problem_ids": ("PROB-9",)}),)}
    )
    validate_trace_phase_pair(execution, with_problems)
