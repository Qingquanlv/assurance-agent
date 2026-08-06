"""Pure fold: TraceProjection + sufficiency → typed coverage gaps."""

from __future__ import annotations

from datetime import UTC, datetime

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.coverage_gaps import CoverageGapFeedstock
from assurance_agent.artifacts.models.trace import (
    TraceExecution,
    TraceProjection,
    TraceRow,
    UnmappedTest,
)
from assurance_agent.artifacts.models.trace_sufficiency import (
    TraceInsufficientCase,
    TraceSufficiencyFacts,
)
from assurance_agent.evidence.coverage_gaps import (
    PHASE1_ACTIVE_KINDS,
    PHASE1_EXTENSION_KINDS,
    build_coverage_gaps,
    count_closed_gaps,
    diff_coverage_gaps,
    gap_identity,
)


CHANGE_ID = "CH-GAP-FOLD-001"
BATCH_ID = "20260805-120000"
TS = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


def _execution(*, status: str = "passed") -> TraceExecution:
    return TraceExecution(
        batch_id=BATCH_ID,
        target="api",
        status=status,  # type: ignore[arg-type]
        ts=TS,
        ts_source="executed_at",
    )


def _row(
    case_id: str,
    *,
    coverage_state: str = "covered",
    automation_required: bool = True,
    latest: TraceExecution | None = None,
) -> TraceRow:
    return TraceRow(
        case_id=case_id,
        module="system.api",
        case_type="API",
        automation_required=automation_required,
        assertions=("ok",),
        covering_tests=(),
        coverage_state=coverage_state,  # type: ignore[arg-type]
        latest_execution=latest,
        freshest_pass=latest if latest and latest.status == "passed" else None,
        presence_in_current_batch="executed" if latest else "not_in_current_batch",
        atemporal_kinds_present=(),
        open_problem_ids=(),
    )


def _projection(
    *rows: TraceRow,
    unmapped: tuple[UnmappedTest, ...] = (),
) -> TraceProjection:
    return TraceProjection(
        schema_version="1",
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id=BATCH_ID,
        sources=(),
        rows=rows,
        unmapped_tests=unmapped,
        gaps=(),
        integrity="complete",
    )


def _sufficiency(*cases: TraceInsufficientCase) -> TraceSufficiencyFacts:
    return TraceSufficiencyFacts(
        schema_version="1",
        change_id=CHANGE_ID,
        authoritative_batch_id=BATCH_ID,
        policy_digest="sha256:policy",
        as_of=TS,
        integrity="complete",
        integrity_blocks_routing=False,
        sufficient=len(cases) == 0,
        has_open_problems=False,
        error_code=None,
        insufficient_cases=cases,
        gap_codes=(),
    )


def test_phase1_active_vs_extension_kinds_are_documented() -> None:
    assert PHASE1_ACTIVE_KINDS == frozenset(
        {
            "uncovered_required_case",
            "stale_required_case",
            "unmapped_test_cluster",
        }
    )
    assert PHASE1_EXTENSION_KINDS == frozenset(
        {
            "constraint_without_property",
            "matrix_cell_unasserted",
        }
    )
    assert PHASE1_ACTIVE_KINDS.isdisjoint(PHASE1_EXTENSION_KINDS)


def test_uncovered_required_case_from_projection() -> None:
    projection = _projection(
        _row("TC_COVERED", coverage_state="covered", latest=_execution()),
        _row("TC_UNCOVERED", coverage_state="uncovered"),
        _row("TC_MANUAL", coverage_state="not_required", automation_required=False),
    )
    doc = build_coverage_gaps(projection, _sufficiency(), change_id=CHANGE_ID, batch_id=BATCH_ID)
    assert [g.kind for g in doc.gaps] == ["uncovered_required_case"]
    assert doc.gaps[0].locator.case_id == "TC_UNCOVERED"
    assert doc.gaps[0].layer == "execution"
    assert doc.projection_digest == sha256_bytes(canonical_json_bytes(projection))
    assert doc.gaps[0].evidence_refs == (doc.projection_digest,)


def test_stale_required_case_from_sufficiency_reason_codes() -> None:
    projection = _projection(_row("TC_STALE", coverage_state="covered", latest=_execution()))
    facts = _sufficiency(
        TraceInsufficientCase(case_id="TC_STALE", reason_codes=("execution_stale",)),
    )
    doc = build_coverage_gaps(projection, facts, change_id=CHANGE_ID, batch_id=BATCH_ID)
    assert [g.kind for g in doc.gaps] == ["stale_required_case"]
    assert doc.gaps[0].locator.case_id == "TC_STALE"


def test_pass_stale_also_maps_to_stale_required_case() -> None:
    projection = _projection(_row("TC_PASS_STALE", coverage_state="covered", latest=_execution()))
    facts = _sufficiency(
        TraceInsufficientCase(case_id="TC_PASS_STALE", reason_codes=("pass_stale",)),
    )
    doc = build_coverage_gaps(projection, facts, change_id=CHANGE_ID, batch_id=BATCH_ID)
    assert [g.kind for g in doc.gaps] == ["stale_required_case"]


