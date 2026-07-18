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
    ReclassificationSignal,
    RetroContext,
    RetroSignalSet,
    RetroWindow,
    SkillExecutionSignal,
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
        reclassifications=_reclassifications(collected),
        skill_execution=_skill_execution(collected),
        eval_trend=read_eval_trend(project_root),
    )
    window = RetroWindow(
        since=since,
        change_count=len(collected),
        change_ids=[c.change_id for c in collected],
        change_sources=[
            ChangeSource(change_id=c.change_id, evidence_source=c.evidence_source, path=c.path)
            for c in collected
        ],
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
