"""Build authenticated Retro slices from explicitly supplied committed evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from pydantic import ValidationError

from graph_engine.artifacts import ArtifactReadError, open_artifact
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.retro import (
    AffectedSurface,
    CoverageGapEvidenceEntry,
    CoverageGapEvidenceSlice,
    EvalEvidenceEntry,
    EvalEvidenceSlice,
    IssueEvidenceEntry,
    IssueEvidenceSlice,
    LoopRoundEvidenceEntry,
    RetroBuildSlicesInputV1,
    RetroCollectInput,
    RetroIntegrity,
    RetroSourceDescriptor,
    TaskFailureEvidenceEntry,
    TaskFailureSignal,
    WorkflowEvidenceEntry,
    WorkflowEvidenceSlice,
    WorkflowRuntimeEvidenceV1,
)
from assurance_improvement.contracts.improvements import ImprovementSourceRefs
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_intake.contracts import EvidenceArtifactRefV1, LoopRoundHistoryV1
from assurance_intake.domain.plan_codec import decode_plan
from assurance_quality.contracts.agent import InspectionResultV1
from assurance_quality.contracts.coverage import CoverageGapsDocument
from assurance_quality.contracts.issues import ChangeIssueSnapshot, ObservationDocument


class RetroSlicesInputError(ValueError):
    """The explicit source set cannot be authenticated or interpreted safely."""


def _read_ref(root: Path, ref: EvidenceArtifactRefV1) -> bytes:
    try:
        return open_artifact(root, ref)
    except ArtifactReadError as error:
        raise RetroSlicesInputError(str(error)) from error


def _json(data: bytes, path: str) -> object:
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RetroSlicesInputError(f"Retro JSON source is invalid: {path}") from error


def _selected(ref: EvidenceArtifactRefV1, change_ids: frozenset[str]) -> bool:
    del ref, change_ids
    return True


def _window_change_id(
    ref: EvidenceArtifactRefV1,
    window_ids: Iterable[str],
    payload: object | None = None,
) -> str | None:
    allowed = tuple(window_ids)
    if isinstance(payload, dict):
        raw = payload.get("change_id")
        if isinstance(raw, str) and raw in allowed:
            return raw
    parts = PurePosixPath(ref.path).parts
    return next((change for change in allowed if change in parts), None)


def _descriptor(
    *,
    kind: str,
    ref: EvidenceArtifactRefV1,
    change_id: str | None,
    evidence_ids: Iterable[str] = (),
    plan_binding: tuple[str, EvidenceArtifactRefV1] | None = None,
) -> RetroSourceDescriptor:
    return RetroSourceDescriptor.model_validate(
        {
            "kind": kind,
            "change_id": change_id,
            "sha256": ref.digest,
            "evidence_ids": sorted(set(evidence_ids)),
            "plan_digest": plan_binding[0] if plan_binding is not None else None,
            "plan_ref": plan_binding[1] if plan_binding is not None else None,
        }
    )


def _regular_file_ref(root: Path, relative: str) -> EvidenceArtifactRefV1 | None:
    candidate = root.joinpath(*relative.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(resolved.read_bytes()).hexdigest())


def _inspect_batch_dir(root: Path, batch_id: str) -> tuple[Path | None, str | None]:
    epochs = root / "qa" / "results" / "inspect" / "epochs"
    if not epochs.is_dir() or epochs.is_symlink():
        return None, None
    matches: list[Path] = []
    for epoch in epochs.iterdir():
        if epoch.is_symlink() or not epoch.is_dir():
            continue
        candidate = epoch / "batches" / batch_id
        if candidate.is_dir() and not candidate.is_symlink():
            matches.append(candidate)
    if len(matches) > 1:
        return None, "inspect_batch_ambiguous"
    if not matches:
        return None, None
    return matches[0], None


@dataclass(frozen=True)
class _LockedInspectExtension:
    """Refs reachable from a locked inspection, plus why some were not followed."""

    refs: tuple[EvidenceArtifactRefV1, ...]
    issue_reasons: tuple[str, ...] = ()
    eval_reasons: tuple[str, ...] = ()
    coverage_reasons: tuple[str, ...] = ()


def _extend_locked_inspect_batch(
    refs: tuple[EvidenceArtifactRefV1, ...],
    *,
    project_root: Path,
) -> _LockedInspectExtension:
    by_path = {ref.path: ref for ref in refs}
    extras: list[EvidenceArtifactRefV1] = []
    issue_reasons: list[str] = []
    eval_reasons: list[str] = []
    coverage_reasons: list[str] = []
    for ref in refs:
        if not ref.path.endswith("/inspect/inspection.json"):
            continue
        try:
            inspection = InspectionResultV1.model_validate(_json(_read_ref(project_root, ref), ref.path))
        except (ValidationError, RetroSlicesInputError):
            continue
        batch_dir, ambiguity = _inspect_batch_dir(project_root, inspection.batch_id)
        if ambiguity is not None:
            eval_reasons.append(ambiguity)
        if batch_dir is None:
            continue
        try:
            relative_base = (
                batch_dir.resolve(strict=True).relative_to(project_root.resolve(strict=True)).as_posix()
            )
        except ValueError:
            continue
        locked = {
            "coverage-gaps.json": inspection.coverage_digest,
            "trace.json": inspection.trace_digest,
            "metrics.json": inspection.metrics_digest,
        }
        for name, digest in locked.items():
            extra = _regular_file_ref(project_root, f"{relative_base}/{name}")
            if extra is None:
                continue
            if extra.digest != digest:
                if name == "coverage-gaps.json":
                    coverage_reasons.append("coverage_gap_evidence_digest_drift")
                else:
                    eval_reasons.append("inspect_projection_digest_drift")
                continue
            if extra.path in by_path:
                continue
            extras.append(extra)
            by_path[extra.path] = extra
        observations = _regular_file_ref(project_root, f"{relative_base}/observations.json")
        if observations is not None and observations.path not in by_path:
            try:
                document = ObservationDocument.model_validate(
                    _json(_read_ref(project_root, observations), observations.path)
                )
            except (ValidationError, RetroSlicesInputError):
                issue_reasons.append("observations_evidence_corrupt")
                continue
            if (document.change_id, document.batch_id) == (inspection.change_id, inspection.batch_id):
                extras.append(observations)
                by_path[observations.path] = observations
            else:
                issue_reasons.append("observations_identity_mismatch")
    return _LockedInspectExtension(
        refs=tuple((*refs, *extras)),
        issue_reasons=tuple(issue_reasons),
        eval_reasons=tuple(eval_reasons),
        coverage_reasons=tuple(coverage_reasons),
    )


def _coverage_gap_entries(
    document: CoverageGapsDocument,
    *,
    document_digest: str,
) -> tuple[CoverageGapEvidenceEntry, ...]:
    projection = document.projection_digest.removeprefix("sha256:")
    entries: list[CoverageGapEvidenceEntry] = []
    for gap in document.gaps:
        locator = {
            "case_id": gap.locator.case_id,
            "constraint_key": gap.locator.constraint_key,
            "cell": gap.locator.cell,
            "cluster_key": gap.locator.cluster_key,
        }
        fingerprint = canonical_digest(cast(JSONValue, locator))
        entries.append(
            CoverageGapEvidenceEntry(
                evidence_id=f"coverage-gap-{canonical_digest([document.change_id, document.batch_id, gap.kind, fingerprint])}",
                change_id=document.change_id,
                batch_id=document.batch_id,
                projection_digest=projection,
                document_digest=document_digest,
                event_kind="current",
                gap_kind=gap.kind,
                locator_fingerprint=fingerprint,
                case_id=gap.locator.case_id,
                constraint_key=gap.locator.constraint_key,
                cell=gap.locator.cell,
                cluster_key=gap.locator.cluster_key,
            )
        )
    return tuple(entries)


def _source_plan_binding(
    document: object,
    *,
    source_refs: dict[str, EvidenceArtifactRefV1],
    project_root: Path,
) -> tuple[str, EvidenceArtifactRefV1] | None:
    if not isinstance(document, dict):
        return None
    raw_digest = document.get("plan_digest")
    raw_ref = document.get("plan_ref")
    if raw_ref is None:
        if raw_digest is None:
            return None
        raise RetroSlicesInputError("plan-bound Retro source has an incomplete plan binding")
    try:
        ref = EvidenceArtifactRefV1.model_validate(raw_ref)
    except ValidationError as error:
        raise RetroSlicesInputError("plan-bound Retro source has an invalid plan_ref") from error
    supplied = source_refs.get(ref.path)
    if supplied != ref:
        raise RetroSlicesInputError("plan-bound Retro source did not include its exact plan source")
    plan = decode_plan(_read_ref(project_root, ref), ref)
    if raw_digest is not None and raw_digest != plan.plan_digest:
        raise RetroSlicesInputError("plan-bound Retro source plan_digest does not match its plan")
    return plan.plan_digest, ref


def _history_entry(history: LoopRoundHistoryV1) -> LoopRoundEvidenceEntry:
    return LoopRoundEvidenceEntry(
        evidence_id=history.evidence_id,
        change_id=history.change_id,
        coverage_epoch=history.coverage_epoch,
        loop_kind=history.loop_kind,
        family=history.family,
        round_index=history.round_index,
        outcome=history.outcome,
        source_refs=history.source_refs,
    )


def _execution_entry(document: ExecutionEvidenceV1) -> EvalEvidenceEntry:
    failed = tuple(item for item in document.results if item.status == "failed")
    verdict = "failed" if document.receipt.exit_code != 0 or failed else "passed"
    signature = (
        canonical_digest(
            cast(
                JSONValue,
                {
                    "exit_code": document.receipt.exit_code,
                    "failures": sorted(
                        {hashlib.sha256(item.message.encode()).hexdigest() for item in failed}
                    ),
                },
            )
        )
        if verdict == "failed"
        else None
    )
    return EvalEvidenceEntry(
        run_id=document.batch_id,
        suite="assurance-execution",
        verdict=verdict,
        failure_signature=signature,
        started_at=document.executed_at.isoformat() if document.executed_at is not None else None,
        source_change_ids=(document.change_id,),
        sample_ids=tuple(sorted(item.test for item in failed)),
    )


def _failure_signals(entries: Iterable[WorkflowEvidenceEntry]) -> tuple[TaskFailureSignal, ...]:
    groups: dict[tuple[str, str, str], list[TaskFailureEvidenceEntry]] = {}
    for entry in entries:
        if isinstance(entry, TaskFailureEvidenceEntry):
            groups.setdefault((entry.node_id, entry.error_kind, entry.message_fingerprint), []).append(entry)
    return tuple(
        TaskFailureSignal(
            signal_id=f"task-failure-{canonical_digest(list(key))}",
            node_id=key[0],
            error_kind=key[1],
            message_fingerprint=key[2],
            summary=f"{len(group)} technical failure(s) at {key[0]} ({key[1]}); recovery does not erase failures.",
            occurrence_count=len(group),
            recommended_change="Review the node's contract and failure fingerprint before proposing a targeted correction.",
            source_refs=ImprovementSourceRefs(
                workflow_evidence_ids=tuple(sorted(item.evidence_id for item in group))
            ),
            confidence="high",
        )
        for key, group in sorted(groups.items())
    )


def _issue_entries(document: ChangeIssueSnapshot) -> tuple[IssueEvidenceEntry, ...]:
    observations = {item.observation_id: item for item in document.observations}
    entries: list[IssueEvidenceEntry] = []
    for occurrence in document.occurrences:
        source = next(
            (observations[item] for item in occurrence.observation_ids if item in observations),
            None,
        )
        if source is None:
            continue
        entries.append(
            IssueEvidenceEntry(
                occurrence_id=occurrence.occurrence_id,
                problem_id=occurrence.problem_id,
                change_id=occurrence.change_id,
                batch_id=occurrence.batch_id,
                surface=AffectedSurface(kind=source.target, value=source.source.artifact),
                symptom=source.signature,
                fingerprint=occurrence.problem_id,
                observed_at=source.observed_at,
                classification_hint=occurrence.provisional_assessment.classification,
            )
        )
    return tuple(sorted(entries, key=lambda item: (item.change_id, item.occurrence_id)))


def build_retro_slices(
    request: RetroBuildSlicesInputV1,
    *,
    project_root: Path,
) -> RetroCollectInput:
    selected_changes = frozenset(request.window.change_ids)
    extension = _extend_locked_inspect_batch(
        tuple(ref for ref in request.source_refs if _selected(ref, selected_changes)),
        project_root=project_root,
    )
    refs = extension.refs
    refs_by_path = {ref.path: ref for ref in refs}
    issue_sources: list[RetroSourceDescriptor] = []
    issue_entries: list[IssueEvidenceEntry] = []
    workflow_sources: list[RetroSourceDescriptor] = []
    eval_sources: list[RetroSourceDescriptor] = []
    coverage_sources: list[RetroSourceDescriptor] = []
    coverage_entries: list[CoverageGapEvidenceEntry] = []
    workflow_entries: list[WorkflowEvidenceEntry] = []
    eval_entries: list[EvalEvidenceEntry] = []
    issue_reasons: list[str] = list(extension.issue_reasons)
    workflow_reasons: list[str] = []
    eval_reasons: list[str] = list(extension.eval_reasons)
    coverage_reasons: list[str] = list(extension.coverage_reasons)
    history_seen = False
    runtime_changes: set[str] = set()
    executions: dict[str, ExecutionEvidenceV1] = {}
    inspections: list[InspectionResultV1] = []
    seen_runs: dict[tuple[str, str], str] = {}

    for ref in refs:
        data = _read_ref(project_root, ref)
        if ref.path.endswith("/workflow-evidence.json"):
            try:
                runtime = WorkflowRuntimeEvidenceV1.model_validate(_json(data, ref.path))
            except (ValidationError, RetroSlicesInputError):
                workflow_reasons.append("runtime_evidence_corrupt")
                continue
            if runtime.change_id not in selected_changes:
                workflow_reasons.append("runtime_evidence_window_mismatch")
                continue
            runtime_changes.add(runtime.change_id)
            workflow_reasons.extend(runtime.integrity.reasons)
            if runtime.integrity.status == "incomplete":
                workflow_reasons.append("runtime_evidence_incomplete")
            workflow_entries.extend(runtime.entries)
            workflow_sources.append(
                _descriptor(
                    kind="workflow_ledger",
                    ref=ref,
                    change_id=runtime.change_id,
                    evidence_ids=(entry.evidence_id for entry in runtime.entries),
                )
            )
            continue
        if ref.path.endswith("/execute-result.json"):
            try:
                payload = _json(data, ref.path)
                execution = ExecutionEvidenceV1.model_validate(payload)
            except (ValidationError, RetroSlicesInputError):
                eval_reasons.append("execution_evidence_corrupt")
                continue
            if execution.change_id not in selected_changes:
                eval_reasons.append("execution_window_mismatch")
                continue
            plan_binding = _source_plan_binding(payload, source_refs=refs_by_path, project_root=project_root)
            entry = _execution_entry(execution)
            if execution.status != entry.verdict:
                eval_reasons.append("execution_status_mismatch")
            if execution.executed_at is None:
                eval_reasons.append("execution_time_missing")
            key = (execution.change_id, execution.batch_id)
            if key in seen_runs and seen_runs[key] != ref.digest:
                raise RetroSlicesInputError("conflicting execution evidence for the same run")
            if key not in seen_runs:
                eval_entries.append(entry)
            seen_runs[key] = ref.digest
            executions[ref.digest] = execution
            eval_sources.append(
                _descriptor(
                    kind="eval_run",
                    ref=ref,
                    change_id=execution.change_id,
                    evidence_ids=(entry.run_id,),
                    plan_binding=plan_binding,
                )
            )
            continue
        if "/rounds/" in ref.path and "/epochs/" in ref.path and ref.path.endswith(".json"):
            history_seen = True
            try:
                history = LoopRoundHistoryV1.model_validate(_json(data, ref.path))
            except (ValidationError, RetroSlicesInputError):
                workflow_reasons.append("loop_history_corrupt")
                continue
            if history.change_id not in selected_changes:
                workflow_reasons.append("loop_history_window_mismatch")
                continue
            entry = _history_entry(history)
            workflow_entries.append(entry)
            workflow_sources.append(
                _descriptor(
                    kind="loop_round_history",
                    ref=ref,
                    change_id=history.change_id,
                    evidence_ids=(entry.evidence_id,),
                )
            )
            continue
        if ref.path.endswith("/inspect/inspection.json"):
            payload = _json(data, ref.path)
            try:
                inspection = InspectionResultV1.model_validate(payload)
            except (ValidationError, RetroSlicesInputError):
                eval_reasons.append("inspection_evidence_corrupt")
                continue
            if inspection.change_id not in selected_changes:
                eval_reasons.append("inspection_window_mismatch")
                continue
            inspections.append(inspection)
            eval_sources.append(
                _descriptor(
                    kind="inspection_outcome",
                    ref=ref,
                    change_id=inspection.change_id,
                    plan_binding=_source_plan_binding(
                        payload,
                        source_refs=refs_by_path,
                        project_root=project_root,
                    ),
                )
            )
            continue
        if ref.path.startswith("issues/") or "/issues/" in ref.path:
            try:
                snapshot = ChangeIssueSnapshot.model_validate(_json(data, ref.path))
            except (ValidationError, RetroSlicesInputError):
                issue_reasons.append("issue_evidence_corrupt")
                continue
            if snapshot.change_id not in selected_changes:
                issue_reasons.append("issue_window_mismatch")
                continue
            entries = _issue_entries(snapshot)
            issue_entries.extend(entries)
            issue_sources.append(
                _descriptor(
                    kind="change_issue_ledger",
                    ref=ref,
                    change_id=snapshot.change_id,
                    # Issue analysis may cite an occurrence or the problem that groups it;
                    # both identities come from this one authenticated snapshot.
                    evidence_ids=(
                        value for item in entries for value in (item.occurrence_id, item.problem_id)
                    ),
                )
            )
            continue
        if ref.path.endswith("/coverage-gaps.json"):
            try:
                payload = _json(data, ref.path)
                document = CoverageGapsDocument.model_validate(payload)
            except (ValidationError, RetroSlicesInputError):
                coverage_reasons.append("coverage_gap_evidence_corrupt")
                continue
            if document.change_id not in selected_changes:
                coverage_reasons.append("coverage_gap_window_mismatch")
                continue
            entries = _coverage_gap_entries(document, document_digest=ref.digest)
            coverage_entries.extend(entries)
            coverage_sources.append(
                _descriptor(
                    kind="coverage_gap_projection",
                    ref=ref,
                    change_id=document.change_id,
                    evidence_ids=(item.evidence_id for item in entries),
                )
            )
            continue
        if "/report/" in ref.path:
            payload = _json(data, ref.path) if ref.path.endswith(".json") else None
            eval_sources.append(
                _descriptor(
                    kind="report_outcome",
                    ref=ref,
                    change_id=_window_change_id(ref, request.window.change_ids, payload),
                    plan_binding=_source_plan_binding(
                        payload,
                        source_refs=refs_by_path,
                        project_root=project_root,
                    ),
                )
            )
            continue
        if "/inspect/" in ref.path:
            payload = _json(data, ref.path) if ref.path.endswith(".json") else None
            eval_sources.append(
                _descriptor(
                    kind="inspection_outcome",
                    ref=ref,
                    change_id=_window_change_id(ref, request.window.change_ids, payload),
                    plan_binding=_source_plan_binding(
                        payload,
                        source_refs=refs_by_path,
                        project_root=project_root,
                    ),
                )
            )

    if not history_seen:
        workflow_reasons.append("loop_history_missing")
    if not selected_changes.issubset(runtime_changes):
        workflow_reasons.append("task_failure_evidence_absent")
    # Loop outcomes and runtime errors do not establish adherence to a skill.
    workflow_reasons.append("skill_drift_not_assessed")
    if not issue_sources:
        issue_reasons.append("issue_evidence_absent")
    if not eval_entries:
        eval_reasons.append("evaluation_evidence_absent")

    for inspection in inspections:
        execution = executions.get(inspection.execution_digest)
        if execution is None:
            eval_reasons.append("inspection_execution_missing")
        elif (execution.change_id, execution.batch_id) != (inspection.change_id, inspection.batch_id):
            eval_reasons.append("inspection_execution_identity_mismatch")

    unique_workflow: dict[str, WorkflowEvidenceEntry] = {}
    for workflow_entry in workflow_entries:
        previous = unique_workflow.setdefault(workflow_entry.evidence_id, workflow_entry)
        if previous != workflow_entry:
            raise RetroSlicesInputError("conflicting workflow evidence for the same evidence ID")
    workflow_entries = list(unique_workflow.values())

    workflow_entries.sort(
        key=lambda item: (
            item.change_id,
            item.entry_kind,
            item.coverage_epoch if isinstance(item, LoopRoundEvidenceEntry) else 0,
            item.loop_kind if isinstance(item, LoopRoundEvidenceEntry) else "",
            (item.family or "") if isinstance(item, LoopRoundEvidenceEntry) else "",
            item.round_index if isinstance(item, LoopRoundEvidenceEntry) else 0,
            item.evidence_id,
        )
    )
    issue_entries.sort(key=lambda item: (item.change_id, item.occurrence_id))
    eval_entries.sort(key=lambda item: (item.run_id, item.suite))
    coverage_entries.sort(key=lambda item: (item.change_id, item.batch_id, item.evidence_id))

    def source_key(item: RetroSourceDescriptor) -> tuple[str, str, str]:
        return (item.change_id or "", item.kind, item.sha256)

    issue_sources.sort(key=source_key)
    workflow_sources.sort(key=source_key)
    eval_sources.sort(key=source_key)
    coverage_sources.sort(key=source_key)

    def integrity(reasons: list[str]) -> RetroIntegrity:
        canonical = tuple(sorted(set(reasons)))
        return (
            RetroIntegrity(status="incomplete", reasons=canonical)
            if canonical
            else RetroIntegrity(status="complete")
        )

    return RetroCollectInput(
        retro_id=request.retro_id,
        window=request.window,
        issue_slice=IssueEvidenceSlice(
            retro_id=request.retro_id,
            window=request.window,
            sources=tuple(issue_sources),
            integrity=integrity(issue_reasons),
            entries=tuple(issue_entries),
        ),
        workflow_slice=WorkflowEvidenceSlice(
            retro_id=request.retro_id,
            window=request.window,
            sources=tuple(workflow_sources),
            integrity=integrity(workflow_reasons),
            entries=tuple(workflow_entries),
            deterministic_signals=_failure_signals(workflow_entries),
        ),
        eval_slice=EvalEvidenceSlice(
            retro_id=request.retro_id,
            window=request.window,
            sources=tuple(eval_sources),
            integrity=integrity(eval_reasons),
            entries=tuple(eval_entries),
        ),
        coverage_gap_slice=(
            CoverageGapEvidenceSlice(
                retro_id=request.retro_id,
                window=request.window,
                sources=tuple(coverage_sources),
                integrity=integrity(coverage_reasons),
                entries=tuple(coverage_entries),
            )
            if coverage_sources or coverage_entries or coverage_reasons
            else None
        ),
    )


class RetroBuildSlicesHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        from assurance_improvement.contracts.handoff import SLICE_PATH
        from assurance_improvement.operations.files import stage_named

        try:
            validated = RetroBuildSlicesInputV1.model_validate(request.input)
            output = build_retro_slices(validated, project_root=context.project_root)
            for name, document in (
                ("issue", output.issue_slice),
                ("workflow", output.workflow_slice),
                ("eval", output.eval_slice),
                ("discovery", output.discovery_slice),
                ("coverage_gap", output.coverage_gap_slice),
            ):
                if document is not None:
                    stage_named(context, SLICE_PATH[name], document)
        except (RetroSlicesInputError, ValidationError, OSError, ValueError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=True)
        return TaskOutcome.succeeded(cast(JSONValue, output.model_dump(mode="json")))


__all__ = [
    "RetroBuildSlicesHandler",
    "RetroSlicesInputError",
    "build_retro_slices",
]
