from __future__ import annotations

from itertools import combinations

import pytest

from tests.phase5.product_runner import GENERATION_FAMILIES

pytestmark = pytest.mark.usefixtures("product_runner")


@pytest.mark.parametrize("family", ["api", "e2e", "fuzz", "performance"])
def test_single_family_runs_only_its_generation_branch(product_runner, family):
    trace = product_runner(selected_test_families=(family,)).run_to_generation_join()
    assert trace.completed_generation_families == {family}
    assert trace.join_expected == {family}


@pytest.mark.parametrize(
    "families",
    [combo for size in range(1, 5) for combo in combinations(GENERATION_FAMILIES, size)],
)
def test_selected_subset_runs_exactly_those_families(product_runner, families):
    trace = product_runner(selected_test_families=families).run_to_generation_join()
    assert trace.completed_generation_families == set(families)
    assert trace.join_expected == set(families)
