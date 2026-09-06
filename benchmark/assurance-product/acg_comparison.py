"""Compare paired ACG initial-selection benchmark results."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

PAIR_KEYS = (
    "requirement_digest",
    "goal_baseline_digest",
    "policy_digest",
    "budget_digest",
    "model_id",
    "tool_versions",
    "environment_digest",
)
COST_KEYS = (
    "first_round_tokens",
    "first_round_tool_calls",
    "first_round_seconds",
    "first_round_cost",
    "total_tokens",
    "total_tool_calls",
    "total_seconds",
    "total_cost",
    "coverage_rounds",
    "healing_rounds",
)
ARMS = frozenset({"select", "all_admissible"})


def _known_condition(key: str, value: object) -> bool:
    if key == "tool_versions":
        return (
            isinstance(value, Mapping)
            and bool(value)
            and all(
                isinstance(name, str)
                and bool(name.strip())
                and isinstance(version, str)
                and bool(version.strip())
                for name, version in value.items()
            )
        )
    return isinstance(value, str) and bool(value.strip())


def require_comparable(left: Mapping[str, object], right: Mapping[str, object]) -> None:
    """Reject a pair whose conditions or arms do not describe one experiment."""
    for key in PAIR_KEYS:
        if not all(_known_condition(key, row.get(key)) for row in (left, right)):
            raise ValueError(f"comparison condition is missing or invalid: {key}")
        if left[key] != right[key]:
            raise ValueError(f"comparison conditions differ: {key}")
    if {left.get("arm"), right.get("arm")} != ARMS:
        raise ValueError("comparison requires both arms")


def _numeric_delta(select: Mapping[str, object], baseline: Mapping[str, object], key: str) -> float | None:
    selected = select.get(key)
    all_admissible = baseline.get(key)
    if (
        not isinstance(selected, int | float)
        or isinstance(selected, bool)
        or not isinstance(all_admissible, int | float)
        or isinstance(all_admissible, bool)
    ):
        return None
    return float(selected) - float(all_admissible)


def _paired_rows(rows: Sequence[Mapping[str, object]]) -> dict[str, dict[str, Mapping[str, object]]]:
    pairs: dict[str, dict[str, Mapping[str, object]]] = {}
    for row in rows:
        pair_id = row.get("pair_id")
        arm = row.get("arm")
        if not isinstance(pair_id, str) or not pair_id:
            raise ValueError("comparison row requires pair_id")
        if arm not in ARMS:
            raise ValueError("comparison row has an unknown arm")
        pair = pairs.setdefault(pair_id, {})
        if arm in pair:
            raise ValueError(f"comparison pair has duplicate arm: {pair_id}/{arm}")
        pair[str(arm)] = row
    for pair_id, pair in pairs.items():
        if set(pair) != ARMS:
            raise ValueError(f"comparison pair is incomplete: {pair_id}")
    return pairs


def compare_pairs(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Return per-pair deltas and aggregates without coercing unknown costs to zero."""
    pairs = _paired_rows(rows)
    compared: list[dict[str, object]] = []
    known: dict[str, list[float]] = {key: [] for key in COST_KEYS}
    for pair_id in sorted(pairs):
        select = pairs[pair_id]["select"]
        baseline = pairs[pair_id]["all_admissible"]
        require_comparable(select, baseline)
        deltas = {key: _numeric_delta(select, baseline, key) for key in COST_KEYS}
        for key, value in deltas.items():
            if value is not None:
                known[key].append(value)
        compared.append(
            {
                "pair_id": pair_id,
                "conditions": {key: select[key] for key in PAIR_KEYS},
                "plans": {
                    arm: {
                        "plan": pairs[pair_id][arm].get("plan"),
                        "plan_ref": pairs[pair_id][arm].get("plan_ref"),
                    }
                    for arm in sorted(ARMS)
                },
                "terminal_outcomes": {
                    arm: pairs[pair_id][arm].get("terminal_outcome") for arm in sorted(ARMS)
                },
                "inspect_metrics": {arm: pairs[pair_id][arm].get("inspect_metrics") for arm in sorted(ARMS)},
                "family_conflicts": {
                    arm: pairs[pair_id][arm].get("family_conflicts") for arm in sorted(ARMS)
                },
                "deltas": deltas,
            }
        )
    return {
        "pair_count": len(compared),
        "pairs": compared,
        "mean_deltas": {key: fmean(values) if values else None for key, values in known.items()},
        "unknown_cost_counts": {key: len(compared) - len(values) for key, values in known.items()},
    }


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("rows", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raw: Any = json.loads(args.rows.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise SystemExit("comparison input must be a JSON array of objects")
    result = compare_pairs(raw)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
