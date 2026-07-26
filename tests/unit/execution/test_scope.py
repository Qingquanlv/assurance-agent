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
    assert paths is not None
    assert "tests/api/adapters/dept.py" not in paths
    assert "tests/testdata/domain/dept.py" not in paths


def test_returns_empty_when_plan_missing(tmp_path: Path) -> None:
    assert resolve_test_paths(tmp_path, "api") is None


def test_returns_empty_when_no_mapping_section(tmp_path: Path) -> None:
    _write_plan(tmp_path, "e2e", "# E2E Codegen Plan\n\nNo mapping table here.\n")
    assert resolve_test_paths(tmp_path, "e2e") is None


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


def test_fuzz_paths_come_from_target_files_and_exclude_support_modules(tmp_path: Path) -> None:
    text = """# Fuzz Codegen Plan

## Target Files

| Path | Policy |
|------|--------|
| `tests/fuzz/test_dept_fuzz.py` | retarget |
| `tests/fuzz/strategies/dept.py` | reuse |
| `tests/fuzz/adapters/dept.py` | reuse |
| `tests/testdata/domain/dept.py` | reuse |
"""
    _write_plan(tmp_path, "fuzz", text)

    assert resolve_test_paths(tmp_path, "fuzz") == ["tests/fuzz/test_dept_fuzz.py"]


def test_fuzz_parseable_target_files_without_a_test_is_scoped_empty(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "fuzz",
        "## Target Files\n\n- `tests/fuzz/strategies/dept.py`\n- `tests/fuzz/adapters/dept.py`\n",
    )

    assert resolve_test_paths(tmp_path, "fuzz") == []


def test_fuzz_rejects_traversal_and_nested_test_paths(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "fuzz",
        "## Target Files\n\n"
        "- `tests/fuzz/test_dept_fuzz.py`\n"
        "- `tests/fuzz/../api/test_history.py`\n"
        "- `tests/fuzz/nested/test_history.py`\n",
    )

    assert resolve_test_paths(tmp_path, "fuzz") == ["tests/fuzz/test_dept_fuzz.py"]
