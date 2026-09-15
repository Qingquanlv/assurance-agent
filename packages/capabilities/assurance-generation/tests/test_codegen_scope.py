from __future__ import annotations

import pytest

from assurance_generation.contracts.codegen import (
    FAMILY_DIRS,
    FAMILY_TARGET_ROOTS,
    case_module_from_path,
    locked_testdata_file,
    locked_test_file,
)


def test_family_dirs_and_target_roots_are_per_family() -> None:
    assert FAMILY_DIRS == {
        "api": "api",
        "e2e": "e2e",
        "fuzz": "fuzz",
        "performance": "perf",
    }
    assert FAMILY_TARGET_ROOTS["api"] == ("qa/tests/api/", "qa/tests/testdata/api/")
    assert FAMILY_TARGET_ROOTS["e2e"] == ("qa/tests/e2e/", "qa/tests/testdata/e2e/")
    assert FAMILY_TARGET_ROOTS["fuzz"] == ("qa/tests/fuzz/", "qa/tests/testdata/fuzz/")
    assert FAMILY_TARGET_ROOTS["performance"] == ("qa/tests/perf/", "qa/tests/testdata/perf/")


def test_locked_paths_use_module_and_family_dir() -> None:
    assert case_module_from_path("qa/cases/dept/case.yaml") == "dept"
    assert locked_test_file("api", "dept") == "qa/tests/api/dept/test_dept.py"
    assert locked_testdata_file("api", "dept") == "qa/tests/testdata/api/dept.py"
    assert case_module_from_path("qa/cases/foo/bar/case.yaml") == "foo/bar"
    assert locked_test_file("e2e", "foo/bar") == "qa/tests/e2e/foo/bar/test_bar.py"
    assert locked_testdata_file("e2e", "foo/bar") == "qa/tests/testdata/e2e/foo/bar.py"


def test_case_module_from_path_rejects_non_case_yaml() -> None:
    with pytest.raises(ValueError, match="qa/cases/<module>/case.yaml"):
        case_module_from_path("qa/cases/dept.yaml")
