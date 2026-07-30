from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.eval.types import (
    EvalGateResult,
    EvalSuite,
    RunManifest,
    SuiteMetrics,
    SuiteThreshold,
)

_OPS = {
    "gte": lambda v, t: v >= t,
    "lte": lambda v, t: v <= t,
    "eq": lambda v, t: v == t,
}


def _passes(value: float, threshold: SuiteThreshold) -> bool:
    return _OPS[threshold.op](value, threshold.value)


def _evidence_ok(manifest: RunManifest, metrics: SuiteMetrics) -> bool:
    return (
        manifest.executed_samples == manifest.total_samples
        and metrics.run_id == manifest.run_id
        and metrics.sample_count == len(manifest.selected_sample_ids)
    )


def compute_gate_result(suite: EvalSuite, manifest: RunManifest, metrics: SuiteMetrics) -> EvalGateResult:
    hard_failures: list[str] = []
    warnings: list[str] = []
    threshold_failures: list[str] = []
    inconclusive = 0
    checked: list[str] = []

    if not _evidence_ok(manifest, metrics):
        hard_failures.append("evidence_integrity: manifest/metrics cross-check failed")
    if metrics.error_count:
        inconclusive += metrics.error_count
        threshold_failures.append(f"sample_execution_errors: {metrics.error_count}")

    for threshold in suite.thresholds:
        if threshold.gate == "observe":
            continue
        checked.append(threshold.metric)
        if threshold.metric not in metrics.metrics:
            inconclusive += 1
            threshold_failures.append(f"{threshold.metric}: missing")
            continue
        value = metrics.metrics[threshold.metric]
        if _passes(value, threshold):
            continue
        detail = f"{threshold.metric}: {value} !{threshold.op} {threshold.value}"
        threshold_failures.append(detail)
        if threshold.gate == "hard":
            hard_failures.append(threshold.metric)
        else:
            warnings.append(threshold.metric)

    if hard_failures and any(f.startswith("evidence_integrity") for f in hard_failures):
        verdict = "inconclusive"
    elif hard_failures:
        verdict = "fail"
    elif inconclusive:
        verdict = "inconclusive"
    elif warnings:
        verdict = "pass_with_warnings"
    else:
        verdict = "pass"

    return EvalGateResult(
        run_id=manifest.run_id,
        suite=suite.name,
        verdict=verdict,
        hard_gate_failures=hard_failures,
        threshold_failures=threshold_failures,
        warnings=warnings,
        inconclusive_count=inconclusive,
        checked=checked,
    )


def write_gate_result(run_dir: Path, result: EvalGateResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "gate-result.json").write_text(json.dumps(result.model_dump(), indent=2), encoding="utf-8")


def read_gate_result(run_dir: Path) -> EvalGateResult:
    data = json.loads((run_dir / "gate-result.json").read_text(encoding="utf-8"))
    return EvalGateResult.model_validate(data)
