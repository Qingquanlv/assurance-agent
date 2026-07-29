"""Current tests-tree scan, per-file digest compare, integrity (Task 4)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import yaml

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.evidence.trace import (
    ExecutionFoldInput,
    canonical_json_bytes,
    fold_trace,
)
from assurance_agent.evidence.tree_scan import scan_test_tree
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-1"
EXECUTED_AT = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


def _setup(tmp_path: Path) -> Path:
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    return change_dir


def _write_case(change_dir: Path, *, case_id: str = "TC_DEPT_API_001", case_type: str = "API") -> None:
    body = f"""
schema_version: "1.0"
added:
  - case_id: {case_id}
    module: system.dept
    type: {case_type}
    title: t
    status: active
    priority: P0
    severity: blocker
    automation:
      required: true
modified: []
removed: []
"""
    if case_type == "Performance":
        body = f"""
schema_version: "1.0"
added:
  - case_id: {case_id}
    module: system.dept
    type: Performance
    title: t
    status: active
    priority: P0
    severity: blocker
    automation:
      required: true
      performance:
        scenario:
          capability: dept_list_p95
modified: []
removed: []
"""
    path = change_dir / "cases" / "x" / "case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _write_manifest(change_dir: Path, test_files: dict[str, str] | None = None) -> None:
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": "20260729-120000",
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "result_files": {},
        "test_files_sha256": test_files or {},
        "executed_at": EXECUTED_AT.isoformat(),
    }
    path = change_dir / "execution" / "execution-manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_function_name_hit_and_docstring_does_not_map(tmp_path: Path) -> None:
    root = tmp_path
    tests = root / "tests" / "api"
    tests.mkdir(parents=True)
    (tests / "test_dept.py").write_text(
        '''
"""Cases: TC_DEPT_E2E_001 – TC_DEPT_E2E_005"""

def test_TC_DEPT_API_001__create():
    """Mentions TC_DEPT_API_999 in docstring only."""
    assert True

# TC_DEPT_API_002 should not map from this comment
''',
        encoding="utf-8",
    )
    scan = scan_test_tree(root)
    assert "TC_DEPT_API_001" in scan.case_ids
    assert "TC_DEPT_E2E_001" not in scan.case_ids
    assert "TC_DEPT_API_999" not in scan.case_ids
    assert "TC_DEPT_API_002" not in scan.case_ids
    assert "test_TC_DEPT_API_001__create" in scan.functions


def test_async_def_is_hit(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_async.py").write_text(
        "async def test_TC_DEPT_API_003__async():\n    pass\n",
        encoding="utf-8",
    )
    scan = scan_test_tree(tmp_path)
    assert "TC_DEPT_API_003" in scan.case_ids


def test_perf_name_hit_ignores_comment(tmp_path: Path) -> None:
    perf = tmp_path / "tests" / "perf"
    perf.mkdir(parents=True)
    (perf / "locustfile_dept.py").write_text(
        """
# name="commented_cap"
def task():
    name = "dept_list_p95"
    pass
