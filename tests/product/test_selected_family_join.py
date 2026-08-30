from __future__ import annotations

from assurance_generation.contracts.families import GENERATION_FAMILIES

from tests.product.product_runner import FAMILY_TERMINALS
from tests.product.test_generation_branches import _run_execute


def test_all_family_join_is_order_independent() -> None:
    _status, dispatched, forward = _run_execute(GENERATION_FAMILIES, completion_order="forward")
    _status, dispatched_reverse, reverse = _run_execute(GENERATION_FAMILIES, completion_order="reverse")
    assert dispatched == dispatched_reverse == set(GENERATION_FAMILIES)
    assert forward == reverse
    assert list(forward["selected_families"]) == list(GENERATION_FAMILIES)  # type: ignore[index]
    assert tuple(FAMILY_TERMINALS) == ("api-done", "e2e-done", "fuzz-done", "performance-done")
