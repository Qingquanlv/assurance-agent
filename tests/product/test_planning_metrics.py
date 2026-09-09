from __future__ import annotations

import json
from pathlib import Path
import runpy


def test_planning_metrics_keep_failed_and_incomplete_runs_visible(tmp_path: Path) -> None:
    module = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "benchmark/assurance-product/planning_metrics.py")
    )
    evidence = tmp_path / "evidence.json"
    change = tmp_path / "change"
    evidence.write_text(
        json.dumps(
            {
                "change_root": str(change),
                "change_id": "CH-1",
                "outcome": "blocked",
                "started_at": "2026-09-08T00:00:00Z",
                "ended_at": "2026-09-08T00:02:00Z",
            }
        )
    )
    empty = module["summarize_run"](evidence)
    assert empty["outcome"] == "blocked"
    assert empty["loops"] == []
    assert empty["total_tokens"] is None
    assert empty["wall_seconds"] == 120
    rounds = change / "cases/reviews/epochs/0/rounds"
    rounds.mkdir(parents=True)

    def write_round(index: int, outcome: str) -> None:
        (rounds / f"{index}.json").write_text(
            json.dumps(
                {
                    "loop_kind": "case_review",
                    "family": None,
                    "coverage_epoch": 0,
                    "round_index": index,
                    "outcome": outcome,
                }
            )
        )

    write_round(1, "pass")
    missing_first = module["summarize_run"](evidence)["loops"][0]
    assert missing_first["history_complete"] is False
    assert missing_first["pass_within_one_repair"] is None
    write_round(0, "needs_fix")
    complete = module["summarize_run"](evidence)["loops"][0]
    assert complete["first_review_pass"] is False
    assert complete["pass_within_one_repair"] is True
