"""Read-only projection of retro artifacts into `RetroDashboardV1`.

This module only reads what synthesis/reconcile already produced; it never
re-derives retro semantics (no re-slicing, no re-running analyzers).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import ValidationError

from assurance_improvement.contracts.agent import RetroAnalysisResultV3
from assurance_improvement.contracts.dashboard import (
    DashboardMetric,
    DashboardRun,
    DashboardStages,
    RetroDashboardV1,
)
from assurance_improvement.contracts.improvements import ImprovementCandidateV3, ImprovementLedgerProjection
from assurance_improvement.contracts.retro import RetroContextV3, RetroRunStatus, Signal

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


_RETRO_DIR = "qa/results/retro"
_LEDGER_PATH = "qa/improvements/ledger.json"
_ANALYSIS_FILES = (
    "retro-eval-analysis.json",
    "retro-issue-analysis.json",
    "retro-workflow-analysis.json",
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class _LoadResult(Generic[_T]):
    exists: bool
    model: _T | None
    reason: str | None


def _load_model(path: Path, loader: Callable[[str], _T], artifact_name: str) -> _LoadResult[_T]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _LoadResult(exists=False, model=None, reason=None)
    except OSError:
        return _LoadResult(exists=True, model=None, reason=f"artifact_unreadable:{artifact_name}")
    try:
        return _LoadResult(exists=True, model=loader(text), reason=None)
    except (ValidationError, ValueError, json.JSONDecodeError):
        return _LoadResult(exists=True, model=None, reason=f"artifact_unreadable:{artifact_name}")


def _load_candidates(text: str) -> tuple[ImprovementCandidateV3, ...]:
    raw = json.loads(text)
    items = raw.get("candidates", []) if isinstance(raw, dict) else []
    return tuple(ImprovementCandidateV3.model_validate(item) for item in items)


def build_retro_dashboard(change_root: Path, *, change_id: str | None = None) -> RetroDashboardV1:
    """Project the current change's retro artifacts into `RetroDashboardV1`.

    Reads only: the three `retro-*-analysis.json` files (stages.analyses),
    `context.json` (stages.synthesis), `status.json` (stages.reconcile),
    `candidates.json`, and `qa/improvements/ledger.json`. Missing or partial
    artifacts are never an error — they are expressed through `stages` and
    `run.integrity_reasons`. Only an explicit `change_id` that is not part of
    the retro window raises `ValueError`.
    """
    retro_dir = change_root / _RETRO_DIR
    integrity_reasons: list[str] = []

    analyses_ok = True
    for name in _ANALYSIS_FILES:
        result = _load_model(retro_dir / name, RetroAnalysisResultV3.model_validate_json, name)
        if result.model is None:
            analyses_ok = False
            if result.reason:
                integrity_reasons.append(result.reason)

    context_result = _load_model(
        retro_dir / "context.json", RetroContextV3.model_validate_json, "context.json"
    )
    context = context_result.model
    if context_result.reason:
        integrity_reasons.append(context_result.reason)

    status_result = _load_model(retro_dir / "status.json", RetroRunStatus.model_validate_json, "status.json")
    run_status = status_result.model
    if status_result.reason:
        integrity_reasons.append(status_result.reason)

    candidates_result = _load_model(retro_dir / "candidates.json", _load_candidates, "candidates.json")
    # `_candidates` is loaded here (and its integrity reason folded in) so that
    # Tasks 4-5 can populate `RetroDashboardV1.candidates` without re-reading
    # the artifact; this task only needs the integrity side effect below.
    _candidates: tuple[ImprovementCandidateV3, ...] = candidates_result.model or ()
    if candidates_result.reason:
        integrity_reasons.append(candidates_result.reason)

    ledger_result = _load_model(
        change_root / _LEDGER_PATH, ImprovementLedgerProjection.model_validate_json, "ledger.json"
    )
    # Same rationale as `_candidates`: Task 4-5 will consume `_ledger` when
    # deriving improvement linkage; this task only surfaces its integrity
    # reason.
    _ledger = ledger_result.model
    if ledger_result.reason:
        integrity_reasons.append(ledger_result.reason)

    if change_id is not None and context is not None and change_id not in context.window.change_ids:
        raise ValueError(f"--change {change_id} is not part of the retro window")

    run = _build_run(context, run_status, tuple(integrity_reasons))

    resolved_change_id = change_id
    if resolved_change_id is None and context is not None and context.window.change_ids:
        resolved_change_id = context.window.change_ids[0]

    return RetroDashboardV1(
        change_id=resolved_change_id,
        retro_id=context.retro_id if context is not None else None,
        generated_at=context.generated_at if context is not None else None,
        dry_run=context.dry_run if context is not None else False,
        stages=DashboardStages(
            analyses=analyses_ok,
            synthesis=context is not None,
            reconcile=run_status is not None,
        ),
        run=run,
    )


def _build_run(
    context: RetroContextV3 | None,
    run_status: RetroRunStatus | None,
    extra_reasons: tuple[str, ...],
) -> DashboardRun:
    if context is None:
        return DashboardRun(integrity_reasons=extra_reasons)
    return DashboardRun(
        result=run_status.result if run_status is not None else None,
        integrity_status=context.integrity.status,
        integrity_reasons=tuple(context.integrity.reasons) + extra_reasons,
        signal_count=context.signal_count,
        candidate_count=0,
        window_change_ids=context.window.change_ids,
        selection_mode=context.window.selection.mode,
        batch_id=run_status.batch_id if run_status is not None else None,
        failure_ids=run_status.failure_ids if run_status is not None else (),
        improvement_ids=run_status.improvement_ids if run_status is not None else (),
    )
