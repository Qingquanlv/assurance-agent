from __future__ import annotations

import runpy
from pathlib import Path

import pytest

MODULE = runpy.run_path(str(Path(__file__).parents[2] / "benchmark/assurance-product/acg_comparison.py"))
PAIR_KEYS = MODULE["PAIR_KEYS"]
require_comparable = MODULE["require_comparable"]
compare_pairs = MODULE["compare_pairs"]


def _row(pair_id: str, arm: str, **updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "pair_id": pair_id,
        "arm": arm,
        **dict.fromkeys(PAIR_KEYS, "same"),
        "plan": {"selected_test_families": ["api"]},
        "plan_ref": {"path": "plan.json", "digest": "a" * 64},
        "terminal_outcome": "achieved",
        "inspect_metrics": {"coverage": 1.0},
        "family_conflicts": [],
        "first_round_tokens": 100,
        "total_tokens": 150,
    }
    row.update(updates)
    return row


@pytest.mark.parametrize("key", PAIR_KEYS)
def test_comparison_rejects_a_different_condition(key: str) -> None:
    left = _row("pair-1", "select")
    right = _row("pair-1", "all_admissible", **{key: "different"})

    with pytest.raises(ValueError, match=key):
        require_comparable(left, right)


@pytest.mark.parametrize("key", PAIR_KEYS)
def test_comparison_rejects_a_missing_condition(key: str) -> None:
    left = _row("pair-1", "select")
    right = _row("pair-1", "all_admissible")
    del right[key]

    with pytest.raises(ValueError, match=key):
        require_comparable(left, right)


def test_comparison_requires_one_row_from_each_arm() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        compare_pairs([_row("pair-1", "select")])
    with pytest.raises(ValueError, match="duplicate arm"):
        compare_pairs([_row("pair-1", "select"), _row("pair-1", "select")])


def test_comparison_reports_cost_deltas_and_preserves_unknowns() -> None:
    select = _row("pair-1", "select", first_round_tokens=80, total_tokens=None)
    baseline = _row("pair-1", "all_admissible", first_round_tokens=100, total_tokens=200)

    result = compare_pairs([baseline, select])

    assert result["pair_count"] == 1
    pair = result["pairs"][0]
    assert pair["deltas"]["first_round_tokens"] == -20.0
    assert pair["deltas"]["total_tokens"] is None
    assert result["mean_deltas"]["first_round_tokens"] == -20.0
    assert result["mean_deltas"]["total_tokens"] is None
    assert result["unknown_cost_counts"]["total_tokens"] == 1
