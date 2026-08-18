from pathlib import Path
import json

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


def _write_mapping(change_dir: Path, target: str, target_file: str) -> None:
    plans = change_dir / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{target}-codegen-mapping.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "layer": target,
                "entries": [
                    {
                        "case_id": "TC_001",
                        "symbol": "test_tc_001__behavior",
                        "target_file": target_file,
                    }
                ],
                "schema_case_ids": ["TC_001"],
            }
        ),
        encoding="utf-8",
    )


def test_extracts_unique_paths_from_mapping_table(tmp_path: Path) -> None:
    _write_plan(tmp_path, "api", _PLAN)
    paths = resolve_test_paths(tmp_path, "api")
    assert paths == ["tests/api/test_dept_api.py"]


def test_extracts_plain_markdown_paths_from_mapping_table(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "api",
        """# API Codegen Plan

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| TC_USER_API_001 | test_tc_user_api_001__create | tests/api/test_user_api.py |
""",
    )

    assert resolve_test_paths(tmp_path, "api") == ["tests/api/test_user_api.py"]


def test_ignores_paths_outside_mapping_section(tmp_path: Path) -> None:
    _write_plan(tmp_path, "api", _PLAN)
    paths = resolve_test_paths(tmp_path, "api")
    assert paths is not None
    assert "tests/api/adapters/dept.py" not in paths
    assert "tests/testdata/domain/dept.py" not in paths


def test_returns_empty_when_plan_missing(tmp_path: Path) -> None:
    assert resolve_test_paths(tmp_path, "api") is None


def test_plan_without_mapping_section_fails_closed(tmp_path: Path) -> None:
    _write_plan(tmp_path, "e2e", "# E2E Codegen Plan\n\nNo mapping table here.\n")
    assert resolve_test_paths(tmp_path, "e2e") == []


def test_structured_mapping_is_source_of_truth_when_markdown_heading_drifts(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "e2e",
        "# E2E Codegen Plan\n\n## Closed Mapping Contract\n\n"
        "| Case ID | Test Function | Target File |\n"
        "|---|---|---|\n"
        "| TC_001 | test_tc_001__behavior | tests/e2e/test_wrong.py |\n",
    )
    _write_mapping(tmp_path, "e2e", "tests/e2e/test_dept_management.py")

    assert resolve_test_paths(tmp_path, "e2e") == ["tests/e2e/test_dept_management.py"]


def test_structured_api_mapping_accepts_nested_test_module(tmp_path: Path) -> None:
    _write_mapping(tmp_path, "api", "tests/api/system/test_dept.py")

    assert resolve_test_paths(tmp_path, "api") == ["tests/api/system/test_dept.py"]


def test_malformed_structured_mapping_fails_closed_instead_of_using_markdown(tmp_path: Path) -> None:
    _write_plan(tmp_path, "api", _PLAN)
    plans = tmp_path / "plans"
    (plans / "api-codegen-mapping.json").write_text("{broken", encoding="utf-8")

    assert resolve_test_paths(tmp_path, "api") == []


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


def test_fuzz_paths_fall_back_to_test_function_mapping(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "fuzz",
        """# Fuzz Codegen Plan

## Implementation Target

Extend `tests/fuzz/test_menu_fuzz.py` for the approved fuzz case only.

## Test Function Mapping

| Case ID | Test Function | Target File |
|---|---|---|
| TC_MENU_FUZZ_002 | `test_tc_menu_fuzz_002` | `tests/fuzz/test_menu_fuzz.py` |

## Reused Fixtures and Helpers

| Symbol | Location |
|---|---|
| helper | `tests/fuzz/test_unrelated_fuzz.py` |
""",
    )

    assert resolve_test_paths(tmp_path, "fuzz") == ["tests/fuzz/test_menu_fuzz.py"]


def test_performance_paths_fall_back_to_task_mapping(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "performance",
        """# Performance Codegen Plan

## Generated Artifact Target

Update `tests/perf/locustfile_menu.py` for this change.

## Task Mapping

| Case ID | Task Method | Target File |
|---|---|---|
| TC_MENU_PERF_002 | MenuTask.list | tests/perf/locustfile_menu.py |

## Factory Mapping

| Shared Module | Function |
|---|---|
| tests/perf/locustfile_unrelated.py | helper |
""",
    )

    assert resolve_test_paths(tmp_path, "performance") == ["tests/perf/locustfile_menu.py"]


def test_fuzz_paths_include_deeper_target_file_subheadings_until_same_level_heading(
    tmp_path: Path,
) -> None:
    _write_plan(
        tmp_path,
        "fuzz",
        """# Fuzz Codegen Plan

## Target Files

### 1. tests/fuzz/test_dept_fuzz.py

| Path | Policy |
|------|--------|
| `tests/fuzz/test_dept_fuzz.py` | generate |

## Support Modules

| Path | Policy |
|------|--------|
| `tests/fuzz/test_unrelated_fuzz.py` | do not run |
""",
    )

    assert resolve_test_paths(tmp_path, "fuzz") == ["tests/fuzz/test_dept_fuzz.py"]


def test_fuzz_paths_stop_at_higher_level_heading(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "fuzz",
        """## Target Files

### 1. tests/fuzz/test_dept_fuzz.py

- `tests/fuzz/test_dept_fuzz.py`

# Another Plan

- `tests/fuzz/test_unrelated_fuzz.py`
""",
    )

    assert resolve_test_paths(tmp_path, "fuzz") == ["tests/fuzz/test_dept_fuzz.py"]


def test_fuzz_paths_ignore_non_atx_heading_like_lines(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "fuzz",
        """## Target Files

##not-a-heading
####### not-a-heading

- `tests/fuzz/test_dept_fuzz.py`
""",
    )

    assert resolve_test_paths(tmp_path, "fuzz") == ["tests/fuzz/test_dept_fuzz.py"]


def test_fuzz_paths_stop_at_empty_same_level_heading(tmp_path: Path) -> None:
    _write_plan(
        tmp_path,
        "fuzz",
        """## Target Files

- `tests/fuzz/test_dept_fuzz.py`

##

- `tests/fuzz/test_unrelated_fuzz.py`
""",
    )

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
