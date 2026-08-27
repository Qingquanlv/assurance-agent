from __future__ import annotations

import pytest

from tests.phase4.path_cases import (
    FILESYSTEM_CASES,
    NO_WORKSPACE_OPEN_WHEELS,
    PATH_CASES,
    WHEELS,
    assert_no_workspace_open_seam,
    assert_no_write_outside_workspace,
    exercise_path_case,
)

_CASES = (
    "absolute",
    "parent-dotdot",
    "windows-drive",
    "symlink-file",
    "symlink-parent",
    "hard-link",
    "path-swap",
    "undeclared-write-root",
)
_WHEELS = (
    "intake",
    "generation",
    "execution",
    "healing",
    "quality",
    "improvement",
)


@pytest.mark.parametrize("wheel", _WHEELS)
@pytest.mark.parametrize("case", _CASES)
async def test_path_cases_fail_before_spawn_and_stay_inside_workspace(wheel: str, case: str) -> None:
    assert wheel in WHEELS
    assert case in PATH_CASES
    observed = await exercise_path_case(wheel, case)
    assert observed.spawned is False
    assert observed.effect_emitted is False
    assert_no_write_outside_workspace(observed)
    if case in FILESYSTEM_CASES and wheel in NO_WORKSPACE_OPEN_WHEELS:
        assert observed.seam == "uncovered"
        assert_no_workspace_open_seam(wheel)
        return
    assert observed.rejected is True
    assert observed.seam != "uncovered"
