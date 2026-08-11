"""operation:build-coverage-gap-signals — fold projection → inspect/coverage-gaps.json."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.coverage_gaps import COVERAGE_GAPS_REL, CoverageGapsDocument
from assurance_agent.artifacts.models.metrics import MetricCollectionGap, MetricScope
from assurance_agent.artifacts.models.pr_metric_evidence import (
    AuthMatrixCellResult,
    AuthMatrixEvidence,
    ConstraintCoverageEvidence,
    JourneyCoverageEvidence,
    JourneyCoverageItem,
)
from assurance_agent.artifacts.models.trace import TraceProjection, TraceRow, UnmappedTest
from assurance_agent.artifacts.models.trace_sufficiency import (
    TraceInsufficientCase,
    TraceSufficiencyFacts,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.handlers.trace_projection import TRACE_SUFFICIENCY_REL
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.auth_matrix import AUTH_MATRIX_REL
from assurance_agent.workflow.metrics.batch_io import batch_runs_dir
from assurance_agent.workflow.metrics.constraint_coverage import CONSTRAINT_COVERAGE_REL
from assurance_agent.workflow.metrics.coverage_gaps import (
    TRACE_PROJECTION_REL,
    build_coverage_gap_signals_operation,
)
from assurance_agent.workflow.metrics.journey_coverage import JOURNEY_COVERAGE_REL
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-GAP-OP-001"
BATCH_ID = "20260805-150000"
TS = datetime(2026, 8, 5, 15, 0, 0, tzinfo=UTC)


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="build-coverage-gap-signals",
        graph_id="assurance",
        target="operation:build-coverage-gap-signals",
        input={},
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def _write_projection(change_dir: Path) -> TraceProjection:
    projection = TraceProjection(
        schema_version="1",
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id=BATCH_ID,
        sources=(),
        rows=(
            TraceRow(
                case_id="TC_UNCOVERED",
                module="system.api",
                case_type="API",
                automation_required=True,
                assertions=("ok",),
                covering_tests=(),
                coverage_state="uncovered",
                latest_execution=None,
                freshest_pass=None,
                presence_in_current_batch="not_in_current_batch",
                atemporal_kinds_present=(),
                open_problem_ids=(),
            ),
        ),
        unmapped_tests=(UnmappedTest(file="tests/api/orphan.py", test_name="test_orphan"),),
        gaps=(),
        integrity="complete",
    )
    path = change_dir / TRACE_PROJECTION_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, canonical_json_bytes(projection))
    return projection


def _write_sufficiency(change_dir: Path) -> None:
    facts = TraceSufficiencyFacts(
        schema_version="1",
        change_id=CHANGE_ID,
        authoritative_batch_id=BATCH_ID,
        policy_digest="sha256:policy",
        as_of=TS,
        integrity="complete",
        integrity_blocks_routing=False,
        sufficient=False,
        has_open_problems=False,
        error_code=None,
        insufficient_cases=(TraceInsufficientCase(case_id="TC_UNCOVERED", reason_codes=("not_covered",)),),
        gap_codes=(),
    )
    path = change_dir / TRACE_SUFFICIENCY_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, canonical_json_bytes(facts))


def test_operation_is_registered_under_exact_target() -> None:
    ops = default_operations()
    assert "operation:build-coverage-gap-signals" in ops
    assert ops["operation:build-coverage-gap-signals"] is build_coverage_gap_signals_operation


def test_operation_writes_coverage_gaps_once(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    _write_projection(change_dir)
    _write_sufficiency(change_dir)

    result = build_coverage_gap_signals_operation(_task(), _workspace(project_root), _context(project_root))
    assert result.status == "succeeded"
    out = change_dir / COVERAGE_GAPS_REL
    assert out.is_file()
    doc = CoverageGapsDocument.model_validate_json(out.read_text(encoding="utf-8"))
    assert doc.change_id == CHANGE_ID
    assert doc.batch_id == BATCH_ID
    assert {g.kind for g in doc.gaps} == {"uncovered_required_case", "unmapped_test_cluster"}


def test_operation_folds_batch_a2_a3_evidence_into_extension_gaps(tmp_path: Path) -> None:
    """A numeric A2/A3 shortfall must reach dual-source case design as a gap.

    Both extension kinds were wired in the fold but never fed: nothing wrote
    ``CoverageGapFeedstock``, so a batch with ``constraint_coverage == 0.0`` or
    unasserted auth cells produced zero typed gaps for it.
    """
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    _write_projection(change_dir)

    batch = batch_runs_dir(change_dir, BATCH_ID)
    batch.mkdir(parents=True, exist_ok=True)
    constraint_evidence = ConstraintCoverageEvidence(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        declared=MetricScope.of(total=1, covered=0, uncovered=("entities.dept.constraints.name_unique",)),
        touched=MetricScope.of(total=1, covered=0, uncovered=("entities.dept.constraints.name_unique",)),
        value=0.0,
    )
    atomic_write_bytes(batch / CONSTRAINT_COVERAGE_REL, canonical_json_bytes(constraint_evidence))
    auth_evidence = AuthMatrixEvidence(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        declared=MetricScope.of(total=1, covered=0),
        cells=(
            AuthMatrixCellResult(
                cell_id="dept_list_guest",
                route="/api/v1/dept/list",
                method="GET",
                token="guest_token",
                expected="deny",
                allowed_status_codes=(403,),
                asserted=False,
                outcome="missing",
            ),
        ),
    )
    atomic_write_bytes(batch / AUTH_MATRIX_REL, canonical_json_bytes(auth_evidence))

    result = build_coverage_gap_signals_operation(_task(), _workspace(project_root), _context(project_root))
    assert result.status == "succeeded"
    doc = CoverageGapsDocument.model_validate_json(
        (change_dir / COVERAGE_GAPS_REL).read_text(encoding="utf-8")
    )
    kinds_by_locator = {(g.kind, g.locator.constraint_key, g.locator.cell) for g in doc.gaps}
    assert ("constraint_without_property", "entities.dept.constraints.name_unique", None) in kinds_by_locator
    assert ("matrix_cell_unasserted", None, "dept_list_guest") in kinds_by_locator


def test_operation_projects_weak_journey_oracle_into_repairable_case_gap(tmp_path: Path) -> None:
    """A4 shortfall from a weak oracle must not leave coverage repair with zero work."""
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    projection = TraceProjection(
        schema_version="1",
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id=BATCH_ID,
        sources=(),
        rows=(
            TraceRow(
                case_id="TC_WEAK_ORACLE",
                module="system.role",
                case_type="E2E",
                automation_required=True,
                assertions=("role is created",),
                covering_tests=(),
                coverage_state="covered",
                latest_execution=None,
                freshest_pass=None,
                presence_in_current_batch="executed",
                atemporal_kinds_present=(),
                open_problem_ids=(),
            ),
        ),
        unmapped_tests=(),
        gaps=(),
        integrity="complete",
    )
    atomic_write_bytes(change_dir / TRACE_PROJECTION_REL, canonical_json_bytes(projection))

    batch = batch_runs_dir(change_dir, BATCH_ID)
    batch.mkdir(parents=True, exist_ok=True)
    journey = JourneyCoverageEvidence(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        declared=MetricScope.of(total=1, covered=0, uncovered=("admin_creates_role",)),
        touched=MetricScope.of(total=1, covered=0, uncovered=("admin_creates_role",)),
        value=0.0,
        items=(
            JourneyCoverageItem(
                journey_key="admin_creates_role",
                case_ids=("TC_WEAK_ORACLE",),
                executed_case_ids=("TC_WEAK_ORACLE",),
                covered=False,
                status="weak_oracle",
            ),
        ),
    )
    atomic_write_bytes(batch / JOURNEY_COVERAGE_REL, canonical_json_bytes(journey))

    result = build_coverage_gap_signals_operation(_task(), _workspace(project_root), _context(project_root))
    assert result.status == "succeeded"
    doc = CoverageGapsDocument.model_validate_json(
        (change_dir / COVERAGE_GAPS_REL).read_text(encoding="utf-8")
    )
    assert {(gap.kind, gap.locator.case_id) for gap in doc.gaps} == {
        ("uncovered_required_case", "TC_WEAK_ORACLE")
    }


def test_operation_drops_extension_feedstock_when_batch_evidence_has_collection_gaps(
    tmp_path: Path,
) -> None:
    """A collection-failed batch file must not fabricate an extension gap."""
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)
    _write_projection(change_dir)

    batch = batch_runs_dir(change_dir, BATCH_ID)
    batch.mkdir(parents=True, exist_ok=True)
    corrupt_evidence = ConstraintCoverageEvidence(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        declared=None,
        collection_gaps=(
            MetricCollectionGap(code="artifact_corrupt", metric="constraint_coverage", detail="bad"),
        ),
    )
    atomic_write_bytes(batch / CONSTRAINT_COVERAGE_REL, canonical_json_bytes(corrupt_evidence))

    result = build_coverage_gap_signals_operation(_task(), _workspace(project_root), _context(project_root))
    assert result.status == "succeeded"
    doc = CoverageGapsDocument.model_validate_json(
        (change_dir / COVERAGE_GAPS_REL).read_text(encoding="utf-8")
    )
    assert "constraint_without_property" not in {g.kind for g in doc.gaps}


def test_missing_projection_is_invalid_input(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True, exist_ok=True)

    result = build_coverage_gap_signals_operation(_task(), _workspace(project_root), _context(project_root))
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
