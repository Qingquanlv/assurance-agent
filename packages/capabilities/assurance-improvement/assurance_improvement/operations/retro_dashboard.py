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
    DashboardCandidate,
    DashboardDomain,
    DashboardDomainName,
    DashboardMetric,
    DashboardRun,
    DashboardSignal,
    DashboardSourceRefs,
    DashboardStages,
    DashboardVerification,
    RetroDashboardV1,
)
from assurance_improvement.contracts.improvements import (
    ImprovementCandidateV3,
    ImprovementLedgerProjection,
    ImprovementSourceRefs,
    ImprovementVerification,
)
from assurance_improvement.contracts.retro import (
    ContextSignalSet,
    DomainAnalysisStatus,
    RetroContextV3,
    RetroRunStatus,
    RetroSourceDescriptor,
    Signal,
)

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


_DOMAIN_ORDER: tuple[DashboardDomainName, ...] = ("issue", "workflow", "eval", "discovery", "coverage_gap")
_SOURCE_FIELD_BY_DOMAIN: dict[DashboardDomainName, tuple[str, str]] = {
    "issue": ("issue_sources", "issue_slice_sha256"),
    "workflow": ("workflow_sources", "workflow_slice_sha256"),
    "eval": ("eval_sources", "eval_slice_sha256"),
    "discovery": ("discovery_sources", "discovery_slice_sha256"),
    "coverage_gap": ("coverage_gap_sources", "coverage_gap_slice_sha256"),
}


def _map_source_refs(refs: ImprovementSourceRefs) -> DashboardSourceRefs:
    return DashboardSourceRefs(
        problem_ids=refs.problem_ids,
        occurrence_ids=refs.occurrence_ids,
        issue_event_ids=refs.issue_event_ids,
        workflow_evidence_ids=refs.workflow_evidence_ids,
        eval_run_ids=refs.eval_run_ids,
    )


