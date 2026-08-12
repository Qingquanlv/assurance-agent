"""Materialize immutable Retro v3 evidence slices from typed history readers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.improvements import ImprovementSourceRefs
from assurance_agent.artifacts.models.retro_v3 import (
    AffectedSurface,
    BatchMemberEvidenceGapSignal,
    ConfirmedEscapeSignal,
    CoverageGapEvidenceEntry,
    CoverageGapEvidenceSlice,
    DiscoveryEvidenceEntry,
    DiscoveryEvidenceSlice,
    DomainEvidenceGapSignal,
    EvalEvidenceEntry,
    EvalEvidenceSlice,
    GateVerdictEvidenceEntry,
    HealingOutcomeEvidenceEntry,
    IssueEvidenceEntry,
    IssueEvidenceSlice,
    LowPromotionRateSignal,
    LowReplayStabilitySignal,
    ReopenedCoverageGapSignal,
    RetroIntegrity,
    RetroPipelineFailureSignal,
    RetroSourceDescriptor,
    RetroWindow,
    SignalDocumentV3,
    SkillDriftEvidenceEntry,
    TaskFailureEvidenceEntry,
    TaskFailureSignal,
    WorkflowEvidenceSlice,
)
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.discovery_history import (
    CoverageGapHistoryReader,
    CoverageGapHistorySlice,
    DiscoveryHistoryReader,
    DiscoveryHistorySlice,
)
from assurance_agent.retro.eval_history import (
    EvalEvidenceSlice as EvalHistorySlice,
    EvalHistoryReader,
)
from assurance_agent.retro.window import ResolvedRetroWindow, RetroWindowSelection, resolve_retro_window
from assurance_agent.retro.workflow_history import (
    WorkflowEvidenceSlice as WorkflowHistorySlice,
    WorkflowHistoryReader,
)
from assurance_agent.workflow.issues.history import IssueHistoryReader
from assurance_agent.workflow.issues.history_models import (
    IssueEvidenceSlice as IssueHistorySlice,
    IssueHistoryIntegrity,
)

# Report-only Retro thresholds (constants OK for M4 Task 3).
LOW_PROMOTION_RATE_THRESHOLD = 0.5
LOW_REPLAY_STABILITY_THRESHOLD = 1.0


class RetroSliceImmutableError(AaError):
    """A v3 evidence artifact already exists with different canonical bytes."""


@dataclass(frozen=True)
class SliceBundle:
    window: RetroWindow
    issue: IssueEvidenceSlice
    workflow: WorkflowEvidenceSlice
    eval: EvalEvidenceSlice
    # Optional for historical runs; when present, assembly verifies and includes
    # these deterministic domains alongside the core three.
    discovery: DiscoveryEvidenceSlice | None = None
    coverage_gap: CoverageGapEvidenceSlice | None = None

    @property
    def all_slices(
        self,
    ) -> tuple[
        IssueEvidenceSlice
        | WorkflowEvidenceSlice
        | EvalEvidenceSlice
        | DiscoveryEvidenceSlice
        | CoverageGapEvidenceSlice,
        ...,
    ]:
        optional = tuple(item for item in (self.discovery, self.coverage_gap) if item is not None)
        return (self.issue, self.workflow, self.eval, *optional)


_BATCH_GAP_RE = re.compile(
    r"^batch_member_evidence_gap:(?P<change_id>[^:]+):(?P<status>[^:]+):"
    r"(?P<domain>issue|workflow|eval):(?P<reason>[^:]+)$"
)
_GapDomain = Literal["issue", "workflow", "eval"]
_GapReason = Literal[
    "workspace_missing",
    "non_terminal",
    "ledger_missing",
    "ledger_corrupt",
    "digest_drift",
    "projection_missing",
    "projection_corrupt",
]
_CONTRACT_ROOT_ERROR_KINDS = frozenset({"auth", "forbidden_write", "contract"})
_DETERMINISTIC_FAILURE_KINDS = _CONTRACT_ROOT_ERROR_KINDS | {"internal"}


def _batch_gap_signals(
    window: RetroWindow,
    integrity: RetroIntegrity | IssueHistoryIntegrity,
    *,
    domain: _GapDomain,
) -> tuple[tuple[BatchMemberEvidenceGapSignal, ...], tuple[RetroSourceDescriptor, ...]]:
    if window.batch_scope is None:
        return (), ()
    manifest_sha = sha256_bytes(canonical_json_bytes(window.batch_scope))
    signals: list[BatchMemberEvidenceGapSignal] = []
    sources: list[RetroSourceDescriptor] = []
    for reason in integrity.reasons:
        match = _BATCH_GAP_RE.fullmatch(str(reason))
        if match is None or match.group("domain") != domain:
            continue
        preimage = ":".join(
            (
                window.batch_scope.batch_id,
                match.group("change_id"),
                match.group("status"),
                domain,
                match.group("reason"),
            )
        )
        signal_id = "BATCH-GAP-" + sha256_bytes(preimage.encode("utf-8")).removeprefix("sha256:")[:24]
        refs = ImprovementSourceRefs(workflow_evidence_ids=(signal_id,))
        signals.append(
            BatchMemberEvidenceGapSignal(
                signal_id=signal_id,
                summary=f"{domain} evidence unavailable for {match.group('change_id')}",
                occurrence_count=1,
                recommended_change="Restore complete, immutable evidence collection for this batch member.",
                source_refs=refs,
                confidence="high",
                change_id=match.group("change_id"),
                execution_status=match.group("status"),
                domain=domain,
                reason_code=cast(_GapReason, match.group("reason")),
            )
        )
        sources.append(
            RetroSourceDescriptor(
                kind="batch_manifest",
                change_id=match.group("change_id"),
                sha256=manifest_sha,
                evidence_ids=(signal_id,),
            )
        )
    ordered = tuple(sorted(signals, key=lambda item: (item.change_id, item.domain, item.reason_code)))
    ordered_sources = tuple(sorted(sources, key=lambda item: (item.change_id or "", item.evidence_ids)))
    return ordered, ordered_sources


def _message_fingerprint(message: str) -> str:
    normalized = re.sub(r"\s+", " ", message).strip()
    return sha256_bytes(normalized.encode("utf-8"))


def _task_failure_signals(
    entries: tuple[TaskFailureEvidenceEntry, ...],
) -> tuple[TaskFailureSignal, ...]:
    by_change: dict[str, list[TaskFailureEvidenceEntry]] = {}
    for entry in entries:
        if not entry.recovered and entry.error_kind in _DETERMINISTIC_FAILURE_KINDS:
            by_change.setdefault(entry.change_id, []).append(entry)

    selected: list[TaskFailureEvidenceEntry] = []
    for failures in by_change.values():
        contract_roots = [entry for entry in failures if entry.error_kind in _CONTRACT_ROOT_ERROR_KINDS]
        selected.extend(contract_roots or failures)

    grouped: dict[tuple[str, str, str], list[TaskFailureEvidenceEntry]] = {}
    for entry in selected:
        key = (entry.node_id, entry.error_kind, entry.message_fingerprint)
        grouped.setdefault(key, []).append(entry)

    signals: list[TaskFailureSignal] = []
    for (node_id, error_kind, message_fingerprint), failures in sorted(grouped.items()):
        evidence_ids = tuple(sorted({entry.evidence_id for entry in failures}))
        identity = ":".join((node_id, error_kind, message_fingerprint))
        signal_id = "TASK-FAILURE-" + sha256_bytes(identity.encode("utf-8")).removeprefix("sha256:")[:24]
        signals.append(
            TaskFailureSignal(
                signal_id=signal_id,
                summary=f"Unrecovered {error_kind} failure at {node_id}",
                occurrence_count=len(evidence_ids),
                recommended_change=(
                    f"Eliminate the unrecovered {error_kind} failure at {node_id}; align the "
                    "operation contract and runtime behavior, then add regression coverage."
                ),
                source_refs=ImprovementSourceRefs(workflow_evidence_ids=evidence_ids),
                confidence="high",
                node_id=node_id,
                error_kind=error_kind,
                message_fingerprint=message_fingerprint,
            )
        )
    return tuple(signals)


def _blocked_healing_signals(
    entries: tuple[
        GateVerdictEvidenceEntry
        | TaskFailureEvidenceEntry
        | HealingOutcomeEvidenceEntry
        | SkillDriftEvidenceEntry,
        ...,
    ],
) -> tuple[RetroPipelineFailureSignal, ...]:
    """Promote allocated healing blocked by a fail-closed gate deterministically."""
    by_change: dict[str, list[object]] = {}
    for entry in entries:
        by_change.setdefault(entry.change_id, []).append(entry)

    signals: list[RetroPipelineFailureSignal] = []
    for change_id, change_entries in sorted(by_change.items()):
        allocations = [
            entry
            for entry in change_entries
            if isinstance(entry, HealingOutcomeEvidenceEntry) and entry.outcome == "allocated"
        ]
        applied = any(
            isinstance(entry, HealingOutcomeEvidenceEntry) and entry.outcome == "applied"
            for entry in change_entries
        )
        stops_by_gate: dict[str, list[GateVerdictEvidenceEntry]] = {}
        for entry in change_entries:
            if (
                isinstance(entry, GateVerdictEvidenceEntry)
                and entry.verdict == "stop"
                and entry.reason == "fail-closed default"
            ):
                stops_by_gate.setdefault(entry.gate_id, []).append(entry)
        if not allocations or applied:
            continue
        for gate_id, stops in sorted(stops_by_gate.items()):
            evidence_ids = tuple(
                sorted(
                    {
                        *(entry.evidence_id for entry in allocations),
                        *(entry.evidence_id for entry in stops),
                    }
                )
            )
            operation_ids = tuple(sorted({entry.operation for entry in allocations}))
            identity = ":".join((change_id, gate_id, *operation_ids))
            digest = sha256_bytes(identity.encode("utf-8")).removeprefix("sha256:")[:24]
            failure_id = f"HEALING-GATE-{digest}"
            signals.append(
                RetroPipelineFailureSignal(
                    signal_id=failure_id,
                    summary=(
                        f"Healing allocation was blocked by fail-closed gate {gate_id} "
                        "before any fixer applied"
                    ),
                    occurrence_count=len(stops),
                    recommended_change=(
                        "Validate every fixer-approval gate input before allocating a healing "
                        "attempt, and retain this allocation-without-apply scenario as a "
                        "workflow regression test."
                    ),
                    source_refs=ImprovementSourceRefs(workflow_evidence_ids=evidence_ids),
                    confidence="high",
                    failure_id=failure_id,
                    stage=gate_id,
                    error_kind="healing_allocation_blocked",
                )
            )
    return tuple(signals)


def _expected_skill(phase: str, declared: str | None) -> str:
    if declared and declared.strip():
        return declared.strip()
    return phase if phase.startswith("aa-") else f"aa-{phase}"


def _integrity(*source_integrities: RetroIntegrity | IssueHistoryIntegrity) -> RetroIntegrity:
    reasons: list[str] = []
    for integrity in source_integrities:
        for reason in integrity.reasons:
            text = str(reason)
            if text not in reasons:
                reasons.append(text)
    return (
        RetroIntegrity(status="incomplete", reasons=tuple(reasons))
        if reasons
        else RetroIntegrity(status="complete")
    )


def _issue_slice(retro_id: str, window: RetroWindow, source_slice: IssueHistorySlice) -> IssueEvidenceSlice:
    observations = {item.observation_id: item for item in source_slice.observations}
    problems = {item.problem_id: item for item in source_slice.problem_snapshots}
    problem_event_ids: dict[str, list[str]] = {}
    for event in source_slice.problem_events:
        problem_event_ids.setdefault(event.problem_id, []).append(event.event_id)

    entries_by_id: dict[str, IssueEvidenceEntry] = {}
    for occurrence in source_slice.occurrences:
        if occurrence.occurrence_id in entries_by_id:
            continue
        problem = problems.get(occurrence.problem_id)
        if problem is None:
            raise ValueError(f"issue occurrence {occurrence.occurrence_id} has no problem snapshot")
        linked = [observations[item] for item in occurrence.observation_ids if item in observations]
        if not linked:
            raise ValueError(f"issue occurrence {occurrence.occurrence_id} has no observation timestamp")
        preimage = problem.fingerprint.preimage
        surface_kind = preimage.surface_kind if preimage is not None else "unknown"
        surface_value = preimage.surface_identity if preimage is not None else problem.title
        symptom = preimage.symptom if preimage is not None else problem.title
        event_ids = problem_event_ids.get(problem.problem_id, [])
        entries_by_id[occurrence.occurrence_id] = IssueEvidenceEntry(
            occurrence_id=occurrence.occurrence_id,
            problem_id=occurrence.problem_id,
            change_id=occurrence.change_id,
            batch_id=occurrence.batch_id,
            surface=AffectedSurface(kind=surface_kind, value=surface_value),
            symptom=symptom,
            fingerprint=problem.fingerprint.digest,
            observed_at=min(item.observed_at for item in linked),
            classification_hint=occurrence.provisional_assessment.classification,
            head_event_id=event_ids[-1] if event_ids else None,
        )

    entries = tuple(sorted(entries_by_id.values(), key=lambda item: (item.change_id, item.occurrence_id)))
    occurrence_ids_by_change: dict[str, set[str]] = {}
    observation_ids_by_change: dict[str, set[str]] = {}
    for entry in entries:
        occurrence_ids_by_change.setdefault(entry.change_id, set()).add(entry.occurrence_id)
    for observation in source_slice.observations:
        observation_ids_by_change.setdefault(observation.change_id, set()).add(observation.observation_id)
    all_problem_ids = {entry.problem_id for entry in entries}
    all_problem_event_ids = {
        event_id for problem_id in all_problem_ids for event_id in problem_event_ids.get(problem_id, ())
    }
    sources = []
    for source in source_slice.sources:
        if source.kind == "change_issue_ledger" and source.change_id is not None:
            evidence_ids = tuple(
                sorted(
                    observation_ids_by_change.get(source.change_id, set())
                    | occurrence_ids_by_change.get(source.change_id, set())
                )
            )
        else:
            evidence_ids = tuple(sorted(all_problem_ids | all_problem_event_ids))
        sources.append(
            RetroSourceDescriptor(
                kind=source.kind,
                change_id=source.change_id,
                head_event_id=source.head_event_id,
                sha256=source.sha256,
                evidence_ids=evidence_ids,
            )
        )
    gap_signals, gap_sources = _batch_gap_signals(window, source_slice.integrity, domain="issue")
    return IssueEvidenceSlice(
        retro_id=retro_id,
        window=window,
        sources=(*sources, *gap_sources),
        integrity=_integrity(source_slice.integrity),
        deterministic_signals=gap_signals,
        entries=entries,
    )


def _workflow_slice(
    retro_id: str,
    window: RetroWindow,
    source_slice: WorkflowHistorySlice,
    window_integrity: RetroIntegrity,
) -> WorkflowEvidenceSlice:
    entries = []
    entries.extend(
        GateVerdictEvidenceEntry(
            evidence_id=item.evidence_id,
            change_id=item.change_id,
            gate_id=item.gate,
            verdict=item.verdict,
            cause=item.cause,
            reason=item.reason,
            ts=item.ts,
        )
        for item in source_slice.gate_verdicts
    )
    for item in source_slice.task_failures:
        if not item.task_id or not item.ts:
            raise ValueError(f"workflow failure {item.evidence_id} lacks task_id or ts")
        entries.append(
            TaskFailureEvidenceEntry(
                evidence_id=item.evidence_id,
                change_id=item.change_id,
                task_id=item.task_id,
                attempt_id=item.attempt_id,
                node_id=item.node_id,
                error_kind=item.error_kind,
                message_fingerprint=_message_fingerprint(item.message),
                recovered=item.recovered,
                ts=item.ts,
            )
        )
    entries.extend(
        HealingOutcomeEvidenceEntry(
            evidence_id=item.evidence_id,
            change_id=item.change_id,
            operation=item.operation_id,
            outcome="allocated",
            ts=item.ts,
        )
        for item in source_slice.healing_allocations
    )
    entries.extend(
        HealingOutcomeEvidenceEntry(
            evidence_id=item.evidence_id,
            change_id=item.change_id,
            operation=item.target,
            outcome="applied" if item.applied else "not_applied",
        )
        for item in source_slice.healing_applies
    )
    entries.extend(
        SkillDriftEvidenceEntry(
            evidence_id=item.evidence_id,
            change_id=item.change_id,
            phase=item.phase,
            expected_skill=_expected_skill(item.phase, item.expected_skill),
        )
        for item in source_slice.skill_loaded_false
    )
    by_id = {item.evidence_id: item for item in entries}
    ordered = tuple(sorted(by_id.values(), key=lambda item: (item.change_id, item.evidence_id)))
    actual_ids_by_change: dict[str, set[str]] = {}
    for entry in ordered:
        actual_ids_by_change.setdefault(entry.change_id, set()).add(entry.evidence_id)
    sources = tuple(
        RetroSourceDescriptor(
            kind=source.kind,
            change_id=source.change_id,
            head_event_id=source.head_event_id,
            sha256=source.sha256,
            evidence_ids=tuple(sorted(actual_ids_by_change.get(source.change_id or "", set()))),
        )
        for source in source_slice.sources
    )
    gap_signals, gap_sources = _batch_gap_signals(window, source_slice.integrity, domain="workflow")
    failure_signals = _task_failure_signals(
        tuple(entry for entry in ordered if isinstance(entry, TaskFailureEvidenceEntry))
    )
    blocked_healing_signals = _blocked_healing_signals(ordered)
    return WorkflowEvidenceSlice(
        retro_id=retro_id,
        window=window,
        sources=(*sources, *gap_sources),
        integrity=_integrity(window_integrity, source_slice.integrity),
        deterministic_signals=(*gap_signals, *failure_signals, *blocked_healing_signals),
        entries=ordered,
    )


def _eval_slice(retro_id: str, window: RetroWindow, source_slice: EvalHistorySlice) -> EvalEvidenceSlice:
    by_id: dict[str, EvalEvidenceEntry] = {}
    digest_by_id: dict[str, str] = {}
    for report in source_slice.reports:
        if report.run_id in by_id:
            continue
        by_id[report.run_id] = EvalEvidenceEntry(
            run_id=report.run_id,
            suite=report.suite,
            verdict=report.verdict,
            failure_signature=report.failure_signature,
            started_at=report.started_at,
            source_change_ids=report.source_change_ids,
            sample_ids=report.sample_ids,
        )
        digest_by_id[report.run_id] = report.sha256
    entries = tuple(sorted(by_id.values(), key=lambda item: (item.started_at, item.run_id)))
    sources = tuple(
        RetroSourceDescriptor(
            kind="eval_run",
            head_event_id=entry.run_id,
            sha256=digest_by_id[entry.run_id],
            evidence_ids=(entry.run_id,),
        )
        for entry in entries
    )
    gap_signals, gap_sources = _batch_gap_signals(window, source_slice.integrity, domain="eval")
    return EvalEvidenceSlice(
        retro_id=retro_id,
        window=window,
        sources=(*sources, *gap_sources),
        integrity=_integrity(source_slice.integrity),
        deterministic_signals=gap_signals,
        entries=entries,
    )


def _agent_json_bytes(value: BaseModel) -> bytes:
    payload = value.model_dump(mode="json")
    return (json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_immutable_bundle(
    files: dict[Path, object],
    *,
    agent_files: dict[Path, BaseModel] | None = None,
) -> None:
    encoded = {path: canonical_json_bytes(value) for path, value in files.items()}
    encoded.update({path: _agent_json_bytes(value) for path, value in (agent_files or {}).items()})
    for path, canonical in encoded.items():
        if path.is_file() and path.read_bytes() != canonical:
            raise RetroSliceImmutableError(f"retro evidence artifact already differs: {path}")
    for path, canonical in encoded.items():
        if path.is_file():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical)


def _stable_signal_id(prefix: str, identity: str) -> str:
    return prefix + "-" + sha256_bytes(identity.encode("utf-8")).removeprefix("sha256:")[:24]


def _domain_gap_signals(
    integrity: RetroIntegrity,
    *,
    domain: Literal["discovery", "coverage_gap"],
) -> tuple[DomainEvidenceGapSignal, ...]:
    signals: list[DomainEvidenceGapSignal] = []
    prefix = f"{domain}_projection_"
    for reason in integrity.reasons:
        text = str(reason)
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix) :]
        code, _, change_id = rest.partition(":")
        if code not in {"missing", "corrupt"}:
            continue
        reason_code = "projection_missing" if code == "missing" else "projection_corrupt"
        signal_id = _stable_signal_id("DOMAIN-GAP", f"{domain}:{reason_code}:{change_id}")
        signals.append(
            DomainEvidenceGapSignal(
                signal_id=signal_id,
                summary=f"{domain} evidence {code} for {change_id or 'window'}",
                occurrence_count=1,
                recommended_change=(f"Restore compact archived {domain} projection for this Retro window."),
                source_refs=ImprovementSourceRefs(workflow_evidence_ids=(signal_id,)),
                confidence="high",
                domain=domain,
                reason_code=reason_code,  # type: ignore[arg-type]
                change_id=change_id or None,
            )
        )
    return tuple(sorted(signals, key=lambda item: (item.change_id or "", item.reason_code)))


def _discovery_signals(
    entries: tuple[DiscoveryEvidenceEntry, ...],
    integrity: RetroIntegrity,
) -> tuple[
    DomainEvidenceGapSignal | ConfirmedEscapeSignal | LowPromotionRateSignal | LowReplayStabilitySignal,
    ...,
]:
    signals: list[
        DomainEvidenceGapSignal | ConfirmedEscapeSignal | LowPromotionRateSignal | LowReplayStabilitySignal
    ] = list(_domain_gap_signals(integrity, domain="discovery"))
    # Missing/corrupt domain → gap only; never invent vacuous rates.
    if integrity.status == "incomplete" and not entries:
        return tuple(signals)

    for entry in entries:
        for problem_id in entry.problem_escape_refs:
            signal_id = _stable_signal_id("ESCAPE", f"{entry.change_id}:{problem_id}")
            signals.append(
                ConfirmedEscapeSignal(
                    signal_id=signal_id,
                    summary=f"Human-confirmed escape {problem_id}",
                    occurrence_count=1,
                    recommended_change=(
                        "Capture missed obligations as domain knowledge or regression coverage."
                    ),
                    source_refs=ImprovementSourceRefs(
                        problem_ids=(problem_id,),
                        workflow_evidence_ids=(entry.evidence_id,),
                    ),
                    confidence="high",
                    problem_id=problem_id,
                    change_id=entry.change_id,
                )
            )
        if (
            entry.promoted_count is not None
            and entry.total_counterexamples is not None
            and entry.total_counterexamples > 0
        ):
            rate = entry.promoted_count / entry.total_counterexamples
            if rate < LOW_PROMOTION_RATE_THRESHOLD:
                signal_id = _stable_signal_id(
                    "LOW-PROMO",
                    f"{entry.change_id}:{entry.promoted_count}:{entry.total_counterexamples}",
                )
                signals.append(
                    LowPromotionRateSignal(
                        signal_id=signal_id,
                        summary=(
                            f"Promotion rate {rate:.2f} below {LOW_PROMOTION_RATE_THRESHOLD} "
                            f"for {entry.change_id}"
                        ),
                        occurrence_count=1,
                        recommended_change=(
                            "Raise counterexample→case promotion yield for this campaign window."
                        ),
                        source_refs=ImprovementSourceRefs(workflow_evidence_ids=(entry.evidence_id,)),
                        confidence="high",
                        rate=rate,
                        numerator=entry.promoted_count,
                        denominator=entry.total_counterexamples,
                    )
                )
        if (
            entry.replay_rate is not None
            and entry.replay_attempts is not None
            and entry.replay_attempts > 0
            and entry.replay_success is not None
            and entry.replay_rate < LOW_REPLAY_STABILITY_THRESHOLD
        ):
            signal_id = _stable_signal_id(
                "LOW-REPLAY",
                f"{entry.change_id}:{entry.replay_success}:{entry.replay_attempts}",
            )
            signals.append(
                LowReplayStabilitySignal(
                    signal_id=signal_id,
                    summary=(
                        f"Seed replay stability {entry.replay_rate:.2f} below "
                        f"{LOW_REPLAY_STABILITY_THRESHOLD} for {entry.change_id}"
                    ),
                    occurrence_count=1,
                    recommended_change="Stabilize seed replay before expanding adversarial surfaces.",
                    source_refs=ImprovementSourceRefs(workflow_evidence_ids=(entry.evidence_id,)),
                    confidence="high",
                    rate=entry.replay_rate,
                    success=entry.replay_success,
                    attempts=entry.replay_attempts,
                )
            )
    return tuple(signals)


def _augment_sources_for_signal_refs(
    sources: tuple[RetroSourceDescriptor, ...],
    signals: tuple[object, ...],
    *,
    kind: Literal["discovery_projection", "coverage_gap_projection"],
) -> tuple[RetroSourceDescriptor, ...]:
    resolvable = frozenset(chain.from_iterable(source.evidence_ids for source in sources))
    missing: set[str] = set()
    for signal in signals:
        refs = getattr(signal, "source_refs", None)
        if refs is None:
            continue
        for eid in (*refs.workflow_evidence_ids, *refs.problem_ids):
            if eid not in resolvable:
                missing.add(eid)
    if not missing:
        return sources
    return (
        *sources,
        RetroSourceDescriptor(
            kind=kind,
            sha256=sha256_bytes(canonical_json_bytes({"signal_ref_ids": sorted(missing)})),
            evidence_ids=tuple(sorted(missing)),
        ),
    )


def _discovery_slice(
    retro_id: str,
    window: RetroWindow,
    source_slice: DiscoveryHistorySlice,
) -> DiscoveryEvidenceSlice:
    entries = tuple(
        DiscoveryEvidenceEntry(
            evidence_id=f"{record.change_id}:{record.campaign_id}",
            change_id=record.change_id,
            campaign_id=record.campaign_id,
            counterexample_ids=record.counterexample_ids,
            promotion_receipt_digests=record.promotion_receipt_digests,
            replay_success=record.replay_success,
            replay_attempts=record.replay_attempts,
            replay_rate=record.replay_rate,
            problem_escape_refs=record.problem_escape_refs,
            promoted_count=record.promoted_count,
            total_counterexamples=record.total_counterexamples,
        )
        for record in source_slice.records
    )
    signals = _discovery_signals(entries, source_slice.integrity)
    sources = _augment_sources_for_signal_refs(
        tuple(source_slice.sources),
        signals,
        kind="discovery_projection",
    )
    return DiscoveryEvidenceSlice(
        retro_id=retro_id,
        window=window,
        sources=sources,
        integrity=_integrity(source_slice.integrity),
        deterministic_signals=signals,
        entries=entries,
    )


def _coverage_gap_signals(
    source_slice: CoverageGapHistorySlice,
) -> tuple[DomainEvidenceGapSignal | ReopenedCoverageGapSignal, ...]:
    signals: list[DomainEvidenceGapSignal | ReopenedCoverageGapSignal] = list(
        _domain_gap_signals(source_slice.integrity, domain="coverage_gap")
    )
    for event in source_slice.events:
        if event.event_kind != "reopened":
            continue
        fingerprint = event.locator_fingerprint
        signal_id = _stable_signal_id("REOPEN-GAP", f"{event.change_id}:{fingerprint}")
        evidence_id = f"GAP-{fingerprint}"
        signals.append(
            ReopenedCoverageGapSignal(
                signal_id=signal_id,
                summary=f"Coverage gap reopened: {fingerprint}",
                occurrence_count=1,
                recommended_change="Close the reopened coverage gap with a durable regression case.",
                source_refs=ImprovementSourceRefs(workflow_evidence_ids=(evidence_id, signal_id)),
                confidence="high",
                gap_kind=event.kind,
                locator_fingerprint=fingerprint,
                change_id=event.change_id,
                case_id=event.case_id or None,
                constraint_key=event.constraint_key or None,
                cell=event.cell or None,
                cluster_key=event.cluster_key or None,
            )
        )
    return tuple(signals)


def _coverage_gap_slice(
    retro_id: str,
    window: RetroWindow,
    source_slice: CoverageGapHistorySlice,
) -> CoverageGapEvidenceSlice:
    entries: list[CoverageGapEvidenceEntry] = []
    for record in source_slice.records:
        entries.append(
            CoverageGapEvidenceEntry(
                evidence_id=f"{record.change_id}:{record.batch_id}",
                change_id=record.change_id,
                batch_id=record.batch_id,
                projection_digest=record.projection_digest,
                document_digest=record.document_digest,
                event_kind="current",
            )
        )
    for event in source_slice.events:
        fingerprint = event.locator_fingerprint
        entries.append(
            CoverageGapEvidenceEntry(
                evidence_id=f"GAP-{fingerprint}",
                change_id=event.change_id,
                batch_id=next(
                    (r.batch_id for r in source_slice.records if r.change_id == event.change_id),
                    "unknown",
                ),
                projection_digest=next(
                    (r.projection_digest for r in source_slice.records if r.change_id == event.change_id),
                    "sha256:unknown",
                ),
                document_digest=next(
                    (r.document_digest for r in source_slice.records if r.change_id == event.change_id),
                    "sha256:unknown",
                ),
                event_kind=event.event_kind,
                gap_kind=event.kind,
                locator_fingerprint=fingerprint,
                case_id=event.case_id or None,
                constraint_key=event.constraint_key or None,
                cell=event.cell or None,
                cluster_key=event.cluster_key or None,
            )
        )
    ordered = tuple(sorted(entries, key=lambda item: (item.change_id, item.event_kind, item.evidence_id)))
    signals = _coverage_gap_signals(source_slice)
    sources = list(source_slice.sources)
    resolvable = frozenset(chain.from_iterable(item.evidence_ids for item in sources))
    for entry in ordered:
        if entry.evidence_id in resolvable:
            continue
        sources.append(
            RetroSourceDescriptor(
                kind="coverage_gap_projection",
                change_id=entry.change_id,
                sha256=entry.document_digest,
                evidence_ids=(entry.evidence_id,),
            )
        )
        resolvable = frozenset(chain.from_iterable(item.evidence_ids for item in sources))
    sources_tuple = _augment_sources_for_signal_refs(
        tuple(sources),
        signals,
        kind="coverage_gap_projection",
    )
    return CoverageGapEvidenceSlice(
        retro_id=retro_id,
        window=window,
        sources=sources_tuple,
        integrity=_integrity(source_slice.integrity),
        deterministic_signals=signals,
        entries=ordered,
    )


def _write_domain_signal_doc(path: Path, slice_: DiscoveryEvidenceSlice | CoverageGapEvidenceSlice) -> None:
    data = canonical_json_bytes(slice_)
    doc = SignalDocumentV3(
        retro_id=slice_.retro_id,
        domain=slice_.domain,
        analysis_status="ok",
        analyzer=f"operation:retro-{slice_.domain}-deterministic",
        signals=(),
        slice_sha256=sha256_bytes(data),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() != canonical_json_bytes(doc):
        raise RetroSliceImmutableError(f"retro evidence artifact already differs: {path}")
    if not path.is_file():
        path.write_bytes(canonical_json_bytes(doc))


def materialize_slices(
    sut: Path,
    *,
    retro_id: str,
    selection: RetroWindowSelection,
    issue_history: IssueHistoryReader,
    workflow_history: WorkflowHistoryReader,
    eval_history: EvalHistoryReader,
    write_root: Path,
    discovery_history: DiscoveryHistoryReader | None = None,
    coverage_gap_history: CoverageGapHistoryReader | None = None,
) -> SliceBundle:
    """Resolve one window, materialize typed slices, then write atomically-by-preflight.

    Core domains (issue/workflow/eval) always materialize. Optional discovery /
    coverage_gap readers emit slices + deterministic ``signals/*.json`` that
    assembly verifies when present.
    """
    del sut  # selection is resolved against history readers, not the SUT tree
    assert_path_segment_safe(retro_id, label="retro id")
    resolved: ResolvedRetroWindow = resolve_retro_window(selection, workflow_history=workflow_history)
    issue_source = issue_history.read_window(resolved.to_issue_selection())
    project_head = next(
        (
            source.head_event_id
            for source in issue_source.sources
            if source.kind == "project_problem_ledger" and source.head_event_id
        ),
        None,
    )
    window = resolved.to_context_window().model_copy(update={"project_event_through": project_head})
    workflow_source = workflow_history.read_window(resolved)
    eval_source = eval_history.read_window(resolved)
    discovery_slice = None
    coverage_gap_slice = None
    if resolved.change_ids:
        optional_selection = RetroWindowSelection(
            change_ids=resolved.change_ids,
            last=None,
            batch_scope=selection.batch_scope,
        )
        if discovery_history is not None:
            discovery_source = discovery_history.read_discovery_slice(optional_selection)
            if discovery_source is not None:
                discovery_slice = _discovery_slice(retro_id, window, discovery_source)
        if coverage_gap_history is not None:
            coverage_source = coverage_gap_history.read_coverage_gap_slice(optional_selection)
            if coverage_source is not None:
                coverage_gap_slice = _coverage_gap_slice(retro_id, window, coverage_source)
    bundle = SliceBundle(
        window=window,
        issue=_issue_slice(retro_id, window, issue_source),
        workflow=_workflow_slice(retro_id, window, workflow_source, resolved.integrity),
        eval=_eval_slice(retro_id, window, eval_source),
        discovery=discovery_slice,
        coverage_gap=coverage_gap_slice,
    )
    retro_dir = write_root / "qa" / "retro" / retro_id
    files: dict[Path, object] = {
        retro_dir / "window.json": bundle.window,
        retro_dir / "evidence" / "issue-slice.json": bundle.issue,
        retro_dir / "evidence" / "workflow-slice.json": bundle.workflow,
        retro_dir / "evidence" / "eval-slice.json": bundle.eval,
    }
    if bundle.discovery is not None:
        files[retro_dir / "evidence" / "discovery-slice.json"] = bundle.discovery
    if bundle.coverage_gap is not None:
        files[retro_dir / "evidence" / "coverage_gap-slice.json"] = bundle.coverage_gap
    _write_immutable_bundle(
        files,
        agent_files={
            retro_dir / "evidence" / "agent" / "issue-slice.json": bundle.issue,
            retro_dir / "evidence" / "agent" / "workflow-slice.json": bundle.workflow,
            retro_dir / "evidence" / "agent" / "eval-slice.json": bundle.eval,
        },
    )
    if bundle.discovery is not None:
        _write_domain_signal_doc(retro_dir / "signals" / "discovery.json", bundle.discovery)
    if bundle.coverage_gap is not None:
        _write_domain_signal_doc(retro_dir / "signals" / "coverage_gap.json", bundle.coverage_gap)
    return bundle


__all__ = [
    "LOW_PROMOTION_RATE_THRESHOLD",
    "LOW_REPLAY_STABILITY_THRESHOLD",
    "RetroSliceImmutableError",
    "SliceBundle",
    "materialize_slices",
]
