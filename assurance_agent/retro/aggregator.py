from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe, assert_path_segment_safe
from assurance_agent.retro.archive_reader import (
    list_archived_changes,
    read_archived_change,
    resolve_change_dir,
)
from assurance_agent.retro.eval_trend import read_eval_trend
from assurance_agent.retro.types import (
    ArchivedChange,
    ChangeSource,
    FailureDistributionSignal,
    GatePushbackSignal,
    HealingEfficiencySignal,
    HumanDecisionSignal,
    IssueRegressionSignal,
    NotAnIssuePatternSignal,
    OccurrenceTrendSignal,
    ProblemDecisionSignal,
    ProblemResolutionSignal,
    ReclassificationSignal,
    RetroContext,
    RetroSignalSet,
    RetroWindow,
    SkillExecutionSignal,
)
from assurance_agent.artifacts.models.issues import ProblemProjection
from assurance_agent.workflow.issues.events import (
    LedgerIntegrityError,
    OccurrenceDetectedEvent,
    OccurrenceLinkedEvent,
    ProblemDetectedEvent,
    ProblemEvent,
    ProblemMarkedNotAnIssueEvent,
    ProblemOccurrenceLinkedEvent,
    ProblemRegressedEvent,
    ProblemResolvedEvent,
    ProblemVerificationRequestedEvent,
    read_problem_events,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _failure_evidence_id(change_id: str, failure: object, index: int) -> str:
    """Stable citable id for a failure: `<change_id>#<failure.id | case_id | FAIL-NNN>`.

    Matches the `<change-id>#<local-id>` shape the aa-retro skill cites and the
    pre-migration context used (e.g. `RET-…#FAIL-001`).
    """
    local = getattr(failure, "id", None) or getattr(failure, "case_id", None) or f"FAIL-{index:03d}"
    return f"{change_id}#{local}"


def _event_evidence_id(change_id: str, event: dict) -> str | None:
    """Citable id for a ledger event: `<change_id>#seq<seq>` when a seq exists."""
    seq = event.get("seq")
    if seq is None:
        return None
    return f"{change_id}#seq{seq}"


def _issue_event_evidence_id(change_id: str, event: object) -> str:
    return f"{change_id}#issue-{getattr(event, 'event_id')}"


def _problem_event_evidence_id(event: object) -> str:
    return f"project#problem-{getattr(event, 'event_id')}"


def _module_of(change_id: str) -> str:
    """Best-effort module token from a change id (e.g. `RET-user-management-…` → `user`)."""
    token = change_id
    if token.startswith("RET-"):
        token = token[len("RET-") :]
    token = token.split("-management-", 1)[0]
    return token.split("-", 1)[0] or change_id


def _default_retro_id() -> str:
    return "retro-" + datetime.now(timezone.utc).strftime("%Y%m%d")


def _collect_changes(
    project_root: Path, since: str | None, changes: list[str] | None
) -> list[ArchivedChange]:
    cutoff: float | None = None
    if since is not None:
        try:
            cutoff = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        except ValueError as err:
            raise AaError(f"invalid --since timestamp: {since}") from err
    ids = changes if changes is not None else list_archived_changes(project_root)
    result: list[ArchivedChange] = []
    for change_id in ids:
        resolved = resolve_change_dir(project_root, change_id)
        if resolved is None:
            continue
        change_dir, source = resolved
        if cutoff is not None and change_dir.stat().st_mtime < cutoff:
            continue
        result.append(read_archived_change(change_dir, source=source))
    return result


def _failure_distribution(changes: list[ArchivedChange]) -> list[FailureDistributionSignal]:
    counter: Counter[str] = Counter()
    change_ids: dict[str, list[str]] = {}
    modules: dict[str, list[str]] = {}
    evidence: dict[str, list[str]] = {}
    for change in changes:
        if change.failure_analysis is None:
            continue
        module = _module_of(change.change_id)
        for index, failure in enumerate(change.failure_analysis.failures, start=1):
            category = str(failure.category)
            counter[category] += 1
            cids = change_ids.setdefault(category, [])
            if change.change_id not in cids:
                cids.append(change.change_id)
            mods = modules.setdefault(category, [])
            if module not in mods:
                mods.append(module)
            evidence.setdefault(category, []).append(_failure_evidence_id(change.change_id, failure, index))
    return [
        FailureDistributionSignal(
            category=c,
            count=n,
            changes=change_ids.get(c, []),
            top_modules=modules.get(c, []),
            evidence_ids=evidence.get(c, []),
        )
        for c, n in sorted(counter.items())
    ]


# Pushback verdicts per the source TS aggregator (`summarizeGatePushback`):
# only these gate_verdict outcomes count as pushback.
_PUSHBACK_VERDICTS = frozenset({"needs_fix", "fail", "blocked", "stop"})


def _top_entries(counter: Counter[str], limit: int) -> list[str]:
    """TS `topEntries`: count desc, then key asc (localeCompare), capped at ``limit``."""
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [key for key, _ in ordered[:limit]]


def _pushback_reason(event: dict) -> str:
    """Reason string for one pushback event (TS: evidence.reason → JSON(evidence) → fallback).

    The Python GateVerdictEvent also carries a top-level ``reason`` field (the
    migration lifted it out of ``evidence``), so it is honored with the same
    priority as ``evidence.reason``.
    """
    evidence = event.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    reason = evidence.get("reason")
    if isinstance(reason, str):
        return reason
    top_level = event.get("reason")
    if isinstance(top_level, str):
        return top_level
    if evidence:
        return json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    return "(no evidence)"


def _gate_pushback(changes: list[ArchivedChange]) -> list[GatePushbackSignal]:
    """One signal per (gate, verdict) over pushback `gate_verdict` events.

    Mirrors TS `summarizeGatePushback`: pushback verdicts are
    needs_fix/fail/blocked/stop; evidence ids are `<change_id>#seq<seq>`;
    top_reasons holds the five most frequent reasons; sorted by count desc,
    then gate asc.
    """
    counter: Counter[tuple[str, str]] = Counter()
    reasons: dict[tuple[str, str], Counter[str]] = {}
    evidence: dict[tuple[str, str], list[str]] = {}
    for change in changes:
        for event in change.events:
            if event.get("type") != "gate_verdict":
                continue
            verdict = str(event.get("verdict", ""))
            if verdict not in _PUSHBACK_VERDICTS:
                continue
            gate = str(event.get("gate", "unknown"))
            key = (gate, verdict)
            counter[key] += 1
            reasons.setdefault(key, Counter())[_pushback_reason(event)] += 1
            eid = _event_evidence_id(change.change_id, event)
            if eid is not None:
                evidence.setdefault(key, []).append(eid)
    signals = [
        GatePushbackSignal(
            gate=gate,
            verdict=verdict,
            count=count,
            top_reasons=_top_entries(reasons[(gate, verdict)], 5),
            evidence_ids=evidence.get((gate, verdict), []),
        )
        for (gate, verdict), count in counter.items()
    ]
    return sorted(signals, key=lambda s: (-s.count, s.gate))


def _healing_efficiency(changes: list[ArchivedChange]) -> HealingEfficiencySignal:
    operation_ids: set[str] = set()
    evidence_ids: list[str] = []
    applied = 0
    for change in changes:
        for event in change.events:
            if event.get("type") == "healing_attempt_allocated" and event.get("operation_id"):
                operation_ids.add(str(event["operation_id"]))
                eid = _event_evidence_id(change.change_id, event)
                if eid is not None:
                    evidence_ids.append(eid)
        for summary in change.apply_summaries:
            if summary.applied:
                applied += 1
    attempts = len(operation_ids)
    rate = applied / attempts if attempts else 0.0
    return HealingEfficiencySignal(
        attempts=attempts, applied=applied, success_rate=rate, evidence_ids=evidence_ids
    )


def _human_decisions(changes: list[ArchivedChange]) -> list[HumanDecisionSignal]:
    decisions: list[HumanDecisionSignal] = []
    for change in changes:
        for event in change.events:
            if event.get("type") == "human_decision":
                decisions.append(
                    HumanDecisionSignal(
                        change_id=change.change_id,
                        decision=str(event.get("action", "unknown")),
                        evidence_id=_event_evidence_id(change.change_id, event),
                    )
                )
    return decisions


def _reclassifications(changes: list[ArchivedChange]) -> list[ReclassificationSignal]:
    """One signal per `failure_reclassified` ledger event (from-side audit proof)."""
    signals: list[ReclassificationSignal] = []
    for change in changes:
        for event in change.events:
            if event.get("type") != "failure_reclassified":
                continue
            eid = _event_evidence_id(change.change_id, event)
            signals.append(
                ReclassificationSignal(
                    change_id=change.change_id,
                    from_category=str(event.get("from", "unknown")),
                    to_category=str(event.get("to", "unknown")),
                    evidence_ids=[eid] if eid is not None else [],
                )
            )
    return signals


def _skill_execution(changes: list[ArchivedChange]) -> list[SkillExecutionSignal]:
    """One signal per phase that ran with ``skill_loaded: false`` (skill-execution drift).

    Mirrors TS `summarizeSkillExecution`: only ``skill_loaded === false`` is
    drift — ``true`` and ``n/a`` (CLI-driven phases) are fine. Evidence ids are
    `<change_id>#workflow-state:<phase>`; sorted by count desc, then phase asc.
    """
    counter: Counter[str] = Counter()
    change_sets: dict[str, set[str]] = {}
    evidence: dict[str, list[str]] = {}
    for change in changes:
        if change.workflow_state is None:
            continue
        for phase, state in change.workflow_state.phases.model_dump().items():
            if not isinstance(state, dict) or state.get("skill_loaded") is not False:
                continue
            counter[phase] += 1
            change_sets.setdefault(phase, set()).add(change.change_id)
            evidence.setdefault(phase, []).append(f"{change.change_id}#workflow-state:{phase}")
    signals = [
        SkillExecutionSignal(
            phase=phase,
            count=count,
            changes=sorted(change_sets[phase]),
            evidence_ids=evidence.get(phase, []),
        )
        for phase, count in counter.items()
    ]
    return sorted(signals, key=lambda s: (-s.count, s.phase))


_HUMAN_DECISION_TYPES: dict[str, str] = {
    "problem_assessment_confirmed": "confirm_assessment",
    "problem_work_started": "start_work",
    "problem_marked_not_an_issue": "mark_not_an_issue",
    "problem_risk_accepted": "accept_risk",
    "problem_reopened": "reopen",
    "problem_merged": "merge",
}


def _load_problem_projection(project_root: Path) -> ProblemProjection | None:
    path = project_root / "qa" / "issues" / "problems.json"
    if not path.is_file():
        return None
    try:
        return ProblemProjection.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_problem_events(project_root: Path) -> list[ProblemEvent]:
    path = project_root / "qa" / "issues" / "events.jsonl"
    if not path.is_file():
        return []
    try:
        return read_problem_events(path)
    except LedgerIntegrityError as exc:
        raise AaError(f"invalid project problem ledger: {exc}") from exc


def _window_problem_ids(changes: list[ArchivedChange]) -> set[str]:
    window_changes = {change.change_id for change in changes}
    problem_ids: set[str] = set()
    for change in changes:
        if change.issue_read_error:
            continue
        for event in change.issue_events:
            if isinstance(event, (OccurrenceDetectedEvent, OccurrenceLinkedEvent)):
                problem_ids.add(event.occurrence.problem_id)
    for change in changes:
        if change.issue_snapshot is not None:
            for occurrence in change.issue_snapshot.occurrences:
                if occurrence.change_id in window_changes:
                    problem_ids.add(occurrence.problem_id)
    return problem_ids


def _problem_event_in_window(event: ProblemEvent, window_changes: set[str], problem_ids: set[str]) -> bool:
    change_id = getattr(event, "change_id", None)
    if isinstance(change_id, str) and change_id in window_changes:
        return True
    return event.problem_id in problem_ids


def _occurrence_trends(changes: list[ArchivedChange]) -> list[OccurrenceTrendSignal]:
    counter: Counter[str] = Counter()
    change_ids: dict[str, list[str]] = {}
    evidence: dict[str, list[str]] = {}
    for change in changes:
        if change.issue_read_error:
            continue
        for event in change.issue_events:
            if not isinstance(event, (OccurrenceDetectedEvent, OccurrenceLinkedEvent)):
                continue
            classification = event.occurrence.provisional_assessment.classification
            counter[classification] += 1
            cids = change_ids.setdefault(classification, [])
            if change.change_id not in cids:
                cids.append(change.change_id)
            evidence.setdefault(classification, []).append(
                _issue_event_evidence_id(change.change_id, event)
            )
    return [
        OccurrenceTrendSignal(
            classification=classification,
            count=count,
            changes=change_ids.get(classification, []),
            evidence_ids=evidence.get(classification, []),
        )
        for classification, count in sorted(counter.items())
    ]


def _issue_regressions(
    changes: list[ArchivedChange],
    project_events: list[ProblemEvent],
    window_changes: set[str],
    problem_ids: set[str],
) -> list[IssueRegressionSignal]:
    signals: list[IssueRegressionSignal] = []
    for event in project_events:
        if not isinstance(event, ProblemRegressedEvent):
            continue
        if not _problem_event_in_window(event, window_changes, problem_ids):
            continue
        signals.append(
            IssueRegressionSignal(
                problem_id=event.problem_id,
                change_id=event.change_id,
                evidence_ids=[_problem_event_evidence_id(event)],
            )
        )
    return signals


def _problem_decisions(
    project_events: list[ProblemEvent],
    window_changes: set[str],
    problem_ids: set[str],
) -> list[ProblemDecisionSignal]:
    signals: list[ProblemDecisionSignal] = []
    for event in project_events:
        action = _HUMAN_DECISION_TYPES.get(event.type)  # type: ignore[arg-type]
        if action is None:
            continue
        if not _problem_event_in_window(event, window_changes, problem_ids):
            continue
        signals.append(
            ProblemDecisionSignal(
                problem_id=event.problem_id,
                action=action,
                evidence_ids=[_problem_event_evidence_id(event)],
            )
        )
    return signals


def _problem_resolutions(
    project_events: list[ProblemEvent],
    window_changes: set[str],
    problem_ids: set[str],
) -> list[ProblemResolutionSignal]:
    signals: list[ProblemResolutionSignal] = []
    for event in project_events:
        if isinstance(event, ProblemResolvedEvent):
            if not _problem_event_in_window(event, window_changes, problem_ids):
                continue
            signals.append(
                ProblemResolutionSignal(
                    problem_id=event.problem_id,
                    outcome="resolved",
                    change_id=event.change_id,
                    evidence_ids=[_problem_event_evidence_id(event)],
                )
            )
        elif isinstance(event, ProblemVerificationRequestedEvent):
            if not _problem_event_in_window(event, window_changes, problem_ids):
                continue
            signals.append(
                ProblemResolutionSignal(
                    problem_id=event.problem_id,
                    outcome="verification_pending",
                    change_id=event.change_id,
                    evidence_ids=[_problem_event_evidence_id(event)],
                )
            )
    return signals


def _not_an_issue_patterns(
    project_events: list[ProblemEvent],
    projection: ProblemProjection | None,
    window_changes: set[str],
    problem_ids: set[str],
) -> list[NotAnIssuePatternSignal]:
    classifications: dict[str, list[str]] = {}
    evidence: dict[str, list[str]] = {}
    problem_class: dict[str, str] = {}
    if projection is not None:
        for problem in projection.problems:
            problem_class[problem.problem_id] = problem.assessment.classification
    for event in project_events:
        if isinstance(event, ProblemDetectedEvent):
            problem_class[event.problem_id] = event.classification
    for event in project_events:
        if not isinstance(event, ProblemMarkedNotAnIssueEvent):
            continue
        if not _problem_event_in_window(event, window_changes, problem_ids):
            continue
        classification = problem_class.get(event.problem_id, "unknown")
        classifications.setdefault(classification, []).append(event.problem_id)
        evidence.setdefault(classification, []).append(_problem_event_evidence_id(event))
    return [
        NotAnIssuePatternSignal(
            classification=classification,
            count=len(ids),
            problem_ids=sorted(set(ids)),
            evidence_ids=evidence.get(classification, []),
        )
        for classification, ids in sorted(classifications.items())
        if len(set(ids)) >= 1
    ]


def _issue_lifecycle_signals(
    project_root: Path,
    changes: list[ArchivedChange],
) -> tuple[
    list[OccurrenceTrendSignal],
    list[IssueRegressionSignal],
    list[ProblemDecisionSignal],
    list[ProblemResolutionSignal],
    list[NotAnIssuePatternSignal],
]:
    window_changes = {change.change_id for change in changes}
    problem_ids = _window_problem_ids(changes)
    projection = _load_problem_projection(project_root)
    project_events = _load_problem_events(project_root)
    for event in project_events:
        if isinstance(event, ProblemDetectedEvent) and event.change_id in window_changes:
            problem_ids.add(event.problem_id)
        elif (
            isinstance(event, ProblemOccurrenceLinkedEvent)
            and event.change_id in window_changes
        ):
            problem_ids.add(event.problem_id)
    return (
        _occurrence_trends(changes),
        _issue_regressions(changes, project_events, window_changes, problem_ids),
        _problem_decisions(project_events, window_changes, problem_ids),
        _problem_resolutions(project_events, window_changes, problem_ids),
        _not_an_issue_patterns(project_events, projection, window_changes, problem_ids),
    )


def build_retro_context(
    project_root: Path,
    *,
    since: str | None = None,
    changes: list[str] | None = None,
    retro_id: str | None = None,
) -> RetroContext:
    for change_id in changes or []:
        assert_change_id_safe(change_id)
    resolved_retro_id = retro_id or _default_retro_id()
    assert_path_segment_safe(resolved_retro_id, label="retro id")
    collected = _collect_changes(project_root, since, changes)
    (
        occurrence_trends,
        issue_regressions,
        problem_decisions,
        problem_resolutions,
        not_an_issue_patterns,
    ) = _issue_lifecycle_signals(project_root, collected)
    issue_evidence_errors = {
        change.change_id: change.issue_read_error
        for change in collected
        if change.issue_read_error
    }
    signals = RetroSignalSet(
        failure_distribution=_failure_distribution(collected),
        gate_pushback=_gate_pushback(collected),
        healing_efficiency=_healing_efficiency(collected),
        human_decisions=_human_decisions(collected),
        reclassifications=_reclassifications(collected),
        skill_execution=_skill_execution(collected),
        eval_trend=read_eval_trend(project_root),
        occurrence_trends=occurrence_trends,
        issue_regressions=issue_regressions,
        problem_decisions=problem_decisions,
        problem_resolutions=problem_resolutions,
        not_an_issue_patterns=not_an_issue_patterns,
    )
    window = RetroWindow(
        since=since,
        change_count=len(collected),
        change_ids=[c.change_id for c in collected],
        change_sources=[
            ChangeSource(change_id=c.change_id, evidence_source=c.evidence_source, path=c.path)
            for c in collected
        ],
        issue_evidence_errors=issue_evidence_errors,
    )
    context = RetroContext(
        retro_id=resolved_retro_id,
        generated_at=_now(),
        window=window,
        signals=signals,
    )
    context.signal_count = count_signals(context)
    return context


def count_signals(context: RetroContext) -> int:
    signals = context.signals
    total = 0
    total += sum(s.count for s in signals.failure_distribution)
    total += sum(s.count for s in signals.gate_pushback)
    total += signals.healing_efficiency.attempts
    total += len(signals.human_decisions)
    total += len(signals.reclassifications)
    total += sum(s.count for s in signals.skill_execution)
    total += len(signals.eval_trend)
    total += sum(s.count for s in signals.occurrence_trends)
    total += len(signals.issue_regressions)
    total += len(signals.problem_decisions)
    total += len(signals.problem_resolutions)
    total += sum(s.count for s in signals.not_an_issue_patterns)
    return total
