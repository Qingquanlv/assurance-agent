"""Dual-source feedstock acceptance: gap → declaration proposal → C2 reverify.

Closes Dual-source Lane B/C feedstock only. No escape_analysis, no M4 MetricKey
aggregate, no writes to inspect/metrics.json.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.coverage_gaps import (
    COVERAGE_GAPS_REL,
    CoverageGap,
    CoverageGapLocator,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.models.declarations import DeclarationProposal
from assurance_agent.artifacts.models.trace import (
    TraceExecution,
    TraceProjection,
    TraceRow,
)
from assurance_agent.artifacts.models.trace_sufficiency import (
    TraceInsufficientCase,
    TraceSufficiencyFacts,
)
from assurance_agent.evidence.coverage_gaps import (
    build_coverage_gaps,
    count_closed_gaps,
    diff_coverage_gaps,
    gap_identity,
    projection_digest,
)
from assurance_agent.evidence.declarations import (
    build_declaration_proposal_from_counterexample,
    build_declaration_proposal_from_gaps,
    validate_declaration_intake,
    write_declaration_proposal,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.coverage_gaps import (
    TRACE_PROJECTION_REL,
    TRACE_SUFFICIENCY_REL,
    build_coverage_gap_signals,
    build_coverage_gap_signals_operation,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-DUAL-SRC-001"
BATCH_ID = "20260805-160000"
TS = datetime(2026, 8, 5, 16, 0, 0, tzinfo=UTC)

# Explicit non-MetricKey feedstock surfaces only — Scenario C pins this module.
_METRICS_JSON = "inspect/metrics.json"
_METRICS_NIGHTLY_JSON = "inspect/metrics-nightly.json"


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
    batch_id: str = BATCH_ID,
) -> TraceProjection:
    return TraceProjection(
        schema_version="1",
        change_id=CHANGE_ID,
        phase="reconciled",
        authoritative_batch_id=batch_id,
        sources=(),
        rows=rows,
        unmapped_tests=(),
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


def _write_projection(change_dir: Path, projection: TraceProjection) -> None:
    path = change_dir / TRACE_PROJECTION_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, canonical_json_bytes(projection))


def _write_sufficiency(change_dir: Path, facts: TraceSufficiencyFacts) -> None:
    path = change_dir / TRACE_SUFFICIENCY_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, canonical_json_bytes(facts))


def _workspace(project_root: Path, change_dir: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t-dual-src",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree-0",
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t-dual-src",
        node_id="build-coverage-gap-signals",
        graph_id="assurance",
        target="operation:build-coverage-gap-signals",
        input={},
    )


def _context(project_root: Path, change_dir: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        change_id=CHANGE_ID,
        params={},
    )


def _declaration_missing_gap(
    *,
    constraint_key: str = "entities.dept.constraints.name_unique",
    batch_id: str = BATCH_ID,
    digest: str = "sha256:projdeclmissing",
) -> CoverageGap:
    """Declaration-layer feedstock standing in for a missing obligation declaration."""
    return CoverageGap(
        kind="constraint_without_property",
        locator=CoverageGapLocator(constraint_key=constraint_key),
        layer="declaration",
        batch_id=batch_id,
        evidence_refs=(digest,),
    )


def _gaps_doc(*gaps: CoverageGap, digest: str = "sha256:projdeclmissing") -> CoverageGapsDocument:
    return CoverageGapsDocument(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        projection_digest=digest,
        gaps=gaps,
    )


# ---------------------------------------------------------------------------
# Scenario A — Lane B gap signalize
# ---------------------------------------------------------------------------


def test_scenario_a_lane_b_gap_signalize_writes_typed_coverage_gaps(tmp_path: Path) -> None:
    project = tmp_path / "project"
    change = project / "qa" / "changes" / CHANGE_ID
    project.mkdir()
    change.mkdir(parents=True)
    write_aa_config(project)

    projection = _projection(
        _row("TC_COVERED", coverage_state="covered", latest=_execution()),
        _row("TC_UNCOVERED_Z", coverage_state="uncovered"),
        _row("TC_UNCOVERED_A", coverage_state="uncovered"),
        _row("TC_STALE", coverage_state="covered", latest=_execution()),
    )
    facts = _sufficiency(
        TraceInsufficientCase(case_id="TC_STALE", reason_codes=("execution_stale",)),
    )
    _write_projection(change, projection)
    _write_sufficiency(change, facts)

    # Pure fold path.
    folded = build_coverage_gaps(
        projection,
        facts,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
    )
    expected_digest = projection_digest(projection)
    assert folded.projection_digest == expected_digest
    assert folded.projection_digest == sha256_bytes(canonical_json_bytes(projection))
    assert [g.kind for g in folded.gaps] == [
        "uncovered_required_case",
        "uncovered_required_case",
        "stale_required_case",
    ]
    assert [g.locator.case_id for g in folded.gaps] == [
        "TC_UNCOVERED_A",
        "TC_UNCOVERED_Z",
        "TC_STALE",
    ]
    assert all(g.layer == "execution" for g in folded.gaps)
    assert all(g.evidence_refs == (expected_digest,) for g in folded.gaps)

    # Operation write path.
    assert "operation:build-coverage-gap-signals" in default_operations()
    result = build_coverage_gap_signals_operation(
        _task(),
        _workspace(project, change),
        _context(project, change),
    )
    assert result.status == "succeeded"
    out = change / COVERAGE_GAPS_REL
    assert out.is_file()
    loaded = CoverageGapsDocument.model_validate_json(out.read_text(encoding="utf-8"))
    assert loaded == folded
    assert loaded.projection_digest == expected_digest
    assert isinstance(result.value, dict)
    assert result.value["projection_digest"] == expected_digest
    assert result.value["gap_count"] == 3

    # Idempotent rewrite stays deterministic.
    again = build_coverage_gap_signals(change_dir=change, change_id=CHANGE_ID)
    assert again == folded


# ---------------------------------------------------------------------------
# Scenario B — Lane C declaration fork
# ---------------------------------------------------------------------------


def test_scenario_b_lane_c_declaration_fork_writes_proposal_and_passes_intake(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    # Declaration-missing style gap → builder path.
    doc = _gaps_doc(_declaration_missing_gap())
    proposal_a = build_declaration_proposal_from_gaps(doc)
    proposal_b = build_declaration_proposal_from_gaps(doc)
    assert proposal_a == proposal_b

    # CE identity path (digest only — never CE body).
    ce_proposal = build_declaration_proposal_from_counterexample(
        counterexample_id="CE-DUAL-001",
        digest="sha256:cefeedstockdigest",
        change_id=CHANGE_ID,
        path="discovery/counterexamples/CE-DUAL-001.yaml",
    )

    path = write_declaration_proposal(project, proposal_a)
    assert path == (
        project / "qa" / "improvements" / "declarations" / f"{proposal_a.improvement_id}.proposal.yaml"
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    reloaded = DeclarationProposal.model_validate(raw)
    assert reloaded == proposal_a

    for proposal in (proposal_a, ce_proposal, reloaded):
        verdict = validate_declaration_intake(proposal)
        assert verdict.ok is True
        assert verdict.errors == ()
        assert all(draft.status == "draft" for draft in proposal.case_drafts)
        assert all(ref.digest.strip() for ref in proposal.evidence_refs)
        assert proposal.fingerprint
        assert proposal.improvement_id.startswith("IMP-")

    # Digests stable across rebuild / reload.
    assert proposal_a.evidence_refs[0].digest == "sha256:projdeclmissing"
    assert proposal_a.fingerprint == reloaded.fingerprint
    assert ce_proposal.evidence_refs[0].digest == "sha256:cefeedstockdigest"


# ---------------------------------------------------------------------------
# Scenario C — reverify / C2 feedstock (NOT MetricKey)
# ---------------------------------------------------------------------------


def test_scenario_c_reverify_c2_feedstock_closes_and_reopens_without_metrics(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    change = project / "qa" / "changes" / CHANGE_ID
    project.mkdir()
    change.mkdir(parents=True)

    prev = build_coverage_gaps(
        _projection(_row("TC_X", coverage_state="uncovered"), batch_id="batch-1"),
        None,
        change_id=CHANGE_ID,
        batch_id="batch-1",
    )
    assert len(prev.gaps) == 1
    closed_identity = gap_identity(prev.gaps[0])

    current_closed = build_coverage_gaps(
        _projection(batch_id="batch-2"),
        None,
        change_id=CHANGE_ID,
        batch_id="batch-2",
    )
    assert count_closed_gaps(prev, current_closed) == 1
    closed_diff = diff_coverage_gaps(prev, current_closed)
    assert closed_diff.closed == (closed_identity,)
    assert closed_diff.opened == ()
    assert closed_diff.reopened == ()

    # Reopen: identity was historically closed, then reappears.
    reopened_doc = build_coverage_gaps(
        _projection(_row("TC_X", coverage_state="uncovered"), batch_id="batch-3"),
        None,
        change_id=CHANGE_ID,
        batch_id="batch-3",
    )
    reopen_diff = diff_coverage_gaps(
        current_closed,
        reopened_doc,
        historically_closed=(closed_identity,),
    )
    assert reopen_diff.reopened == (closed_identity,)
    assert reopen_diff.opened == ()
    assert reopen_diff.closed == ()

    # No M4 aggregate surfaces written by this feedstock loop.
    assert not (change / _METRICS_JSON).exists()
    assert not (change / _METRICS_NIGHTLY_JSON).exists()
    assert not (project / _METRICS_JSON).exists()
    assert not (project / _METRICS_NIGHTLY_JSON).exists()


def test_scenario_c_module_does_not_use_metric_key() -> None:
    """This acceptance module must not introduce MetricKey / MetricsDocument usage."""
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)
            for alias in node.names:
                imported_names.add(alias.name)
                if alias.asname:
                    imported_names.add(alias.asname)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
                imported_names.add(alias.name.split(".")[0])
                if alias.asname:
                    imported_names.add(alias.asname)

    assert "MetricKey" not in imported_names
    assert "MetricsDocument" not in imported_names
    assert "MetricEntry" not in imported_names
    assert "assurance_agent.artifacts.models.metrics" not in imported_modules
    assert not any(mod.startswith("assurance_agent.workflow.metrics.pr_metrics") for mod in imported_modules)
    assert not any(mod.endswith(".aggregate") and "metrics" in mod for mod in imported_modules)
    # Feedstock helpers are the only C2 surface exercised here.
    assert "count_closed_gaps" in source
    assert "diff_coverage_gaps" in source


# ---------------------------------------------------------------------------
# Scenario D — Lane B vs Lane C fork honesty
# ---------------------------------------------------------------------------


def test_scenario_d_execution_gap_stays_on_coverage_gaps_not_declaration(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    change = project / "qa" / "changes" / CHANGE_ID
    project.mkdir()
    change.mkdir(parents=True)
    write_aa_config(project)

    projection = _projection(_row("TC_EXEC_UNCOVERED", coverage_state="uncovered"))
    _write_projection(change, projection)

    doc = build_coverage_gap_signals(change_dir=change, change_id=CHANGE_ID)
    assert len(doc.gaps) == 1
    assert doc.gaps[0].kind == "uncovered_required_case"
    assert doc.gaps[0].layer == "execution"
    assert (change / COVERAGE_GAPS_REL).is_file()

    # Same inputs cannot fork into a Lane C declaration proposal.
    with pytest.raises(ValueError, match="declaration"):
        build_declaration_proposal_from_gaps(doc)

    decls = project / "qa" / "improvements" / "declarations"
    assert not decls.exists() or not any(decls.glob("*.proposal.yaml"))


def test_scenario_d_declaration_missing_forks_to_proposal_not_test_improvement(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    doc = _gaps_doc(_declaration_missing_gap())
    proposal = build_declaration_proposal_from_gaps(doc)
    path = write_declaration_proposal(project, proposal)

    assert path.is_file()
    assert "declarations" in path.as_posix()
    assert path.name.endswith(".proposal.yaml")

    # Lane C must not open a test_improvement ledger entry.
    improvements = project / "qa" / "improvements"
    ledger_candidates = [
        *improvements.glob("*.json"),
        *improvements.glob("*.yaml"),
        *improvements.glob("ledger*"),
        *improvements.glob("**/test_improvement*"),
        *improvements.glob("**/improvement-ledger*"),
    ]
    assert ledger_candidates == []
    assert list(improvements.rglob("*.proposal.yaml")) == [path]

    # Coverage-gaps.json is not the Lane C write surface.
    assert not (project / COVERAGE_GAPS_REL).exists()

    dumped = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert dumped["status"] == "draft"
    assert all(entry["status"] == "draft" for entry in dumped["case_drafts"])
    assert "test_improvement" not in yaml.safe_dump(dumped)
