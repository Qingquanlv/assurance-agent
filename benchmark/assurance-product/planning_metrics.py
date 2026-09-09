"""Read-only planning-loop measurements from existing benchmark evidence.

Usage: uv run python benchmark/assurance-product/planning_metrics.py RUN/evidence.json [...]
Compare repeated runs with the same model, item, inputs and budgets. A smaller
review count is not an improvement if more runs fail or defects escape to Execute.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from typing import Any


def summarize_run(evidence_path: Path) -> dict[str, Any]:
    evidence = json.loads(evidence_path.read_bytes())
    change = Path(evidence["change_root"])
    groups: dict[tuple[str, str | None, int], list[dict[str, Any]]] = defaultdict(list)
    patterns = ("cases/reviews/epochs/*/rounds/*.json", "plan/*/reviews/epochs/*/rounds/*.json")
    for pattern in patterns:
        for path in sorted(change.glob(pattern)):
            item = json.loads(path.read_bytes())
            groups[(item["loop_kind"], item["family"], item["coverage_epoch"])].append(item)
    loops = []
    for (kind, family, epoch), rows in sorted(groups.items()):
        rows.sort(key=lambda item: item["round_index"])
        complete = [row["round_index"] for row in rows] == list(range(len(rows)))
        loops.append(
            {
                "kind": kind,
                "family": family,
                "epoch": epoch,
                "reviews_observed": len(rows),
                "history_complete": complete,
                "outcomes": [row["outcome"] for row in rows],
                "first_review_pass": rows[0]["outcome"] == "pass" if complete else None,
                "pass_within_one_repair": any(row["outcome"] == "pass" for row in rows[:2])
                if complete
                else None,
            }
        )
    started, ended = evidence.get("started_at"), evidence.get("ended_at")
    wall_seconds = (
        (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
        if started and ended
        else None
    )
    return {
        "evidence": str(evidence_path),
        "change_id": evidence["change_id"],
        "model_id": evidence.get("model_id"),
        "lock_digest": evidence.get("lock_digest"),
        "outcome": evidence.get("outcome"),
        "terminal_status": evidence.get("terminal_status"),
        "wall_seconds": wall_seconds,
        "total_tokens": evidence.get("total_tokens"),
        "total_cost": evidence.get("total_cost"),
        "loops": loops,
        "notes": evidence.get("notes"),
        "limitation": "Missing history is unknown, not zero repairs. Defect escape and reviewer false-positive rates require adjudicated findings; not inferred from pass or test failure.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, nargs="+")
    args = parser.parse_args()
    print(json.dumps([summarize_run(path) for path in args.evidence], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
