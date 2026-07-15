from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.eval.types import SampleScore, SuiteMetrics


def aggregate_scores(run_id: str, suite: str, scores: list[SampleScore]) -> SuiteMetrics:
    per_sample: dict[str, dict[str, float]] = {}
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for score in scores:
        per_sample[score.sample_id] = dict(score.metrics)
        if score.status != "ok":
            continue
        for name, value in score.metrics.items():
            sums[name] = sums.get(name, 0.0) + float(value)
            counts[name] = counts.get(name, 0) + 1
    metrics = {name: sums[name] / counts[name] for name in sums if counts[name]}
    return SuiteMetrics(
        run_id=run_id,
        suite=suite,
        sample_count=len(scores),
        metrics=metrics,
        per_sample=per_sample,
    )


def write_metrics(run_dir: Path, metrics: SuiteMetrics) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(json.dumps(metrics.model_dump(), indent=2), encoding="utf-8")


def read_metrics(run_dir: Path) -> SuiteMetrics:
    data = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    return SuiteMetrics.model_validate(data)
