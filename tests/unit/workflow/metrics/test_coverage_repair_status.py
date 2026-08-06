"""Task 5: coverage-repair allocate + record-status (budget ledger + baseline freeze)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.coverage_gaps import CoverageGapLocator
from assurance_agent.artifacts.models.coverage_repair import (
    COVERAGE_REPAIR_BASELINE_REL,
    COVERAGE_REPAIR_BRIEF_REL,
    COVERAGE_REPAIR_STATUS_REL,
    CoverageRepairBaseline,
    CoverageRepairBrief,
    CoverageRepairStatus,
    CoverageRepairStatusValue,
    DeferredItem,
    RepairItem,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.execution.tree_hash import hash_test_tree
from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.coverage_repair import (
    allocate_coverage_repair_attempt_operation,
    record_coverage_repair_status_operation,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-REPAIR-001"
CASE_ID = "TC_API_001"
BATCH_ID = "20260806-120000"
CASE_REL = f"qa/changes/{CHANGE_ID}/cases/system/api/case.yaml"
BRIEFED_TEST = f"tests/api/test_{CASE_ID.lower()}.py"
PRODUCT_FILE = "app/service.py"


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _allocate_task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="allocate-coverage-repair-attempt",
        graph_id="coverage-repair",
        target="operation:allocate-coverage-repair-attempt",
        input={"with": {}},
    )


def _record_task(status: str) -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t1",
        node_id="record-coverage-repair-status",
        graph_id="coverage-repair",
        target="operation:record-coverage-repair-status",
        input={"with": {"status": status}},
    )


def _context(project_root: Path) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={},
    )


def _seed_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "proj"
    write_aa_config(project_root)
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    (project_root / "qa" / "cases").mkdir(parents=True)
    (project_root / "app").mkdir(parents=True)
    (project_root / "tests" / "api").mkdir(parents=True)
    (change_dir / "cases" / "system" / "api").mkdir(parents=True)
    (change_dir / "cases" / "system" / "api" / "case.yaml").write_text(
        "schema_version: '1.0'\nadded: []\n",
        encoding="utf-8",
    )
    (project_root / PRODUCT_FILE).write_text("def ok():\n    return 1\n", encoding="utf-8")
    (project_root / BRIEFED_TEST).write_text(
        f"def test_{CASE_ID.lower()}__happy():\n    assert True\n",
        encoding="utf-8",
    )
    (project_root / ".aa" / "data-knowledge.yaml").write_text("entities: []\n", encoding="utf-8")
    return project_root, change_dir


def _write_brief(
    change_dir: Path,
    *,
    eligible: bool = True,
    batch_id: str | None = BATCH_ID,
    deferred: tuple[DeferredItem, ...] | None = None,
) -> CoverageRepairBrief:
    deferred_items = (
        deferred
        if deferred is not None
        else (
            DeferredItem(
                kind="uncovered_required_case",
                locator=CoverageGapLocator(case_id="TC_MISSING"),
                reason="declaration_layer",
            ),
        )
    )
    repair_items: tuple[RepairItem, ...] = ()
    if eligible:
        repair_items = (
            RepairItem(
                kind="uncovered_required_case",
                locator=CoverageGapLocator(case_id=CASE_ID),
                metric="constraint_coverage",
                hint="bind a test",
            ),
        )
    brief = CoverageRepairBrief(
        change_id=CHANGE_ID,
        batch_id=batch_id,
        probe_verdict="needs_human" if eligible else "pass",
        eligible=eligible,
        repair_items=repair_items,
        deferred_to_intake=deferred_items,
        computed_at=datetime.now(tz=UTC),
    )
    out = change_dir / COVERAGE_REPAIR_BRIEF_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(brief))
    return brief


def _read_status(change_dir: Path) -> CoverageRepairStatus:
    return CoverageRepairStatus.model_validate_json(
        (change_dir / COVERAGE_REPAIR_STATUS_REL).read_text(encoding="utf-8")
    )


def _read_baseline(change_dir: Path) -> CoverageRepairBaseline:
    return CoverageRepairBaseline.model_validate_json(
        (change_dir / COVERAGE_REPAIR_BASELINE_REL).read_text(encoding="utf-8")
    )


# ── Step 1: allocation + status bookkeeping ──────────────────────────────────


def test_first_allocate_sets_attempts_in_progress_and_batch(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir)

    result = allocate_coverage_repair_attempt_operation(
        _allocate_task(), _workspace(project_root), _context(project_root)
    )
    assert result.status == "succeeded"
    status = _read_status(change_dir)
    assert status.attempts_used == 1
    assert status.status == "in_progress"
    assert status.last_batch_id == BATCH_ID
    assert status.deferred_to_intake
    assert status.deferred_to_intake[0].kind == "uncovered_required_case"
    assert status.deferred_to_intake[0].reason == "declaration_layer"


def test_second_allocate_increments_attempts_monotonically(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir)
    workspace = _workspace(project_root)
    context = _context(project_root)

    first = allocate_coverage_repair_attempt_operation(_allocate_task(), workspace, context)
    second = allocate_coverage_repair_attempt_operation(_allocate_task(), workspace, context)
    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert _read_status(change_dir).attempts_used == 2


def test_allocate_ineligible_brief_is_invalid_input_and_writes_nothing(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir, eligible=False, batch_id=BATCH_ID)

    result = allocate_coverage_repair_attempt_operation(
        _allocate_task(), _workspace(project_root), _context(project_root)
    )
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert not (change_dir / COVERAGE_REPAIR_STATUS_REL).is_file()
    assert not (change_dir / COVERAGE_REPAIR_BASELINE_REL).is_file()


def test_allocate_missing_brief_is_invalid_input(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)

    result = allocate_coverage_repair_attempt_operation(
        _allocate_task(), _workspace(project_root), _context(project_root)
    )
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert not (change_dir / COVERAGE_REPAIR_STATUS_REL).is_file()


def test_record_status_exhausted_preserves_attempts_and_deferred(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir)
    allocate_coverage_repair_attempt_operation(
        _allocate_task(), _workspace(project_root), _context(project_root)
    )
    prior = _read_status(change_dir)

    result = record_coverage_repair_status_operation(
        _record_task("exhausted"), _workspace(project_root), _context(project_root)
    )
    assert result.status == "succeeded"
    status = _read_status(change_dir)
    assert status.status == "exhausted"
    assert status.attempts_used == prior.attempts_used
    assert status.deferred_to_intake == prior.deferred_to_intake


def test_record_status_without_prior_status_still_writes(tmp_path: Path) -> None:
    # not_eligible path never runs allocate.
    project_root, change_dir = _seed_project(tmp_path)

    result = record_coverage_repair_status_operation(
        _record_task("not_eligible"), _workspace(project_root), _context(project_root)
    )
    assert result.status == "succeeded"
    status = _read_status(change_dir)
    assert status.status == "not_eligible"
    assert status.attempts_used == 0
    assert status.change_id == CHANGE_ID


def test_record_status_rejects_unknown_status(tmp_path: Path) -> None:
    project_root, _change_dir = _seed_project(tmp_path)
    allowed = set(get_args(CoverageRepairStatusValue))
    assert "bogus" not in allowed

    result = record_coverage_repair_status_operation(
        _record_task("bogus"), _workspace(project_root), _context(project_root)
    )
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"


# ── Step 1b: baseline freezing (design v4-2 / v4-5 / v4-6) ───────────────────


def test_allocate_freezes_baseline_matching_attempts(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir)

    result = allocate_coverage_repair_attempt_operation(
        _allocate_task(), _workspace(project_root), _context(project_root)
    )
    assert result.status == "succeeded"
    baseline = _read_baseline(change_dir)
    status = _read_status(change_dir)
    assert baseline.attempt == status.attempts_used == 1
    assert baseline.attempt_token


def test_baseline_test_digests_are_pre_mutation_snapshot(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir)
    pre = hash_test_tree(project_root)

    allocate_coverage_repair_attempt_operation(
        _allocate_task(), _workspace(project_root), _context(project_root)
    )
    baseline = _read_baseline(change_dir)
    assert baseline.test_files_sha256 == dict(pre.files)
    assert baseline.test_tree_sha256 == pre.aggregate

    (project_root / BRIEFED_TEST).write_text(
        f"def test_{CASE_ID.lower()}__happy():\n    assert 'mutated'\n",
        encoding="utf-8",
    )
    post = hash_test_tree(project_root)
    assert post.files != pre.files
    # Stored digest stays at allocation-time; this is the property safety rests on.
    assert baseline.test_files_sha256 == dict(pre.files)
    assert baseline.test_files_sha256 != dict(post.files)


def test_baseline_captures_product_and_declaration_trees(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir)

    allocate_coverage_repair_attempt_operation(
        _allocate_task(), _workspace(project_root), _context(project_root)
    )
    baseline = _read_baseline(change_dir)
    assert baseline.product_tree_sha256
    assert PRODUCT_FILE in baseline.product_files_sha256
    assert baseline.declaration_tree_sha256
    assert CASE_REL in baseline.declaration_files_sha256


def test_second_allocate_refreezes_baseline_at_attempt_two(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir)
    workspace = _workspace(project_root)
    context = _context(project_root)

    allocate_coverage_repair_attempt_operation(_allocate_task(), workspace, context)
    first = _read_baseline(change_dir)
    (project_root / BRIEFED_TEST).write_text(
        f"def test_{CASE_ID.lower()}__happy():\n    assert 'attempt1-edit'\n",
        encoding="utf-8",
    )
    allocate_coverage_repair_attempt_operation(_allocate_task(), workspace, context)
    second = _read_baseline(change_dir)

    assert second.attempt == 2
    assert second.test_files_sha256 != first.test_files_sha256
    assert second.test_files_sha256[BRIEFED_TEST] != first.test_files_sha256[BRIEFED_TEST]


def test_ineligible_allocate_writes_neither_status_nor_baseline(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir, eligible=False)

    result = allocate_coverage_repair_attempt_operation(
        _allocate_task(), _workspace(project_root), _context(project_root)
    )
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert not (change_dir / COVERAGE_REPAIR_STATUS_REL).exists()
    assert not (change_dir / COVERAGE_REPAIR_BASELINE_REL).exists()


def test_attempt_token_differs_across_attempts_with_unchanged_trees(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir)
    workspace = _workspace(project_root)
    context = _context(project_root)

    allocate_coverage_repair_attempt_operation(_allocate_task(), workspace, context)
    first = _read_baseline(change_dir)
    allocate_coverage_repair_attempt_operation(_allocate_task(), workspace, context)
    second = _read_baseline(change_dir)

    assert first.attempt_token
    assert second.attempt_token
    assert first.attempt_token != second.attempt_token
    assert first.test_tree_sha256 == second.test_tree_sha256
    assert first.product_tree_sha256 == second.product_tree_sha256
    assert first.declaration_tree_sha256 == second.declaration_tree_sha256


def test_declaration_digests_include_change_cases_via_declaration_roots(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir)

    allocate_coverage_repair_attempt_operation(
        _allocate_task(), _workspace(project_root), _context(project_root)
    )
    baseline = _read_baseline(change_dir)
    assert CASE_REL in baseline.declaration_files_sha256


def test_default_operations_registers_allocate_and_record_status() -> None:
    ops = default_operations()
    assert ops["operation:allocate-coverage-repair-attempt"] is allocate_coverage_repair_attempt_operation
    assert ops["operation:record-coverage-repair-status"] is record_coverage_repair_status_operation
