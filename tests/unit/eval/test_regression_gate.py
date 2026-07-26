from __future__ import annotations

import pytest

from assurance_agent.eval.baseline import BaselineSuiteEntry
from assurance_agent.eval.types import (
    EvalSuite,
    RegressionMetricPolicy,
    RegressionPolicy,
    RunManifest,
)


def _regression_gate():
    try:
        from assurance_agent.eval import regression_gate
    except ImportError:
        pytest.fail("regression gate severity reducer is missing")
    return regression_gate


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        (("pass", "inconclusive", "error", "pass"), "error"),
        (("pass_with_warnings", "fail", "inconclusive"), "fail"),
        (("pass", "pass_with_warnings"), "pass_with_warnings"),
        ((), "pass"),
    ],
)
def test_worst_verdict_uses_severity_not_suite_order(
    verdicts: tuple[str, ...],
    expected: str,
) -> None:
    assert _regression_gate().worst_verdict(verdicts) == expected


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        (("pass", "inconclusive"), 0),
        (("pass", "fail"), 1),
        (("inconclusive", "error"), 1),
        (("pass", "unexpected"), 1),
    ],
)
def test_gate_exit_code_fails_on_fail_error_or_unknown_verdict(
    verdicts: tuple[str, ...],
    expected: int,
) -> None:
    assert _regression_gate().gate_exit_code(verdicts) == expected


def _suite(*, direction: str = "higher_is_better", max_regression: float = 0.0) -> EvalSuite:
    return EvalSuite(
        name="golden",
        version="2",
        scorer="smoke",
        regression=RegressionPolicy(
            repeat=1,
            metrics={
                "quality": RegressionMetricPolicy(
                    direction=direction,  # type: ignore[arg-type]
                    max_regression=max_regression,
                )
            },
        ),
    )


def _manifest(suite: EvalSuite, *, version: str | None = None) -> RunManifest:
    from assurance_agent.eval.types import regression_policy_sha256

    return RunManifest(
        run_id="eval-1",
        suite=suite.name,
        scorer=suite.scorer,
        suite_version=version or suite.version,
        repeat=1,
        regression_policy_sha256=regression_policy_sha256(suite.regression),
        started_at="2026-07-26T00:00:00Z",
    )


def _baseline(suite: EvalSuite, *, quality: float = 1.0) -> BaselineSuiteEntry:
    from assurance_agent.eval.types import regression_policy_sha256

    return BaselineSuiteEntry(
        run_id="eval-base",
        suite_version=suite.version,
        repeat=1,
        regression_policy_sha256=regression_policy_sha256(suite.regression),
        approved_at="2026-07-25T00:00:00Z",
        approved_by="reviewer",
        metrics={"quality": quality},
    )


def test_baseline_regression_turns_absolute_pass_into_fail() -> None:
    suite = _suite()

    verdict = _regression_gate().baseline_regression_verdict(
        suite=suite,
        manifest=_manifest(suite),
        current_metrics={"quality": 0.9},
        baseline=_baseline(suite),
        gate_verdict="pass",
    )

    assert verdict == "fail"


def test_lower_is_better_regression_is_direction_aware() -> None:
    suite = _suite(direction="lower_is_better", max_regression=0.1)

    verdict = _regression_gate().baseline_regression_verdict(
        suite=suite,
        manifest=_manifest(suite),
        current_metrics={"quality": 0.61},
        baseline=_baseline(suite, quality=0.5),
        gate_verdict="pass",
    )

    assert verdict == "fail"


@pytest.mark.parametrize("condition", ["missing_baseline", "version_mismatch", "missing_metric"])
def test_uncomparable_baseline_is_inconclusive(condition: str) -> None:
    suite = _suite()
    baseline = None if condition == "missing_baseline" else _baseline(suite)
    manifest = _manifest(suite, version="1" if condition == "version_mismatch" else None)
    metrics = {} if condition == "missing_metric" else {"quality": 1.0}

    verdict = _regression_gate().baseline_regression_verdict(
        suite=suite,
        manifest=manifest,
        current_metrics=metrics,
        baseline=baseline,
        gate_verdict="pass",
    )

    assert verdict == "inconclusive"


def test_non_regressing_baseline_preserves_warning_verdict() -> None:
    suite = _suite(max_regression=0.1)

    verdict = _regression_gate().baseline_regression_verdict(
        suite=suite,
        manifest=_manifest(suite),
        current_metrics={"quality": 0.9},
        baseline=_baseline(suite),
        gate_verdict="pass_with_warnings",
    )

    assert verdict == "pass_with_warnings"


def test_suite_policy_drift_after_run_is_inconclusive() -> None:
    run_suite = _suite(max_regression=0.0)
    current_suite = _suite(max_regression=0.2)

    verdict = _regression_gate().baseline_regression_verdict(
        suite=current_suite,
        manifest=_manifest(run_suite),
        current_metrics={"quality": 1.0},
        baseline=_baseline(run_suite),
        gate_verdict="pass",
    )

    assert verdict == "inconclusive"
