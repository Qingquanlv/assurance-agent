"""Pure schema-v2 RetroContext aggregation from typed history readers."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

from assurance_agent.artifacts.models.improvements import ImprovementSourceRefs
from assurance_agent.exceptions import AaError
from assurance_agent.retro.eval_history import EvalEvidenceSlice, EvalHistoryReader
from assurance_agent.retro.types import (
    EvalRetroSignals,
    IssueRetroSignals,
    RetroContext,
    RetroIntegrity,
    RetroSignal,
    RetroSignalSet,
    RetroSourceDescriptor,
    RetroSourceManifest,
    WorkflowRetroSignals,
)
from assurance_agent.retro.window import RetroWindowSelection, resolve_retro_window
from assurance_agent.retro.workflow_history import WorkflowEvidenceSlice, WorkflowHistoryReader
from assurance_agent.workflow.issues.events import (
    ProblemAssessmentConfirmedEvent,
    ProblemDetectedEvent,
    ProblemMarkedNotAnIssueEvent,
    ProblemRegressedEvent,
    ProblemResolvedEvent,
    ProblemVerificationRequestedEvent,
)
from assurance_agent.workflow.issues.history import IssueHistoryReader
from assurance_agent.workflow.issues.history_models import IssueEvidenceSlice

_REVIEW_ACTIONS: dict[str, str] = {
    "problem_assessment_confirmed": "confirm_assessment",
    "problem_work_started": "start_work",
    "problem_marked_not_an_issue": "mark_not_an_issue",
    "problem_risk_accepted": "accept_risk",
    "problem_reopened": "reopen",
    "problem_merged": "merge",
}


class RetroContextImmutableError(AaError):
    """Raised when an existing context.json differs from a newly built context."""


def _signal(
    signal_id: str,
    *,
    source_refs: ImprovementSourceRefs,
    metrics: dict[str, int | float | str],
) -> RetroSignal:
    return RetroSignal(signal_id=signal_id, source_refs=source_refs, metrics=metrics)


def build_source_manifest(
    issue_slice: IssueEvidenceSlice,
    workflow_slice: WorkflowEvidenceSlice,
    eval_slice: EvalEvidenceSlice,
) -> RetroSourceManifest:
    change_evidence: dict[str, set[str]] = {}
    for observation in issue_slice.observations:
        change_evidence.setdefault(observation.change_id, set()).add(observation.observation_id)
    for occurrence in issue_slice.occurrences:
        bucket = change_evidence.setdefault(occurrence.change_id, set())
        bucket.add(occurrence.occurrence_id)
        bucket.update(occurrence.observation_ids)
        bucket.add(occurrence.problem_id)

    problem_evidence: set[str] = set()
    for problem in issue_slice.problem_snapshots:
        problem_evidence.add(problem.problem_id)
        problem_evidence.update(problem.occurrences)
    for event in issue_slice.problem_events:
        problem_evidence.add(event.event_id)
        problem_evidence.add(event.problem_id)
        occurrence_id = getattr(event, "occurrence_id", None)
        if isinstance(occurrence_id, str):
            problem_evidence.add(occurrence_id)

    issue_sources: list[RetroSourceDescriptor] = []
    for source in issue_slice.sources:
        if source.kind == "change_issue_ledger" and source.change_id is not None:
            evidence_ids = tuple(sorted(change_evidence.get(source.change_id, ())))
        else:
            evidence_ids = tuple(sorted(problem_evidence))
        issue_sources.append(
            RetroSourceDescriptor(
                kind=source.kind,
                change_id=source.change_id,
                head_event_id=source.head_event_id,
                sha256=source.sha256,
                evidence_ids=evidence_ids,
            )
        )

    return RetroSourceManifest(
        issue_slice_sha256=issue_slice.digest(),
        issue_sources=tuple(issue_sources),
        workflow_sources=workflow_slice.sources,
        eval_sources=eval_slice.sources,
    )


def combine_integrity(
    issue_slice: IssueEvidenceSlice,
    workflow_slice: WorkflowEvidenceSlice,
    eval_slice: EvalEvidenceSlice,
    *,
    window_reasons: Sequence[str] = (),
) -> RetroIntegrity:
    reasons: list[str] = []
    for reason in window_reasons:
        if reason not in reasons:
            reasons.append(reason)
    for reason in issue_slice.integrity.reasons:
        if reason not in reasons:
            reasons.append(reason)
    for reason in workflow_slice.integrity.reasons:
        if reason not in reasons:
            reasons.append(reason)
    for reason in eval_slice.integrity.reasons:
        if reason not in reasons:
            reasons.append(reason)
    if reasons:
        return RetroIntegrity(status="incomplete", reasons=tuple(reasons))
    return RetroIntegrity(status="complete")


def _iter_signals(signals: RetroSignalSet) -> Iterable[RetroSignal]:
    issue = signals.issue
    workflow = signals.workflow
    yield from issue.observation_distribution
    yield from issue.occurrence_trends
    yield from issue.assessment_corrections
    yield from issue.regressions
    yield from issue.review_decision_patterns
    yield from issue.resolution_outcomes
    yield from issue.repeated_not_an_issue
    yield from workflow.gate_pushback
    yield from workflow.healing_efficiency
    yield from workflow.skill_execution_drift
    yield from signals.eval.trends


def count_signals(signals: RetroSignalSet) -> int:
    return sum(1 for _ in _iter_signals(signals))


def assert_signal_refs_resolve(signals: RetroSignalSet, manifest: RetroSourceManifest) -> None:
    resolvable = manifest.resolvable_ids()
    for signal in _iter_signals(signals):
        missing = [ref for ref in signal.source_refs.all_ids() if ref not in resolvable]
        if missing:
            raise ValueError(f"signal {signal.signal_id!r} cites unresolved source ids: {', '.join(missing)}")


def _aggregate_issue_signals(issue_slice: IssueEvidenceSlice) -> IssueRetroSignals:
    obs_counter: Counter[str] = Counter()
    obs_ids: dict[str, list[str]] = {}
    for observation in issue_slice.observations:
        key = observation.kind
        obs_counter[key] += 1
        obs_ids.setdefault(key, []).append(observation.observation_id)

    # ImprovementSourceRefs has no observation_ids field; observation IDs are pinned
    # on change_issue_ledger evidence_ids and cited via issue_event_ids for resolution.
    observation_distribution = tuple(
        _signal(
            f"observation_distribution:{kind}",
            source_refs=ImprovementSourceRefs(
                issue_event_ids=tuple(sorted(set(obs_ids[kind]))),
            ),
            metrics={"kind": kind, "count": count},
        )
        for kind, count in sorted(obs_counter.items())
    )

    class_counter: Counter[str] = Counter()
    class_problems: dict[str, set[str]] = {}
    class_occs: dict[str, set[str]] = {}
    for occurrence in issue_slice.occurrences:
        classification = occurrence.provisional_assessment.classification
        class_counter[classification] += 1
        class_problems.setdefault(classification, set()).add(occurrence.problem_id)
        class_occs.setdefault(classification, set()).add(occurrence.occurrence_id)

    occurrence_trends = tuple(
        _signal(
            f"occurrence_trend:{classification}",
            source_refs=ImprovementSourceRefs(
                problem_ids=tuple(sorted(class_problems[classification])),
                occurrence_ids=tuple(sorted(class_occs[classification])),
            ),
            metrics={"classification": classification, "count": count},
        )
        for classification, count in sorted(class_counter.items())
    )

    # Map problem_id -> first provisional classification from occurrences.
    provisional: dict[str, str] = {}
    for occurrence in issue_slice.occurrences:
        provisional.setdefault(occurrence.problem_id, occurrence.provisional_assessment.classification)
    detected_class: dict[str, str] = {}
    for event in issue_slice.problem_events:
        if isinstance(event, ProblemDetectedEvent):
            detected_class[event.problem_id] = event.classification

    assessment_corrections: list[RetroSignal] = []
    for event in issue_slice.problem_events:
        if not isinstance(event, ProblemAssessmentConfirmedEvent):
            continue
        prior = provisional.get(event.problem_id) or detected_class.get(event.problem_id)
        if prior is not None and prior == event.classification:
            continue
        assessment_corrections.append(
            _signal(
                f"assessment_correction:{event.event_id}",
                source_refs=ImprovementSourceRefs(
                    problem_ids=(event.problem_id,),
                    issue_event_ids=(event.event_id,),
                ),
                metrics={
                    "from_classification": prior or "unknown",
                    "to_classification": event.classification,
                    "count": 1,
                },
            )
        )

    regressions = tuple(
        _signal(
            f"regression:{event.event_id}",
            source_refs=ImprovementSourceRefs(
                problem_ids=(event.problem_id,),
                occurrence_ids=(event.occurrence_id,),
                issue_event_ids=(event.event_id,),
            ),
            metrics={"change_id": event.change_id, "count": 1},
        )
        for event in issue_slice.problem_events
        if isinstance(event, ProblemRegressedEvent)
    )

    review_decision_patterns = tuple(
        _signal(
            f"review_decision:{event.event_id}",
            source_refs=ImprovementSourceRefs(
                problem_ids=(event.problem_id,),
                issue_event_ids=(event.event_id,),
            ),
            metrics={"action": action, "count": 1},
        )
        for event in issue_slice.problem_events
        for action in (_REVIEW_ACTIONS.get(event.type),)
        if action is not None
    )

    resolution_outcomes: list[RetroSignal] = []
    for event in issue_slice.problem_events:
        if isinstance(event, ProblemResolvedEvent):
            resolution_outcomes.append(
                _signal(
                    f"resolution:{event.event_id}",
                    source_refs=ImprovementSourceRefs(
                        problem_ids=(event.problem_id,),
                        issue_event_ids=(event.event_id,),
                    ),
                    metrics={"outcome": "resolved", "count": 1},
                )
            )
        elif isinstance(event, ProblemVerificationRequestedEvent):
            resolution_outcomes.append(
                _signal(
                    f"resolution:{event.event_id}",
                    source_refs=ImprovementSourceRefs(
                        problem_ids=(event.problem_id,),
                        issue_event_ids=(event.event_id,),
                    ),
                    metrics={"outcome": "verification_pending", "count": 1},
                )
            )

    not_an_issue_class: dict[str, set[str]] = {}
    not_an_issue_events: dict[str, set[str]] = {}
    class_for_problem = dict(detected_class)
    for event in issue_slice.problem_events:
        if isinstance(event, ProblemAssessmentConfirmedEvent):
            class_for_problem[event.problem_id] = event.classification
    for event in issue_slice.problem_events:
        if not isinstance(event, ProblemMarkedNotAnIssueEvent):
            continue
        classification = class_for_problem.get(event.problem_id, "unknown")
        not_an_issue_class.setdefault(classification, set()).add(event.problem_id)
        not_an_issue_events.setdefault(classification, set()).add(event.event_id)

    repeated_not_an_issue = tuple(
        _signal(
            f"repeated_not_an_issue:{classification}",
            source_refs=ImprovementSourceRefs(
                problem_ids=tuple(sorted(problem_ids)),
                issue_event_ids=tuple(sorted(not_an_issue_events[classification])),
            ),
            metrics={"classification": classification, "count": len(problem_ids)},
        )
        for classification, problem_ids in sorted(not_an_issue_class.items())
    )

    return IssueRetroSignals(
        observation_distribution=observation_distribution,
        occurrence_trends=occurrence_trends,
        assessment_corrections=tuple(assessment_corrections),
        regressions=regressions,
        review_decision_patterns=review_decision_patterns,
        resolution_outcomes=tuple(resolution_outcomes),
        repeated_not_an_issue=repeated_not_an_issue,
    )


def _aggregate_workflow_signals(workflow_slice: WorkflowEvidenceSlice) -> WorkflowRetroSignals:
    # Do not fabricate zero-valued signals when the slice carries no evidence.
    gate_counter: Counter[tuple[str, str]] = Counter()
    gate_evidence: dict[tuple[str, str], list[str]] = {}
    for record in workflow_slice.gate_verdicts:
        key = (record.gate, record.verdict)
        gate_counter[key] += 1
        gate_evidence.setdefault(key, []).append(record.evidence_id)

    gate_pushback = tuple(
        _signal(
            f"gate_pushback:{gate}:{verdict}",
            source_refs=ImprovementSourceRefs(
                workflow_evidence_ids=tuple(sorted(set(gate_evidence[(gate, verdict)]))),
            ),
            metrics={"gate": gate, "verdict": verdict, "count": count},
        )
        for (gate, verdict), count in sorted(gate_counter.items())
    )

    healing_efficiency: tuple[RetroSignal, ...] = ()
    attempts = len(workflow_slice.healing_allocations)
    applied = sum(1 for record in workflow_slice.healing_applies if record.applied)
    if attempts or workflow_slice.healing_applies:
        evidence = tuple(
            sorted(
                {
                    *(record.evidence_id for record in workflow_slice.healing_allocations),
                    *(record.evidence_id for record in workflow_slice.healing_applies),
                }
            )
        )
        rate = (applied / attempts) if attempts else 0.0
        healing_efficiency = (
            _signal(
                "healing_efficiency",
                source_refs=ImprovementSourceRefs(workflow_evidence_ids=evidence),
                metrics={
                    "attempts": attempts,
                    "applied": applied,
                    "success_rate": rate,
                },
            ),
        )

    skill_counter: Counter[str] = Counter()
    skill_evidence: dict[str, list[str]] = {}
    for record in workflow_slice.skill_loaded_false:
        skill_counter[record.phase] += 1
        skill_evidence.setdefault(record.phase, []).append(record.evidence_id)

    skill_execution_drift = tuple(
        _signal(
            f"skill_execution_drift:{phase}",
            source_refs=ImprovementSourceRefs(
                workflow_evidence_ids=tuple(sorted(set(skill_evidence[phase]))),
            ),
            metrics={"phase": phase, "count": count},
        )
        for phase, count in sorted(skill_counter.items())
    )

    return WorkflowRetroSignals(
        gate_pushback=gate_pushback,
        healing_efficiency=healing_efficiency,
        skill_execution_drift=skill_execution_drift,
    )


def _aggregate_eval_signals(eval_slice: EvalEvidenceSlice) -> EvalRetroSignals:
    trends = tuple(
        _signal(
            f"eval_trend:{report.run_id}",
            source_refs=ImprovementSourceRefs(eval_run_ids=(report.run_id,)),
            metrics={
                "suite": report.suite,
                "verdict": report.verdict,
                "started_at": report.started_at,
                "count": 1,
            },
        )
        for report in eval_slice.reports
    )
    return EvalRetroSignals(trends=trends)


def aggregate_signals(
    issue_slice: IssueEvidenceSlice,
    workflow_slice: WorkflowEvidenceSlice,
    eval_slice: EvalEvidenceSlice,
) -> RetroSignalSet:
    return RetroSignalSet(
        issue=_aggregate_issue_signals(issue_slice),
        workflow=_aggregate_workflow_signals(workflow_slice),
        eval=_aggregate_eval_signals(eval_slice),
    )


def build_retro_context(
    selection: RetroWindowSelection,
    *,
    issue_history: IssueHistoryReader,
    workflow_history: WorkflowHistoryReader,
    eval_history: EvalHistoryReader,
    retro_id: str,
    generated_at: str,
) -> RetroContext:
    """Pure aggregator: readers + ids only; no filesystem or wall clock."""
    window = resolve_retro_window(selection, workflow_history=workflow_history)
    issue_slice = issue_history.read_window(window.to_issue_selection())
    workflow_slice = workflow_history.read_window(window)
    eval_slice = eval_history.read_window(window)
    manifest = build_source_manifest(issue_slice, workflow_slice, eval_slice)
    signals = aggregate_signals(issue_slice, workflow_slice, eval_slice)
    assert_signal_refs_resolve(signals, manifest)

    context_window = window.to_context_window()
    project_head = next(
        (
            source.head_event_id
            for source in issue_slice.sources
            if source.kind == "project_problem_ledger" and source.head_event_id
        ),
        None,
    )
    if project_head is not None:
        context_window = context_window.model_copy(update={"project_event_through": project_head})

    integrity = combine_integrity(
        issue_slice,
        workflow_slice,
        eval_slice,
        window_reasons=window.integrity.reasons,
    )
    return RetroContext(
        retro_id=retro_id,
        generated_at=generated_at,
        window=context_window,
        source_manifest=manifest,
        integrity=integrity,
        signals=signals,
        signal_count=count_signals(signals),
    )
