"""Task 4: coverage-repair safety — mechanical change set + summary cross-check."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.coverage_gaps import CoverageGapLocator
from assurance_agent.artifacts.models.coverage_repair import (
    COVERAGE_REPAIR_APPLY_SUMMARY_REL,
    COVERAGE_REPAIR_BASELINE_REL,
    COVERAGE_REPAIR_BRIEF_REL,
    COVERAGE_REPAIR_SAFETY_REL,
    CoverageRepairApplySummary,
    CoverageRepairBaseline,
    CoverageRepairBrief,
    CoverageRepairSafetyCheck,
    RepairItem,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.execution.tree_hash import (
    hash_product_tree,
    hash_test_tree,
)
from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.healing.safety import load_product_code_roots
from assurance_agent.workflow.metrics.coverage_repair import (
    compute_coverage_repair_safety,
    compute_coverage_repair_safety_operation,
    declaration_roots,
    mint_attempt_token,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-REPAIR-001"
CASE_ID = "TC_API_001"
BRIEFED_TEST = f"tests/api/test_{CASE_ID.lower()}.py"
UNRELATED_TEST = "tests/unit/test_unrelated.py"
PRODUCT_FILE = "app/service.py"
ATTEMPT = 1


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
        node_id="compute-coverage-repair-safety",
        graph_id="coverage-repair",
        target="operation:compute-coverage-repair-safety",
        input={"with": {}},
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
    (project_root / "tests" / "unit").mkdir(parents=True)
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
    (project_root / UNRELATED_TEST).write_text("def test_other():\n    assert True\n", encoding="utf-8")
    return project_root, change_dir


def _freeze_baseline(
    project_root: Path,
    change_dir: Path,
    *,
    attempt: int = ATTEMPT,
    change_id: str = CHANGE_ID,
) -> CoverageRepairBaseline:
    test_tree = hash_test_tree(project_root)
    product_tree = hash_product_tree(project_root, load_product_code_roots(project_root))
    decl_tree = hash_product_tree(project_root, list(declaration_roots(project_root, change_id)))
    token = mint_attempt_token(
        change_id=change_id,
        attempt=attempt,
        test_tree_sha256=test_tree.aggregate,
        product_tree_sha256=product_tree.aggregate,
        declaration_tree_sha256=decl_tree.aggregate,
    )
    baseline = CoverageRepairBaseline(
        change_id=change_id,
        attempt=attempt,
        attempt_token=token,
        test_tree_sha256=test_tree.aggregate,
        test_files_sha256=dict(test_tree.files),
        product_tree_sha256=product_tree.aggregate,
        product_files_sha256=dict(product_tree.files),
        declaration_tree_sha256=decl_tree.aggregate,
        declaration_files_sha256=dict(decl_tree.files),
    )
    out = change_dir / COVERAGE_REPAIR_BASELINE_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(baseline))
    return baseline


def _write_brief(change_dir: Path, *, case_id: str = CASE_ID) -> None:
    brief = CoverageRepairBrief(
        change_id=CHANGE_ID,
        batch_id="20260806-120000",
        probe_verdict="needs_human",
        eligible=True,
        repair_items=(
            RepairItem(
                kind="uncovered_required_case",
                locator=CoverageGapLocator(case_id=case_id),
                metric="constraint_coverage",
                hint="add test",
            ),
        ),
        computed_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )
    out = change_dir / COVERAGE_REPAIR_BRIEF_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(brief))


def _write_summary(
    change_dir: Path,
    baseline: CoverageRepairBaseline,
    *,
    files_modified: tuple[str, ...] = (),
    applied: bool | None = None,
    change_id: str | None = None,
    attempt: int | None = None,
    attempt_token: str | None = None,
) -> None:
    summary = CoverageRepairApplySummary(
        change_id=change_id if change_id is not None else baseline.change_id,
        attempt=attempt if attempt is not None else baseline.attempt,
        attempt_token=attempt_token if attempt_token is not None else baseline.attempt_token,
        applied=bool(files_modified) if applied is None else applied,
        files_modified=files_modified,
    )
    out = change_dir / COVERAGE_REPAIR_APPLY_SUMMARY_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(summary))


def _mutate(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── Step 1: mechanical facts ─────────────────────────────────────────────────


def test_product_edit_fails_passed(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(project_root / PRODUCT_FILE, "def ok():\n    return 2\n")
    _write_summary(change_dir, baseline, files_modified=(PRODUCT_FILE,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.product_code_modified is True
    assert check.passed is False


def test_declaration_edit_fails_passed(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    case_path = change_dir / "cases" / "system" / "api" / "case.yaml"
    _mutate(case_path, "schema_version: '1.0'\nadded: [{case_id: X}]\n")
    rel = case_path.relative_to(project_root).as_posix()
    _write_summary(change_dir, baseline, files_modified=(rel,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.declaration_files_modified is True
    assert check.passed is False


def test_skip_marker_needs_review(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(
        project_root / BRIEFED_TEST,
        f"import pytest\n\n@pytest.mark.skip\ndef test_{CASE_ID.lower()}__happy():\n    assert True\n",
    )
    _write_summary(change_dir, baseline, files_modified=(BRIEFED_TEST,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.skip_or_xfail_added is True
    assert check.needs_review is True


def test_unrelated_test_is_unbriefed(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(project_root / UNRELATED_TEST, "def test_other():\n    assert 1 == 1\n")
    _write_summary(change_dir, baseline, files_modified=(UNRELATED_TEST,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert UNRELATED_TEST in check.unbriefed_files_modified
    assert check.needs_review is True


def test_comment_planted_case_id_does_not_brief_unrelated_path(tmp_path: Path) -> None:
    """Content-substring briefing must not launder an unbriefed mechanical edit."""
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(
        project_root / UNRELATED_TEST,
        f"# planted brief token: {CASE_ID}\ndef test_other():\n    assert 1 == 1\n",
    )
    _write_summary(change_dir, baseline, files_modified=(UNRELATED_TEST,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert UNRELATED_TEST in check.unbriefed_files_modified
    assert check.needs_review is True


def test_missing_apply_summary_is_invalid_input(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    # No apply-summary.json on disk.

    result = compute_coverage_repair_safety_operation(
        _task(), _workspace(project_root), _context(project_root)
    )
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert not (change_dir / COVERAGE_REPAIR_SAFETY_REL).is_file()


def test_clean_briefed_repair_passes(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(
        project_root / BRIEFED_TEST,
        f"def test_{CASE_ID.lower()}__happy():\n    assert 1 == 1\n",
    )
    _write_summary(change_dir, baseline, files_modified=(BRIEFED_TEST,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.passed is True
    assert check.needs_review is False
    assert check.summary_mismatch is False


def test_ignores_hostile_healing_evidence(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(
        project_root / BRIEFED_TEST,
        f"def test_{CASE_ID.lower()}__happy():\n    assert 1 == 1\n",
    )
    _write_summary(change_dir, baseline, files_modified=(BRIEFED_TEST,))
    healing = change_dir / "healing"
    healing.mkdir(parents=True)
    (healing / "fixer-safety-check.json").write_text(
        json.dumps({"passed": False, "needs_review": True, "hostile": True}),
        encoding="utf-8",
    )

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.passed is True
    assert check.needs_review is False


# ── Step 2: anti-concealment (design v4-2) ───────────────────────────────────


def test_concealed_unbriefed_test_edit(tmp_path: Path) -> None:
    """Safety cannot be defeated by omission from the self-report (design v4-2)."""
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(project_root / UNRELATED_TEST, "def test_other():\n    assert False\n")
    # Self-report lists only a briefed path — omits the unrelated edit.
    _write_summary(change_dir, baseline, files_modified=(BRIEFED_TEST,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert UNRELATED_TEST in check.unbriefed_files_modified
    assert check.summary_mismatch is True
    assert check.needs_review is True


def test_concealed_product_edit(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(project_root / PRODUCT_FILE, "def ok():\n    return 99\n")
    _write_summary(change_dir, baseline, files_modified=(BRIEFED_TEST,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.product_code_modified is True
    assert check.passed is False


def test_honest_report_no_false_positive_mismatch(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(
        project_root / BRIEFED_TEST,
        f"def test_{CASE_ID.lower()}__happy():\n    assert 2 == 2\n",
    )
    _write_summary(change_dir, baseline, files_modified=(BRIEFED_TEST,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.summary_mismatch is False


def test_honest_report_with_sloppy_path_spelling(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(
        project_root / BRIEFED_TEST,
        f"def test_{CASE_ID.lower()}__happy():\n    assert 3 == 3\n",
    )
    sloppy = (f"./tests/api/test_{CASE_ID.lower()}.py", str(project_root / BRIEFED_TEST))
    # Report the same single mechanical change with two spellings of that one path
    # across two runs — first ./ prefix, then absolute.
    _write_summary(change_dir, baseline, files_modified=(sloppy[0],))
    check_dot = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check_dot.summary_mismatch is False

    _write_summary(change_dir, baseline, files_modified=(sloppy[1],))
    check_abs = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check_abs.summary_mismatch is False


def test_applied_false_does_not_blank_mechanical_set(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(
        project_root / BRIEFED_TEST,
        f"def test_{CASE_ID.lower()}__happy():\n    assert 4 == 4\n",
    )
    _write_summary(change_dir, baseline, files_modified=(), applied=False)

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert BRIEFED_TEST in check.test_files_changed
    assert check.summary_mismatch is True


def test_missing_baseline_is_invalid_input_not_clean(tmp_path: Path) -> None:
    # Degrading to clean here would restore the bypass this task exists to remove.
    project_root, change_dir = _seed_project(tmp_path)
    _write_brief(change_dir)
    _write_summary(
        change_dir,
        CoverageRepairBaseline(
            change_id=CHANGE_ID,
            attempt=1,
            attempt_token="deadbeefdeadbeef",
            test_tree_sha256="aa" * 32,
            test_files_sha256={},
            product_tree_sha256="bb" * 32,
            product_files_sha256={},
            declaration_tree_sha256="cc" * 32,
            declaration_files_sha256={},
        ),
        files_modified=(),
        applied=False,
    )
    # No entry-baseline.json on disk.
    result = compute_coverage_repair_safety_operation(
        _task(), _workspace(project_root), _context(project_root)
    )
    assert result.status == "failed"
    assert result.error_kind == "invalid_input"
    assert result.value is None or not (isinstance(result.value, dict) and result.value.get("passed") is True)
    assert not (change_dir / COVERAGE_REPAIR_SAFETY_REL).is_file()


def test_baseline_freeze_ordering_detects_post_freeze_mutation(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(
        project_root / BRIEFED_TEST,
        f"def test_{CASE_ID.lower()}__happy():\n    assert 'after-freeze'\n",
    )
    _write_summary(change_dir, baseline, files_modified=(BRIEFED_TEST,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert BRIEFED_TEST in check.test_files_changed


# ── Step 2b: stale summary (design v4-5) ─────────────────────────────────────


def test_matching_attempt_token_not_stale(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _write_summary(change_dir, baseline, files_modified=(), applied=False)

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.stale_summary is False


def test_prior_attempt_summary_is_stale(tmp_path: Path) -> None:
    # Declared outputs are only checked for existence (workspace.py:876-879), so
    # the previous attempt's file survives; this check is the only thing that notices.
    project_root, change_dir = _seed_project(tmp_path)
    attempt1 = _freeze_baseline(project_root, change_dir, attempt=1)
    _write_brief(change_dir)
    _write_summary(change_dir, attempt1, files_modified=(), applied=False)
    # Re-freeze as attempt 2 with identical trees; leave attempt-1 summary on disk.
    _freeze_baseline(project_root, change_dir, attempt=2)

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.stale_summary is True
    assert check.needs_review is True


def test_stale_summary_does_not_weaken_mechanical_fields(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    attempt1 = _freeze_baseline(project_root, change_dir, attempt=1)
    _write_brief(change_dir)
    _write_summary(change_dir, attempt1, files_modified=(), applied=False)
    _freeze_baseline(project_root, change_dir, attempt=2)

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.stale_summary is True
    assert check.product_code_modified is False
    assert check.declaration_files_modified is False
    assert check.unbriefed_files_modified == ()
    assert check.passed is True
    assert check.needs_review is True


def test_mismatched_change_id_alone_is_stale(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _write_summary(
        change_dir,
        baseline,
        files_modified=(),
        applied=False,
        change_id="CH-OTHER-999",
    )

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.stale_summary is True


def test_attempt_token_includes_attempt_not_just_tree_digests(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    _ = change_dir
    test_tree = hash_test_tree(project_root)
    product_tree = hash_product_tree(project_root, load_product_code_roots(project_root))
    decl_tree = hash_product_tree(project_root, list(declaration_roots(project_root, CHANGE_ID)))
    t1 = mint_attempt_token(
        change_id=CHANGE_ID,
        attempt=1,
        test_tree_sha256=test_tree.aggregate,
        product_tree_sha256=product_tree.aggregate,
        declaration_tree_sha256=decl_tree.aggregate,
    )
    t2 = mint_attempt_token(
        change_id=CHANGE_ID,
        attempt=2,
        test_tree_sha256=test_tree.aggregate,
        product_tree_sha256=product_tree.aggregate,
        declaration_tree_sha256=decl_tree.aggregate,
    )
    assert t1 != t2
    assert len(t1) == 16
    assert len(t2) == 16


# ── Step 2c: declaration-root consistency (design v4-6) ──────────────────────


def test_preexisting_change_cases_do_not_flag_declaration_modified(tmp_path: Path) -> None:
    """Guard: mismatched root sets would read pre-existing cases as newly added."""
    project_root, change_dir = _seed_project(tmp_path)
    assert (change_dir / "cases" / "system" / "api" / "case.yaml").is_file()
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _mutate(
        project_root / BRIEFED_TEST,
        f"def test_{CASE_ID.lower()}__happy():\n    assert 'briefed'\n",
    )
    _write_summary(change_dir, baseline, files_modified=(BRIEFED_TEST,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.declaration_files_modified is False
    assert check.passed is True


def test_declaration_roots_include_resolved_change_cases(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    roots = declaration_roots(project_root, CHANGE_ID)
    change_rel = change_dir.relative_to(project_root).as_posix()
    assert "qa/cases" in roots
    assert ".aa" in roots
    assert f"{change_rel}/cases" in roots


def test_genuine_change_cases_edit_is_caught(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    case_path = change_dir / "cases" / "system" / "api" / "case.yaml"
    _mutate(case_path, "schema_version: '1.0'\nadded: [{case_id: NEW}]\n")
    rel = case_path.relative_to(project_root).as_posix()
    _write_summary(change_dir, baseline, files_modified=(rel,))

    check = compute_coverage_repair_safety(
        change_dir=change_dir, project_root=project_root, change_id=CHANGE_ID
    )
    assert check.declaration_files_modified is True


def test_operation_registered_and_writes_safety_check(tmp_path: Path) -> None:
    project_root, change_dir = _seed_project(tmp_path)
    baseline = _freeze_baseline(project_root, change_dir)
    _write_brief(change_dir)
    _write_summary(change_dir, baseline, files_modified=(), applied=False)

    ops = default_operations()
    assert "operation:compute-coverage-repair-safety" in ops
    result = ops["operation:compute-coverage-repair-safety"](
        _task(), _workspace(project_root), _context(project_root)
    )
    assert result.status == "succeeded"
    path = change_dir / COVERAGE_REPAIR_SAFETY_REL
    assert path.is_file()
    check = CoverageRepairSafetyCheck.model_validate_json(path.read_text(encoding="utf-8"))
    assert check.passed is True
    assert check.stale_summary is False
