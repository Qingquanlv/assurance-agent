from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from assurance_agent.eval.baseline import BaselineSuiteEntry
    from assurance_agent.eval.types import EvalSuite, RunManifest


_VERDICT_SEVERITY = {
    "pass": 0,
    "pass_with_warnings": 1,
    "inconclusive": 2,
    "needs_human_review": 3,
    "fail": 4,
    "error": 5,
}


def _normalized_verdict(verdict: str) -> str:
    """Fail closed when a producer emits an unknown verdict."""
    return verdict if verdict in _VERDICT_SEVERITY else "error"


def worst_verdict(verdicts: Iterable[str]) -> str:
    """Return the most severe verdict, independent of suite execution order."""
    normalized = (_normalized_verdict(verdict) for verdict in verdicts)
    return max(normalized, key=_VERDICT_SEVERITY.__getitem__, default="pass")


def gate_exit_code(verdicts: Iterable[str]) -> int:
    """Fail the deterministic benchmark gate on an actual failure or error.

    ``inconclusive`` remains visible in the summary but is not treated as an
    engine regression: several workflow suites intentionally omit optional
    metrics when their golden fixture does not exercise that stage.
    """
    return int(worst_verdict(verdicts) in {"fail", "error"})


def baseline_regression_verdict(
    *,
    suite: EvalSuite,
    manifest: RunManifest,
    current_metrics: Mapping[str, float],
    baseline: BaselineSuiteEntry | None,
    gate_verdict: str,
    hard_gate_failures: Sequence[str] = (),
) -> str:
    """Combine an absolute suite gate with its declared baseline policy."""
    normalized = _normalized_verdict(gate_verdict)
    if normalized == "error":
        return "error"
    if normalized in {"fail", "needs_human_review"} or hard_gate_failures:
        return "fail"
    if normalized == "inconclusive":
        return "inconclusive"
    policy = suite.regression
    if baseline is None or policy is None:
        return "inconclusive"
    from assurance_agent.eval.types import regression_policy_sha256

    current_policy_sha256 = regression_policy_sha256(policy)
    if (
        manifest.suite != suite.name
        or manifest.suite_version != baseline.suite_version
        or manifest.suite_version != suite.version
        or manifest.repeat != baseline.repeat
        or manifest.repeat != policy.repeat
        or manifest.regression_policy_sha256 != baseline.regression_policy_sha256
        or manifest.regression_policy_sha256 != current_policy_sha256
    ):
        return "inconclusive"

    for metric, rule in policy.metrics.items():
        current = current_metrics.get(metric)
        base = baseline.metrics.get(metric)
        if current is None or base is None:
            return "inconclusive"
        if not math.isfinite(current) or not math.isfinite(base):
            return "error"
        degradation = base - current if rule.direction == "higher_is_better" else current - base
        if degradation > rule.max_regression + 1e-12:
            return "fail"
    return normalized


def run_regression_verdict(*, engine_root: Path, sut_root: Path, run_id: str) -> str:
    """Read one completed run and evaluate its absolute and baseline gates."""
    from assurance_agent.eval.baseline import read_baseline, read_run_manifest
    from assurance_agent.eval.gate import read_gate_result
    from assurance_agent.eval.metrics import read_metrics
    from assurance_agent.eval.paths import run_dir as run_dir_for
    from assurance_agent.eval.plan import load_suite
    from assurance_agent.exceptions import AaError

    run_dir = run_dir_for(sut_root, run_id)
    manifest = read_run_manifest(run_dir)
    metrics = read_metrics(run_dir)
    gate = read_gate_result(run_dir)
    suite, _ = load_suite(engine_root, manifest.suite)
    if (
        metrics.run_id != run_id
        or metrics.suite != manifest.suite
        or gate.run_id != run_id
        or gate.suite != manifest.suite
    ):
        raise AaError(f"eval run artifacts disagree for {run_id}")
    return baseline_regression_verdict(
        suite=suite,
        manifest=manifest,
        current_metrics=metrics.metrics,
        baseline=read_baseline(engine_root).get(manifest.suite),
        gate_verdict=gate.verdict,
        hard_gate_failures=gate.hard_gate_failures,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate one run against its approved baseline")
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--sut-root", type=Path, required=True)
    parser.add_argument("--run", dest="run_id", required=True)
    args = parser.parse_args(argv)
    try:
        verdict = run_regression_verdict(
            engine_root=args.engine_root.resolve(),
            sut_root=args.sut_root.resolve(),
            run_id=args.run_id,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary must fail closed
        print(f"eval regression check failed: {exc}", file=sys.stderr)
        return 1
    print(verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "baseline_regression_verdict",
    "gate_exit_code",
    "run_regression_verdict",
    "worst_verdict",
]
