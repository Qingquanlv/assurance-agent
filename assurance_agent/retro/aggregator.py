from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe, assert_path_segment_safe
from assurance_agent.retro.archive_reader import (
    list_archived_changes, read_archived_change, resolve_change_dir,
)
from assurance_agent.retro.eval_trend import read_eval_trend
from assurance_agent.retro.types import (
    ArchivedChange, ChangeSource, FailureDistributionSignal, GatePushbackSignal,
    HealingEfficiencySignal, HumanDecisionSignal, RetroContext, RetroSignalSet,
    RetroWindow, SkillExecutionSignal,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_retro_id() -> str:
    return "retro-" + datetime.now(timezone.utc).strftime("%Y%m%d")


def _collect_changes(project_root: Path, since: str | None,
                     changes: list[str] | None) -> list[ArchivedChange]:
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
    for change in changes:
        if change.failure_analysis is None:
            continue
        for failure in change.failure_analysis.failures:
            counter[str(failure.category)] += 1
    return [FailureDistributionSignal(category=c, count=n)
            for c, n in sorted(counter.items())]


def _gate_pushback(changes: list[ArchivedChange]) -> list[GatePushbackSignal]:
    counter: Counter[str] = Counter()
    for change in changes:
        for event in change.events:
            if event.get("type") == "gate_pushback":
                counter[event.get("gate", "unknown")] += 1
    return [GatePushbackSignal(gate=g, count=n) for g, n in sorted(counter.items())]


def _healing_efficiency(changes: list[ArchivedChange]) -> HealingEfficiencySignal:
    operation_ids: set[str] = set()
    applied = 0
    for change in changes:
        operation_ids.update(
            str(event["operation_id"])
            for event in change.events
            if event.get("type") == "healing_attempt_allocated" and event.get("operation_id")
        )
        for summary in change.apply_summaries:
            if summary.applied:
                applied += 1
    attempts = len(operation_ids)
    rate = applied / attempts if attempts else 0.0
    return HealingEfficiencySignal(attempts=attempts, applied=applied, success_rate=rate)


def _human_decisions(changes: list[ArchivedChange]) -> list[HumanDecisionSignal]:
    decisions: list[HumanDecisionSignal] = []
    for change in changes:
        for event in change.events:
            if event.get("type") == "human_decision":
                decisions.append(HumanDecisionSignal(
                    change_id=change.change_id, decision=str(event.get("action", "unknown"))))
    return decisions


def _skill_execution(changes: list[ArchivedChange]) -> list[SkillExecutionSignal]:
    counter: Counter[str] = Counter()
    for change in changes:
        for event in change.events:
            if event.get("type") == "skill_executed":
                counter[event.get("skill", "unknown")] += 1
    return [SkillExecutionSignal(skill=s, count=n) for s, n in sorted(counter.items())]


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
    signals = RetroSignalSet(
        failure_distribution=_failure_distribution(collected),
        gate_pushback=_gate_pushback(collected),
        healing_efficiency=_healing_efficiency(collected),
        human_decisions=_human_decisions(collected),
        skill_execution=_skill_execution(collected),
        eval_trend=read_eval_trend(project_root),
    )
    window = RetroWindow(
        since=since,
        change_count=len(collected),
        change_ids=[c.change_id for c in collected],
        change_sources=[ChangeSource(change_id=c.change_id,
                                     evidence_source=c.evidence_source, path=c.path)
                        for c in collected],
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
    return total
