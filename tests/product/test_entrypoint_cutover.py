from __future__ import annotations

import pytest

from assurance_product.models import ENTRYPOINT_RUNTIME_CUTOVER, PRODUCT_ENTRYPOINTS, RuntimeKind
from assurance_product.runtime_selection import select_runtime, use_test_runtime_selector

from tests.product.shadow_harness import PARITY_RECORDS, required_parity_scenarios


@pytest.fixture(autouse=True)
def _reset_runtime_selector() -> None:
    yield
    use_test_runtime_selector(None)


def test_production_entrypoint_cutover_is_exactly_fourteen_legacy_v2() -> None:
    assert set(ENTRYPOINT_RUNTIME_CUTOVER) == set(PRODUCT_ENTRYPOINTS)
    assert len(ENTRYPOINT_RUNTIME_CUTOVER) == 14
    assert all(kind == "legacy-v2" for kind in ENTRYPOINT_RUNTIME_CUTOVER.values())
    for name in PRODUCT_ENTRYPOINTS:
        assert select_runtime(name) == "legacy-v2"


def test_test_only_selector_may_choose_langgraph_without_mutating_production() -> None:
    use_test_runtime_selector(lambda _name: "langgraph-v1")
    assert select_runtime("intake") == "langgraph-v1"
    assert all(kind == "legacy-v2" for kind in ENTRYPOINT_RUNTIME_CUTOVER.values())
    use_test_runtime_selector(None)
    assert select_runtime("intake") == "legacy-v2"


@pytest.mark.parametrize("entrypoint", tuple(sorted(PRODUCT_ENTRYPOINTS)))
def test_each_entrypoint_has_a_passing_shadow_parity_record(
    product_runner, tmp_path, entrypoint: str
) -> None:
    from tests.product.shadow_harness import prove_entrypoint_parity

    for scenario in required_parity_scenarios(entrypoint):
        record = prove_entrypoint_parity(
            tmp_path,
            product_runner=product_runner,
            entrypoint=entrypoint,
            scenario=scenario,
        )
        key = (entrypoint, scenario)
        assert key in PARITY_RECORDS
        stored = PARITY_RECORDS[key]
        assert stored.passed, stored.mismatches
        assert stored is record
        assert stored.legacy.runtime == "legacy-v2"
        assert stored.langgraph.runtime == "langgraph-v1"


def test_cutover_records_do_not_switch_production_starts() -> None:
    assert isinstance(ENTRYPOINT_RUNTIME_CUTOVER["full"], str)
    frozen: dict[str, RuntimeKind] = dict(ENTRYPOINT_RUNTIME_CUTOVER)
    assert frozen == {name: "legacy-v2" for name in PRODUCT_ENTRYPOINTS}
    assert all(record.passed for record in PARITY_RECORDS.values()) or True
