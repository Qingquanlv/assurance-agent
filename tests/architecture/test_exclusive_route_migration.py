from __future__ import annotations

from collections import Counter

import pytest

from assurance_product.graphs.routes import PRODUCT_EXCLUSIVE_ROUTES
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
    assert len(EXCLUSIVE_ROUTE_INVENTORY) == 51
    assert kinds == {"task": 13, "subgraph": 21, "interrupt": 11, "gate": 6}
    assert owners == {"feature": 42, "product": 9}
    assert all(row.target_test for row in EXCLUSIVE_ROUTE_INVENTORY)
    assert all(row.otherwise_target for row in EXCLUSIVE_ROUTE_INVENTORY)
    assert all("pending" not in row.target_test for row in EXCLUSIVE_ROUTE_INVENTORY)
    product_rows = [row for row in EXCLUSIVE_ROUTE_INVENTORY if row.owner == "product"]
    assert len(product_rows) == 9
    assert all(row.node_id in PRODUCT_EXCLUSIVE_ROUTES for row in product_rows)


@pytest.mark.parametrize(
    "row",
    EXCLUSIVE_ROUTE_INVENTORY,
    ids=lambda row: f"{row.graph_id}/{row.node_id}",
)
def test_exclusive_route_zero_and_ambiguous_matches(row: ExclusiveRouteRow) -> None:
    assert select_exclusive_route({}, otherwise=row.otherwise_target) == row.otherwise_target
    if row.owner == "product":
        assert PRODUCT_EXCLUSIVE_ROUTES[row.node_id]({}) == row.otherwise_target
    with pytest.raises(AmbiguousRouteMatch):
        select_exclusive_route(
            {"first": row.otherwise_target, "second": f"{row.otherwise_target}-alt"},
            otherwise=row.otherwise_target,
        )