def _source_kind_counts(sources: tuple[RetroSourceDescriptor, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in sources:
        counts[source.kind] = counts.get(source.kind, 0) + 1
    return counts


def _build_domains(context: RetroContextV3) -> tuple[DashboardDomain, ...]:
    domains: list[DashboardDomain] = []
    for domain in _DOMAIN_ORDER:
        status_entry: DomainAnalysisStatus | None = getattr(context.domain_status, domain)
        sources_field, slice_field = _SOURCE_FIELD_BY_DOMAIN[domain]
        sources: tuple[RetroSourceDescriptor, ...] = getattr(context.source_manifest, sources_field)
        slice_sha256: str | None = getattr(context.source_manifest, slice_field)
        source_count = len(sources)
        source_kinds = _source_kind_counts(sources)
        if status_entry is None:
            domains.append(
                DashboardDomain(
                    domain=domain,
                    status="absent",
                    failure_reason=None,
                    signal_count=None,
                    source_count=source_count,
                    source_kinds=source_kinds,
                    slice_sha256=slice_sha256,
                )
            )
            continue
        signal_tuple = getattr(context.signals, domain)
        domains.append(
            DashboardDomain(
                domain=domain,
                status=status_entry.status,
                failure_reason=status_entry.failure_reason,
                signal_count=len(signal_tuple),
                source_count=source_count,
                source_kinds=source_kinds,
                slice_sha256=slice_sha256,
            )
        )
    return tuple(domains)


def _cited_by_index(candidates: tuple[ImprovementCandidateV3, ...]) -> dict[str, tuple[str, ...]]:
    index: dict[str, list[str]] = {}
    for candidate in candidates:
        for signal_id in candidate.signal_ids:
            index.setdefault(signal_id, []).append(candidate.candidate_id)
    return {signal_id: tuple(ids) for signal_id, ids in index.items()}


def _flatten_signals(
    signal_set: ContextSignalSet, cited_by: dict[str, tuple[str, ...]]
) -> tuple[DashboardSignal, ...]:
    flat: list[DashboardSignal] = []
    for domain in _DOMAIN_ORDER:
        for signal in getattr(signal_set, domain):
            flat.append(
                DashboardSignal(
                    signal_id=signal.signal_id,
                    signal_type=signal.signal_type,
                    domain=domain,
                    summary=signal.summary,
                    occurrence_count=signal.occurrence_count,
                    confidence=signal.confidence,
                    recommended_change=signal.recommended_change,
                    metrics=project_signal_metrics(signal),
                    source_refs=_map_source_refs(signal.source_refs),
                    cited_by_candidate_ids=cited_by.get(signal.signal_id, ()),
                )
            )
    return tuple(sorted(flat, key=lambda item: (-item.occurrence_count, item.signal_id)))


def _map_verification(verification: ImprovementVerification) -> DashboardVerification:
    return DashboardVerification(
        suites=verification.suites,
        required_cases=verification.required_cases,
        success_criteria=verification.success_criteria,
    )


def _build_candidates(
    candidates: tuple[ImprovementCandidateV3, ...],
    improvement_ids: tuple[str, ...],
    ledger: ImprovementLedgerProjection | None,
) -> tuple[tuple[DashboardCandidate, ...], tuple[str, ...]]:
    built: list[DashboardCandidate] = []
    join_mismatch = False
    for index, candidate in enumerate(candidates):
        improvement_id = improvement_ids[index] if index < len(improvement_ids) else None
        resolved_improvement_id: str | None = None
        state: str | None = None
        version: int | None = None
        if improvement_id is not None and ledger is not None:
            projection = ledger.improvements.get(improvement_id)
            if projection is not None:
                if (
                    projection.kind == candidate.kind
                    and projection.delivery == candidate.delivery
                    and projection.target == candidate.target
                ):
                    resolved_improvement_id = improvement_id
                    state = projection.state.value
                    version = projection.version
                else:
                    join_mismatch = True
        built.append(
            DashboardCandidate(
                candidate_id=candidate.candidate_id,
                kind=candidate.kind.value,
                delivery=candidate.delivery.value,
                target=candidate.target,
                rationale=candidate.rationale,
                proposed_change=candidate.proposed_change,
                risk=candidate.risk,
                confidence=candidate.confidence,
                signal_ids=candidate.signal_ids,
                verification=_map_verification(candidate.verification),
                source_refs=_map_source_refs(candidate.source_refs),
                has_knowledge_delta=candidate.knowledge_delta is not None,
                supersedes=candidate.supersedes,
                improvement_id=resolved_improvement_id,
                improvement_state=state,
                improvement_version=version,
            )
        )
    reasons = ("improvement_join_mismatch",) if join_mismatch else ()
    return tuple(built), reasons


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
    candidates: tuple[ImprovementCandidateV3, ...] = candidates_result.model or ()
    if candidates_result.reason:
        integrity_reasons.append(candidates_result.reason)

    ledger_result = _load_model(
        change_root / _LEDGER_PATH, ImprovementLedgerProjection.model_validate_json, "ledger.json"
    )
    ledger = ledger_result.model
    if ledger_result.reason:
        integrity_reasons.append(ledger_result.reason)

    if change_id is not None and context is not None and change_id not in context.window.change_ids:
        raise ValueError(f"--change {change_id} is not part of the retro window")

    domains: tuple[DashboardDomain, ...] = ()
    signals: tuple[DashboardSignal, ...] = ()
    built_candidates: tuple[DashboardCandidate, ...] = ()
    join_reasons: tuple[str, ...] = ()
    if context is not None:
        cited_by = _cited_by_index(candidates)
        signals = _flatten_signals(context.signals, cited_by)
        domains = _build_domains(context)
        built_candidates, join_reasons = _build_candidates(
            candidates, run_status.improvement_ids if run_status is not None else (), ledger
        )

    run = _build_run(context, run_status, len(candidates), tuple(integrity_reasons) + join_reasons)

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
        domains=domains,
        signals=signals,
        candidates=built_candidates,
    )


def _build_run(
    context: RetroContextV3 | None,
    run_status: RetroRunStatus | None,
    candidate_count: int,
    extra_reasons: tuple[str, ...],
) -> DashboardRun:
    if context is None:
        return DashboardRun(integrity_reasons=extra_reasons)
    return DashboardRun(
        result=run_status.result if run_status is not None else None,
        integrity_status=context.integrity.status,
        integrity_reasons=tuple(context.integrity.reasons) + extra_reasons,
        signal_count=context.signal_count,
        candidate_count=candidate_count,
        window_change_ids=context.window.change_ids,
        selection_mode=context.window.selection.mode,
        batch_id=run_status.batch_id if run_status is not None else None,
        failure_ids=run_status.failure_ids if run_status is not None else (),
        improvement_ids=run_status.improvement_ids if run_status is not None else (),
    )
