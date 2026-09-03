from __future__ import annotations

from itertools import permutations

from assurance_generation.contracts.families import GENERATION_FAMILIES
from assurance_product.graphs.execute import complete_parallel_generation
from assurance_product.graphs.state import (
    GenerationLaneResult,
    make_generation_lane_result,
    merge_generation_receipts,
    merge_generation_results,
)


def _result(family: str, *, selected: bool = True, receipt_id: str | None = None) -> GenerationLaneResult:
    return make_generation_lane_result(
        family=family,
        receipt_id=receipt_id or f"receipt-{family}",
        selected=selected,
        status="passed" if selected else "skipped",
    )


def _receipt(family: str) -> dict[str, object]:
    return {"receipt_id": f"receipt-{family}", "receipt_digest": "a" * 64, "family": family}


def test_parallel_generation_results_merge_order_independently() -> None:
    results = [_result(family) for family in GENERATION_FAMILIES]
    expected = merge_generation_results([], results)
    for order in permutations(results):
        acc: list[GenerationLaneResult] = []
        for item in order:
            acc = merge_generation_results(acc, [item])
        assert acc == expected
    replayed = merge_generation_results(expected, [_result("api")])
    assert replayed == expected
    assert [item["family"] for item in expected] == list(GENERATION_FAMILIES)


def test_parallel_generation_receipts_merge_order_independently() -> None:
    receipts = [_receipt(family) for family in GENERATION_FAMILIES]
    expected = merge_generation_receipts([], receipts)
    for order in permutations(receipts):
        acc: list[dict[str, object]] = []
        for item in order:
            acc = merge_generation_receipts(acc, [item])
        assert acc == expected
    replayed = merge_generation_receipts(expected, [_receipt("api")])
    assert replayed == expected


def test_four_family_superstep_does_not_write_concurrent_scalar_keys() -> None:
    results = [_result(family, selected=family in {"api", "e2e"}) for family in GENERATION_FAMILIES]
    receipts = [_receipt(family) for family in GENERATION_FAMILIES]
    merged_results = merge_generation_results([], results)
    merged_receipts = merge_generation_receipts([], receipts)
    for item in merged_results:
        assert set(item) == {"family", "receipt_id", "selected", "status"}
    complete = complete_parallel_generation(
        {
            "generation_results": merged_results,
            "generation_receipts": merged_receipts,
            "selected_test_families": ["api", "e2e"],
        }
    )
    assert "status" not in complete
    assert "decision" not in complete
    assert set(complete) <= {
        "families",
        "selected_families",
        "receipts",
        "generation_results",
        "generation_receipts",
    }
    assert complete["selected_families"] == ["api", "e2e"]
