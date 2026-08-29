from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

import pytest

from assurance_generation.contracts.families import GENERATION_FAMILIES, validate_selected_families

_FEATURE_TESTS = Path(__file__).resolve().parents[2] / "packages/features/assurance-generation/tests"
if str(_FEATURE_TESTS) not in sys.path:
    sys.path.insert(0, str(_FEATURE_TESTS))

from test_workflow_module import _drive_generate  # noqa: E402


@pytest.mark.parametrize("family", list(GENERATION_FAMILIES))
def test_single_family_runs_only_its_generation_branch(family: str) -> None:
    result = _drive_generate(selected=(family,))
    assert result.dispatched_families == {family}
    assert result.skip_families == set(GENERATION_FAMILIES) - {family}
    assert result.join_token_count == 4


@pytest.mark.parametrize(
    "families",
    [combo for size in range(1, 5) for combo in combinations(GENERATION_FAMILIES, size)],
)
def test_selected_subset_runs_exactly_those_families(families: tuple[str, ...]) -> None:
    result = _drive_generate(selected=families)
    assert result.dispatched_families == set(families)
    assert result.skip_families == set(GENERATION_FAMILIES) - set(families)
    assert result.join_token_count == 4


@pytest.mark.parametrize("selected", [(), ("api", "api"), ("api", "mobile")])
def test_invalid_family_selection_fails_at_feature_input(selected: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        validate_selected_families(selected)
