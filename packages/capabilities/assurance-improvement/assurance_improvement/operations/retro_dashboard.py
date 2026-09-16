"""Read-only projection of retro artifacts into `RetroDashboardV1`.

This module only reads what synthesis/reconcile already produced; it never
re-derives retro semantics (no re-slicing, no re-running analyzers).
"""

from __future__ import annotations

from collections.abc import Callable

from assurance_improvement.contracts.dashboard import DashboardMetric
from assurance_improvement.contracts.retro import Signal

_SHORT_LEN = 12


def _metric(label: str, value: str) -> DashboardMetric:
    return DashboardMetric(label=label, value=value)


def _fmt_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    return f"{value:.4f}"


def _short(value: str) -> str:
    return value[:_SHORT_LEN]


_PROJECTORS: dict[str, Callable[[Signal], tuple[DashboardMetric, ...]]] = {}


def _register(
    signal_type: str,
) -> Callable[
    [Callable[[Signal], tuple[DashboardMetric, ...]]], Callable[[Signal], tuple[DashboardMetric, ...]]
]:
    def decorator(
        fn: Callable[[Signal], tuple[DashboardMetric, ...]],
    ) -> Callable[[Signal], tuple[DashboardMetric, ...]]:
        _PROJECTORS[signal_type] = fn
        return fn

    return decorator


@_register("issue_pattern")
def _project_issue_pattern(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("pattern_kind", str(signal.pattern_kind)),  # type: ignore[union-attr]
        _metric("surface", f"{signal.affected_surface.kind}:{signal.affected_surface.value}"),  # type: ignore[union-attr]
        _metric("symptom", signal.symptom),  # type: ignore[union-attr]
    )


@_register("task_failure")
def _project_task_failure(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("node", signal.node_id),  # type: ignore[union-attr]
        _metric("error_kind", signal.error_kind),  # type: ignore[union-attr]
        _metric("fingerprint", _short(signal.message_fingerprint)),  # type: ignore[union-attr]
    )


@_register("gate_pushback")
def _project_gate_pushback(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("gate", signal.gate_id),  # type: ignore[union-attr]
        _metric("cause", signal.cause),  # type: ignore[union-attr]
    )


@_register("healing")
def _project_healing(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("operation", signal.operation),  # type: ignore[union-attr]
        _metric("outcome", signal.outcome),  # type: ignore[union-attr]
    )


@_register("skill_drift")
def _project_skill_drift(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("phase", signal.phase),  # type: ignore[union-attr]
        _metric("expected_skill", signal.expected_skill),  # type: ignore[union-attr]
    )


@_register("eval_trend")
def _project_eval_trend(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("suite", signal.suite),  # type: ignore[union-attr]
        _metric("verdict", signal.verdict),  # type: ignore[union-attr]
        _metric("consecutive", _fmt_number(signal.consecutive_count)),  # type: ignore[union-attr]
        _metric("signature", _short(signal.failure_signature)),  # type: ignore[union-attr]
        _metric("sample_runs", str(len(signal.sample_run_ids))),  # type: ignore[union-attr]
    )


@_register("low_promotion_rate")
def _project_low_promotion_rate(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("rate", _fmt_number(signal.rate)),  # type: ignore[union-attr]
        _metric("ratio", f"{signal.numerator}/{signal.denominator}"),  # type: ignore[union-attr]
    )


@_register("low_replay_stability")
def _project_low_replay_stability(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("rate", _fmt_number(signal.rate)),  # type: ignore[union-attr]
        _metric("ratio", f"{signal.success}/{signal.attempts}"),  # type: ignore[union-attr]
    )


@_register("confirmed_escape")
def _project_confirmed_escape(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("problem_id", signal.problem_id),  # type: ignore[union-attr]
        _metric("missed_obligations", str(len(signal.missed_obligation_ids))),  # type: ignore[union-attr]
    )


@_register("reopened_coverage_gap")
def _project_reopened_coverage_gap(signal: Signal) -> tuple[DashboardMetric, ...]:
    metrics = [
        _metric("gap_kind", signal.gap_kind),  # type: ignore[union-attr]
        _metric("locator", _short(signal.locator_fingerprint)),  # type: ignore[union-attr]
        _metric("change_id", signal.change_id),  # type: ignore[union-attr]
    ]
    if signal.case_id:  # type: ignore[union-attr]
        metrics.append(_metric("case_id", signal.case_id))  # type: ignore[union-attr]
    if signal.cell:  # type: ignore[union-attr]
        metrics.append(_metric("cell", signal.cell))  # type: ignore[union-attr]
    return tuple(metrics)


@_register("batch_member_evidence_gap")
def _project_batch_member_evidence_gap(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("change_id", signal.change_id),  # type: ignore[union-attr]
        _metric("execution_status", signal.execution_status),  # type: ignore[union-attr]
        _metric("reason_code", signal.reason_code),  # type: ignore[union-attr]
    )


@_register("domain_evidence_gap")
def _project_domain_evidence_gap(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("evidence_domain", signal.domain),  # type: ignore[union-attr]
        _metric("reason_code", signal.reason_code),  # type: ignore[union-attr]
    )


@_register("retro_pipeline_failure")
def _project_retro_pipeline_failure(signal: Signal) -> tuple[DashboardMetric, ...]:
    return (
        _metric("stage", signal.stage),  # type: ignore[union-attr]
        _metric("error_kind", signal.error_kind),  # type: ignore[union-attr]
        _metric("failure_id", signal.failure_id),  # type: ignore[union-attr]
    )


def project_signal_metrics(signal: Signal) -> tuple[DashboardMetric, ...]:
    """Project a signal's discriminant fields into display metrics.

    Unknown `signal_type`s (a newer wheel than this CLI knows about) project
    to an empty tuple rather than raising, so an older CLI never fails closed
    on a signal it does not understand.
    """
    projector = _PROJECTORS.get(signal.signal_type)
    if projector is None:
        return ()
    return projector(signal)
