from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("product_runner")


_ALL_FAMILIES = ("api", "e2e", "fuzz", "performance")


def test_all_family_join_is_order_independent(product_runner):
    forward = product_runner(
        selected_test_families=_ALL_FAMILIES,
        completion_order="forward",
    ).run_to_generation_join()
    reverse = product_runner(
        selected_test_families=_ALL_FAMILIES,
        completion_order="reverse",
    ).run_to_generation_join()
    assert forward.join_output == reverse.join_output
    assert forward.join_output["selected_families"] == _ALL_FAMILIES
    assert reverse.join_output["selected_families"] == _ALL_FAMILIES
