from pathlib import Path

from assurance_agent.workflow.execution.scope import resolve_test_paths

_PLAN = """# API Codegen Plan — CH-1

## Target Files

| File | Purpose |
|------|---------|
| `tests/api/test_dept_api.py` | test functions |
| `tests/api/adapters/dept.py` | adapter, not a test file |

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| TC_DEPT_API_001 | `test_tc_dept_api_001__ok` | `tests/api/test_dept_api.py` |
| TC_DEPT_API_002 | `test_tc_dept_api_002__ok` | `tests/api/test_dept_api.py` |

## Factory Mapping

| Entity | Shared Module | Function |
|--------|---------------|----------|
| Dept | `tests/testdata/domain/dept.py` | `make_dept` |
"""


def _write_plan(change_dir: Path, target: str, text: str) -> None:
    plans = change_dir / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{target}-codegen-plan.md").write_text(text, encoding="utf-8")


def test_extracts_unique_paths_from_mapping_table(tmp_path: Path) -> None:
    _write_plan(tmp_path, "api", _PLAN)
    paths = resolve_test_paths(tmp_path, "api")
    assert paths == ["tests/api/test_dept_api.py"]


def test_ignores_paths_outside_mapping_section(tmp_path: Path) -> None:
    _write_plan(tmp_path, "api", _PLAN)
    paths = resolve_test_paths(tmp_path, "api")
    assert "tests/api/adapters/dept.py" not in paths
    assert "tests/testdata/domain/dept.py" not in paths


def test_returns_empty_when_plan_missing(tmp_path: Path) -> None:
    assert resolve_test_paths(tmp_path, "api") == []


def test_returns_empty_when_no_mapping_section(tmp_path: Path) -> None:
    _write_plan(tmp_path, "e2e", "# E2E Codegen Plan\n\nNo mapping table here.\n")
    assert resolve_test_paths(tmp_path, "e2e") == []


def test_multiple_target_files_sorted_and_deduped(tmp_path: Path) -> None:
    text = """# Plan

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| TC_1 | `test_a` | `tests/api/test_b.py` |
| TC_2 | `test_b` | `tests/api/test_a.py` |
| TC_3 | `test_c` | `tests/api/test_a.py` |
"""
    _write_plan(tmp_path, "api", text)
    assert resolve_test_paths(tmp_path, "api") == ["tests/api/test_a.py", "tests/api/test_b.py"]


def test_performance_paths_come_from_target_files_section(tmp_path: Path) -> None:
    text = """# Performance Codegen Plan

## Target Files

| File | Purpose |
|------|---------|
| `tests/perf/locustfile_dept.py` | selected load test |
| `tests/perf/adapters/dept_seed.py` | setup helper |

## Task Mapping

| Capability | Target File |
|------------|-------------|
| dept-list | `tests/perf/locustfile_unrelated.py` |
"""
    _write_plan(tmp_path, "performance", text)

    assert resolve_test_paths(tmp_path, "performance") == [
        "tests/perf/adapters/dept_seed.py",
        "tests/perf/locustfile_dept.py",
    ]