def test_not_covered_sufficiency_does_not_duplicate_when_projection_already_uncovered() -> None:
    """Projection coverage_state owns uncovered; sufficiency not_covered is not a second gap."""
    projection = _projection(_row("TC_UNCOVERED", coverage_state="uncovered"))
    facts = _sufficiency(
        TraceInsufficientCase(case_id="TC_UNCOVERED", reason_codes=("not_covered",)),
    )
    doc = build_coverage_gaps(projection, facts, change_id=CHANGE_ID, batch_id=BATCH_ID)
    assert [g.kind for g in doc.gaps] == ["uncovered_required_case"]


def test_unmapped_tests_fold_to_one_cluster_gap_per_file() -> None:
    projection = _projection(
        unmapped=(
            UnmappedTest(file="tests/api/b.py", test_name="test_z"),
            UnmappedTest(file="tests/api/a.py", test_name="test_y"),
            UnmappedTest(file="tests/api/a.py", test_name="test_x"),
        )
    )
    doc = build_coverage_gaps(projection, None, change_id=CHANGE_ID, batch_id=BATCH_ID)
    assert [g.kind for g in doc.gaps] == ["unmapped_test_cluster", "unmapped_test_cluster"]
    assert [g.locator.cluster_key for g in doc.gaps] == ["tests/api/a.py", "tests/api/b.py"]


def test_extension_kinds_emit_only_when_feedstock_present() -> None:
    projection = _projection()
    empty = build_coverage_gaps(projection, None, change_id=CHANGE_ID, batch_id=BATCH_ID)
    assert empty.gaps == ()

    feedstock = CoverageGapFeedstock(
        constraints_without_property=("entities.dept.constraints.name_unique",),
        matrix_cells_unasserted=("auth_matrix.admin_get_users",),
    )
    doc = build_coverage_gaps(
        projection,
        None,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        feedstock=feedstock,
    )
    kinds = [g.kind for g in doc.gaps]
    assert kinds == ["constraint_without_property", "matrix_cell_unasserted"]
    assert doc.gaps[0].locator.constraint_key == "entities.dept.constraints.name_unique"
    assert doc.gaps[1].locator.cell == "auth_matrix.admin_get_users"


def test_never_invents_extension_kinds_from_bare_projection() -> None:
    projection = _projection(_row("TC_OK", latest=_execution()))
    doc = build_coverage_gaps(projection, _sufficiency(), change_id=CHANGE_ID, batch_id=BATCH_ID)
    assert not any(g.kind in PHASE1_EXTENSION_KINDS for g in doc.gaps)


def test_unjudged_sufficiency_skips_stale() -> None:
    projection = _projection(_row("TC_X", coverage_state="covered", latest=_execution()))
    unjudged = TraceSufficiencyFacts(
        schema_version="1",
        change_id=CHANGE_ID,
        authoritative_batch_id=BATCH_ID,
        policy_digest=None,
        as_of=None,
        integrity="incomplete",
        integrity_blocks_routing=True,
        sufficient=False,
        has_open_problems=False,
        error_code="evidence_projection_missing",
        insufficient_cases=(),
        gap_codes=(),
    )
    doc = build_coverage_gaps(projection, unjudged, change_id=CHANGE_ID, batch_id=BATCH_ID)
    assert doc.gaps == ()


def test_gaps_sorted_deterministically_across_kinds() -> None:
    facts = _sufficiency(
        TraceInsufficientCase(case_id="TC_STALE", reason_codes=("execution_stale",)),
    )
    projection = _projection(
        _row("TC_Z", coverage_state="uncovered"),
        _row("TC_A", coverage_state="uncovered"),
        _row("TC_STALE", coverage_state="covered", latest=_execution()),
        unmapped=(UnmappedTest(file="tests/x.py", test_name="test_1"),),
    )
    doc = build_coverage_gaps(projection, facts, change_id=CHANGE_ID, batch_id=BATCH_ID)
    assert [(g.kind, g.locator.case_id or g.locator.cluster_key) for g in doc.gaps] == [
        ("uncovered_required_case", "TC_A"),
        ("uncovered_required_case", "TC_Z"),
        ("stale_required_case", "TC_STALE"),
        ("unmapped_test_cluster", "tests/x.py"),
    ]


def test_count_closed_gaps_and_diff_are_pure_feedstock_not_metric_key() -> None:
    projection = _projection(_row("TC_A", coverage_state="uncovered"))
    prev = build_coverage_gaps(projection, None, change_id=CHANGE_ID, batch_id="batch-1")
    current_proj = _projection()  # gap closed
    current = build_coverage_gaps(current_proj, None, change_id=CHANGE_ID, batch_id="batch-2")
    assert count_closed_gaps(prev, current) == 1
    diff = diff_coverage_gaps(prev, current)
    assert diff.closed == (gap_identity(prev.gaps[0]),)
    assert diff.opened == ()
    assert diff.reopened == ()
