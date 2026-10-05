from __future__ import annotations

from tests.architecture.loop_scc_inventory import (
    EXPECTED_LOOP_SCC_ANCHORS,
    LOOP_SCC_INVENTORY,
    collect_loop_scc_rows,
)


def test_loop_scc_anchors_match_exactly() -> None:
    live = collect_loop_scc_rows()
    live_anchors = tuple(sorted(row.anchor for row in live))
    frozen_anchors = tuple(sorted(row.anchor for row in LOOP_SCC_INVENTORY))
    assert live_anchors == EXPECTED_LOOP_SCC_ANCHORS
    assert frozen_anchors == EXPECTED_LOOP_SCC_ANCHORS
    assert live_anchors == frozen_anchors
    assert len(live_anchors) == 7
    assert {row.membership for row in live} == {row.membership for row in LOOP_SCC_INVENTORY}
    assert all(row.target_test for row in LOOP_SCC_INVENTORY)
    product = [row for row in LOOP_SCC_INVENTORY if row.graph_id.startswith("assurance.product.")]
    assert {row.anchor for row in product} == {
        ("assurance.product.workflow.graph.execute-tail", "repair"),
        ("assurance.product.workflow.graph.product-full", "coverage-rework"),
    }
    assert all(row.target_test.startswith("tests/product/") for row in product)


def test_loop_scc_count_equality_alone_is_not_acceptance() -> None:
    shifted = (
        EXPECTED_LOOP_SCC_ANCHORS[0],
        EXPECTED_LOOP_SCC_ANCHORS[1],
        EXPECTED_LOOP_SCC_ANCHORS[2],
        EXPECTED_LOOP_SCC_ANCHORS[3],
        EXPECTED_LOOP_SCC_ANCHORS[4],
        EXPECTED_LOOP_SCC_ANCHORS[6],
        EXPECTED_LOOP_SCC_ANCHORS[5],
    )
    assert len(shifted) == len(EXPECTED_LOOP_SCC_ANCHORS)
    assert shifted != EXPECTED_LOOP_SCC_ANCHORS
