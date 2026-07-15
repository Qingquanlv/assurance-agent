from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.retro.types import EvalTrendSignal


def read_eval_trend(project_root: Path, *, suites: list[str] | None = None) -> list[EvalTrendSignal]:
    runs = project_root / "eval" / "out" / "runs"
    if not runs.is_dir():
        return []
    signals: list[EvalTrendSignal] = []
    for run in sorted(runs.iterdir()):
        report = run / "report.json"
        if not report.exists():
            continue
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        suite = data.get("suite", "")
        if suites and suite not in suites:
            continue
        signals.append(EvalTrendSignal(
            suite=suite, run_id=data.get("run_id", run.name),
            verdict=data.get("verdict", "unknown"),
            started_at=data.get("started_at", ""),
        ))
    return signals
