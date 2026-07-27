"""Materialize immutable Retro v3 evidence slices from typed history readers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.retro_v3 import (
    AffectedSurface,
    BatchMemberEvidenceGapSignal,
    EvalEvidenceEntry,
    EvalEvidenceSlice,
    GateVerdictEvidenceEntry,
    HealingOutcomeEvidenceEntry,
    IssueEvidenceEntry,
    IssueEvidenceSlice,
    RetroIntegrity,
    RetroSourceDescriptor,
    RetroWindow,
    SkillDriftEvidenceEntry,
    TaskFailureEvidenceEntry,
    WorkflowEvidenceSlice,
)
from assurance_agent.artifacts.models.improvements import ImprovementSourceRefs
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_path_segment_safe
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


class RetroSliceImmutableError(AaError):
    """A v3 evidence artifact already exists with different canonical bytes."""


@dataclass(frozen=True)
class SliceBundle:
    window: RetroWindow
    issue: IssueEvidenceSlice
    workflow: WorkflowEvidenceSlice
    eval: EvalEvidenceSlice


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
    return WorkflowEvidenceSlice(
        retro_id=retro_id,
        window=window,
        sources=(*sources, *gap_sources),
        integrity=_integrity(window_integrity, source_slice.integrity),
        deterministic_signals=gap_signals,
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


def _write_immutable_bundle(files: dict[Path, object]) -> None:
    encoded = {path: canonical_json_bytes(value) for path, value in files.items()}
    for path, canonical in encoded.items():
        if path.is_file() and path.read_bytes() != canonical:
            raise RetroSliceImmutableError(f"retro evidence artifact already differs: {path}")
    for path, canonical in encoded.items():
        if path.is_file():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical)


def materialize_slices(
    sut: Path,
    *,
    retro_id: str,
    selection: RetroWindowSelection,
    issue_history: IssueHistoryReader,
    workflow_history: WorkflowHistoryReader,
    eval_history: EvalHistoryReader,
    write_root: Path,
) -> SliceBundle:
    """Resolve one window, materialize three typed slices, then write atomically-by-preflight."""
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
    bundle = SliceBundle(
        window=window,
        issue=_issue_slice(retro_id, window, issue_source),
        workflow=_workflow_slice(retro_id, window, workflow_source, resolved.integrity),
        eval=_eval_slice(retro_id, window, eval_source),
    )
    retro_dir = write_root / "qa" / "retro" / retro_id
    _write_immutable_bundle(
        {
            retro_dir / "window.json": bundle.window,
            retro_dir / "evidence" / "issue-slice.json": bundle.issue,
            retro_dir / "evidence" / "workflow-slice.json": bundle.workflow,
            retro_dir / "evidence" / "eval-slice.json": bundle.eval,
        }
    )
    return bundle


__all__ = ["RetroSliceImmutableError", "SliceBundle", "materialize_slices"]