""",
        encoding="utf-8",
    )
    scan = scan_test_tree(tmp_path)
    assert "dept_list_p95" in scan.perf_capabilities
    assert "commented_cap" not in scan.perf_capabilities


def test_tree_digest_is_canonical_json_of_file_sha256(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    path = tests / "a.py"
    path.write_text("def test_x():\n    pass\n", encoding="utf-8")
    scan = scan_test_tree(tmp_path)
    expected = hashlib.sha256(canonical_json_bytes(dict(sorted(scan.file_sha256.items())))).hexdigest()
    assert scan.tree_digest == expected
    assert "tests/a.py" in scan.file_sha256


def test_fold_marks_covered_and_atemporal_kind(tmp_path: Path) -> None:
    change_dir = _setup(tmp_path)
    _write_case(change_dir)
    tests = tmp_path / "tests" / "api"
    tests.mkdir(parents=True)
    (tests / "test_dept.py").write_text(
        "def test_TC_DEPT_API_001__create():\n    assert True\n",
        encoding="utf-8",
    )
    scan = scan_test_tree(tmp_path)
    batch_id = "20260729-120000"
    runs = change_dir / "execution" / "runs" / batch_id
    runs.mkdir(parents=True)
    (runs / "api-result.json").write_text(
        """
{
  "schema_version": "1.0",
  "change_id": "CH-1",
  "batch_id": "20260729-120000",
  "target": "api",
  "status": "passed",
  "command": "cmd",
  "source": {"framework": "pytest", "raw_log": "raw.log"},
  "total": 1,
  "passed": 1,
  "failed": 0,
  "skipped": 0,
  "cases": [
    {"case_id": "TC_DEPT_API_001", "status": "passed", "file": "tests/api/test_dept.py",
     "test_name": "test_TC_DEPT_API_001__create", "duration_ms": 1, "message": ""}
  ],
  "unmapped_tests": []
}
""",
        encoding="utf-8",
    )
    _write_manifest(change_dir, dict(scan.file_sha256))
    proj = fold_trace(tmp_path, CHANGE_ID, phase="execution")
    row = proj.rows[0]
    assert row.coverage_state == "covered"
    assert row.covering_tests[0].function == "test_TC_DEPT_API_001__create"
    assert "covered" in row.atemporal_kinds_present
    assert any(s.path == "tests/#tree-digest" and s.sha256 == scan.tree_digest for s in proj.sources)
    assert proj.integrity == "complete"


def test_mapped_test_missing_from_tree_is_degraded(tmp_path: Path) -> None:
    change_dir = _setup(tmp_path)
    _write_case(change_dir)
    # No covering test in tree, but latest batch recorded a mapped test.
    batch = "20260729-120000"
    runs = change_dir / "execution" / "runs" / batch
    runs.mkdir(parents=True)
    (runs / "api-result.json").write_text(
        """
{
  "change_id": "CH-1",
  "batch_id": "20260729-120000",
  "target": "api",
  "cases": [
    {"case_id": "TC_DEPT_API_001", "status": "passed", "file": "tests/api/test_dept.py",
     "test_name": "test_TC_DEPT_API_001__create"}
  ],
  "unmapped_tests": []
}
""",
        encoding="utf-8",
    )
    _write_manifest(change_dir, {})
    proj = fold_trace(tmp_path, CHANGE_ID, phase="execution")
    assert proj.rows[0].coverage_state == "uncovered"
    assert any(g.code == "mapped_test_missing_from_tree" for g in proj.gaps)
    assert proj.integrity == "degraded"


def test_per_file_mismatch_content_add_delete(tmp_path: Path) -> None:
    change_dir = _setup(tmp_path)
    _write_case(change_dir)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "a.py").write_text("def test_TC_DEPT_API_001__x():\n    pass\n", encoding="utf-8")
    scan = scan_test_tree(tmp_path)

    # content drift
    wrong = dict(scan.file_sha256)
    wrong["tests/a.py"] = "0" * 64
    _write_manifest(change_dir, wrong)
    proj = fold_trace(tmp_path, CHANGE_ID, phase="execution")
    assert any(g.code == "tests_tree_digest_mismatch" for g in proj.gaps)
    assert proj.integrity == "incomplete"

    # add drift (manifest missing a scanned file)
    _write_manifest(change_dir, {})
    proj = fold_trace(tmp_path, CHANGE_ID, phase="execution")
    assert any(g.code == "tests_tree_digest_mismatch" for g in proj.gaps)

    # delete drift (manifest has extra path)
    extra = dict(scan.file_sha256)
    extra["tests/gone.py"] = "a" * 64
    _write_manifest(change_dir, extra)
    proj = fold_trace(tmp_path, CHANGE_ID, phase="execution")
    assert any(g.code == "tests_tree_digest_mismatch" for g in proj.gaps)


def test_injection_and_disk_use_same_compare(tmp_path: Path) -> None:
    change_dir = _setup(tmp_path)
    _write_case(change_dir)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "a.py").write_text("def test_TC_DEPT_API_001__x():\n    pass\n", encoding="utf-8")
    scan = scan_test_tree(tmp_path)
    selected = SelectedTargets(api=True, e2e=False, fuzz=False, performance=False)
    injected = fold_trace(
        tmp_path,
        CHANGE_ID,
        phase="execution",
        current=ExecutionFoldInput(
            batch_id="20260729-120000",
            executed_at=EXECUTED_AT,
            selected_targets=selected,
            test_files_sha256=dict(scan.file_sha256),
        ),
    )
    _write_manifest(change_dir, dict(scan.file_sha256))
    disk = fold_trace(tmp_path, CHANGE_ID, phase="execution")
    assert not any(g.code == "tests_tree_digest_mismatch" for g in injected.gaps)
    assert not any(g.code == "tests_tree_digest_mismatch" for g in disk.gaps)
    assert injected.model_dump(mode="json")["rows"] == disk.model_dump(mode="json")["rows"]


def test_manifest_missing_only_when_no_view(tmp_path: Path) -> None:
    change_dir = _setup(tmp_path)
    _write_case(change_dir)
    proj = fold_trace(tmp_path, CHANGE_ID, phase="execution")
    assert any(g.code == "manifest_missing" for g in proj.gaps)
    assert proj.integrity == "incomplete"


def test_perf_capability_marks_covered(tmp_path: Path) -> None:
    change_dir = _setup(tmp_path)
    _write_case(change_dir, case_id="TC_DEPT_PERF_001", case_type="Performance")
    perf = tmp_path / "tests" / "perf"
    perf.mkdir(parents=True)
    (perf / "locustfile_dept.py").write_text(
        'def task():\n    name="dept_list_p95"\n',
        encoding="utf-8",
    )
    scan = scan_test_tree(tmp_path)
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": "20260729-120000",
        "selected_targets": {"api": False, "e2e": False, "fuzz": False, "performance": True},
        "result_files": {},
        "test_files_sha256": dict(scan.file_sha256),
        "executed_at": EXECUTED_AT.isoformat(),
    }
    path = change_dir / "execution" / "execution-manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    proj = fold_trace(tmp_path, CHANGE_ID, phase="execution")
    assert proj.rows[0].coverage_state == "covered"
    assert "covered" in proj.rows[0].atemporal_kinds_present


def test_zero_rows_is_incomplete(tmp_path: Path) -> None:
    change_dir = _setup(tmp_path)
    _write_manifest(change_dir, {})
    # empty cases
    path = change_dir / "cases" / "x" / "case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n",
        encoding="utf-8",
    )
    proj = fold_trace(tmp_path, CHANGE_ID, phase="execution")
    assert proj.rows == ()
    assert proj.integrity == "incomplete"
