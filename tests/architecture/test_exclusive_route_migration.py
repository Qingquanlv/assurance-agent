from __future__ import annotations

from collections import Counter

import pytest

from graph_engine.stategraph.routing import AmbiguousRouteMatch, select_exclusive_route
from tests.architecture.exclusive_route_inventory import (
    EXCLUSIVE_ROUTE_INVENTORY,
    ExclusiveRouteRow,
    collect_legacy_exclusive_routes,
)


def test_exclusive_route_inventory_matches_legacy_compiler() -> None:
    frozen = {row.key() for row in EXCLUSIVE_ROUTE_INVENTORY}
    live = collect_legacy_exclusive_routes()
    assert frozen == live
    kinds = Counter(row.node_kind for row in EXCLUSIVE_ROUTE_INVENTORY)
    owners = Counter(row.owner for row in EXCLUSIVE_ROUTE_INVENTORY)
    assert len(EXCLUSIVE_ROUTE_INVENTORY) == 45
    assert kinds == {"task": 12, "subgraph": 17, "interrupt": 11, "gate": 5}
    assert owners == {"feature": 40, "product": 5}
    assert all(row.target_test for row in EXCLUSIVE_ROUTE_INVENTORY)
    assert all(row.otherwise_target for row in EXCLUSIVE_ROUTE_INVENTORY)
    assert all("pending" not in row.target_test for row in EXCLUSIVE_ROUTE_INVENTORY)
    product_rows = [row for row in EXCLUSIVE_ROUTE_INVENTORY if row.owner == "product"]
    assert len(product_rows) == 5


@pytest.mark.parametrize(
    "row",
    EXCLUSIVE_ROUTE_INVENTORY,
    ids=lambda row: f"{row.graph_id}/{row.node_id}",
)
def test_exclusive_route_zero_and_ambiguous_matches(row: ExclusiveRouteRow) -> None:
    assert select_exclusive_route({}, otherwise=row.otherwise_target) == row.otherwise_target
    with pytest.raises(AmbiguousRouteMatch):
        select_exclusive_route(
            {"first": row.otherwise_target, "second": f"{row.otherwise_target}-alt"},
            otherwise=row.otherwise_target,
        )
