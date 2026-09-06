"""Build authenticated Retro slices from explicitly supplied committed evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from graph_engine.attempts import AuthorizedAttemptScope, ExecutedAttemptResult
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.retro import (
    AffectedSurface,
    EvalEvidenceEntry,
    EvalEvidenceSlice,
    IssueEvidenceEntry,
    IssueEvidenceSlice,
    LoopRoundEvidenceEntry,
    RetroBuildSlicesInputV1,
    RetroIntegrity,
    RetroSourceDescriptor,
    WorkflowEvidenceSlice,
)
from assurance_improvement.operations.retro import RetroCollectInput
from assurance_intake.contracts import EvidenceArtifactRefV1, LoopRoundHistoryV1
from assurance_intake.contracts.plan import decode_plan
from assurance_quality.contracts.agent import InspectionResultV1
from assurance_quality.contracts.issues import ChangeIssueSnapshot


class RetroSlicesInputError(ValueError):
    """The explicit source set cannot be authenticated or interpreted safely."""


def _read_ref(root: Path, ref: EvidenceArtifactRefV1) -> bytes:
    candidate = root.joinpath(*ref.path.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise RetroSlicesInputError(f"Retro source is unavailable: {ref.path}") from error
    if not resolved.is_file() or resolved.is_symlink():
        raise RetroSlicesInputError(f"Retro source is not a regular file: {ref.path}")
    data = resolved.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.digest:
        raise RetroSlicesInputError(f"Retro source digest drifted: {ref.path}")
    return data


def _json(data: bytes, path: str) -> object:
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RetroSlicesInputError(f"Retro JSON source is invalid: {path}") from error


def _selected(ref: EvidenceArtifactRefV1, change_ids: frozenset[str]) -> bool:
    parts = ref.path.split("/")
    if len(parts) >= 3 and parts[:2] == ["qa", "changes"]:
        return parts[2] in change_ids
    return True


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
    if raw_digest is None and raw_ref is None:
        return None
    if raw_digest is None or raw_ref is None:
        raise RetroSlicesInputError("plan-bound Retro source has an incomplete plan binding")
    try:
        ref = EvidenceArtifactRefV1.model_validate(raw_ref)
    except ValidationError as error:
        raise RetroSlicesInputError("plan-bound Retro source has an invalid plan_ref") from error
    supplied = source_refs.get(ref.path)
    if supplied != ref:
        raise RetroSlicesInputError("plan-bound Retro source did not include its exact plan source")
    plan = decode_plan(_read_ref(project_root, ref), ref)
    if raw_digest != plan.plan_digest:
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


def _inspection_entry(document: InspectionResultV1) -> EvalEvidenceEntry:
    return EvalEvidenceEntry(
        run_id=document.batch_id,
        suite="assurance-quality-inspect",
        verdict=document.status,
        failure_signature="inspection_failed" if document.status == "failed" else None,
        started_at=document.batch_id,
        source_change_ids=(document.change_id,),
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
    refs = tuple(ref for ref in request.source_refs if _selected(ref, selected_changes))
    refs_by_path = {ref.path: ref for ref in refs}
    issue_sources: list[RetroSourceDescriptor] = []
    issue_entries: list[IssueEvidenceEntry] = []
    workflow_sources: list[RetroSourceDescriptor] = []
    eval_sources: list[RetroSourceDescriptor] = []
    workflow_entries: list[LoopRoundEvidenceEntry] = []
    eval_entries: list[EvalEvidenceEntry] = []
    issue_reasons: list[str] = []
    workflow_reasons: list[str] = []
    eval_reasons: list[str] = []
    history_seen = False

    for ref in refs:
        data = _read_ref(project_root, ref)
        change_id = next(
            (change for change in request.window.change_ids if f"qa/changes/{change}/" in ref.path),
            None,
        )
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
            entry = _inspection_entry(inspection)
            eval_entries.append(entry)
            eval_sources.append(
                _descriptor(
                    kind="inspection_outcome",
                    ref=ref,
                    change_id=inspection.change_id,
                    evidence_ids=(entry.run_id,),
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
                    evidence_ids=(item.occurrence_id for item in entries),
                )
            )
            continue
        if "/report/" in ref.path:
            payload = _json(data, ref.path) if ref.path.endswith(".json") else None
            eval_sources.append(
                _descriptor(
                    kind="report_outcome",
                    ref=ref,
                    change_id=change_id,
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
                    change_id=change_id,
                    plan_binding=_source_plan_binding(
                        payload,
                        source_refs=refs_by_path,
                        project_root=project_root,
                    ),
                )
            )

    if not history_seen:
        workflow_reasons.append("loop_history_missing")
    if not issue_sources:
        issue_reasons.append("issue_evidence_absent")
    if not eval_sources:
        eval_reasons.append("evaluation_evidence_absent")

    workflow_entries.sort(
        key=lambda item: (
            item.change_id,
            item.coverage_epoch,
            item.loop_kind,
            item.family or "",
            item.round_index,
            item.evidence_id,
        )
    )
    issue_entries.sort(key=lambda item: (item.change_id, item.occurrence_id))
    eval_entries.sort(key=lambda item: (item.run_id, item.suite))

    def source_key(item: RetroSourceDescriptor) -> tuple[str, str, str]:
        return (item.change_id or "", item.kind, item.sha256)

    issue_sources.sort(key=source_key)
    workflow_sources.sort(key=source_key)
    eval_sources.sort(key=source_key)

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
        ),
        eval_slice=EvalEvidenceSlice(
            retro_id=request.retro_id,
            window=request.window,
            sources=tuple(eval_sources),
            integrity=integrity(eval_reasons),
            entries=tuple(eval_entries),
        ),
    )


class RetroBuildSlicesExecutor:
    async def execute(
        self,
        validated_input: RetroBuildSlicesInputV1,
        scope: AuthorizedAttemptScope,
    ) -> ExecutedAttemptResult[RetroCollectInput]:
        return ExecutedAttemptResult(
            output=build_retro_slices(validated_input, project_root=scope.workspace.project_root)
        )


class RetroBuildSlicesHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            validated = RetroBuildSlicesInputV1.model_validate(request.input)
            output = build_retro_slices(validated, project_root=context.project_root)
        except (RetroSlicesInputError, ValidationError, OSError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)
        return TaskOutcome.succeeded(cast(JSONValue, output.model_dump(mode="json")))


__all__ = [
    "RetroBuildSlicesExecutor",
    "RetroBuildSlicesHandler",
    "RetroSlicesInputError",
    "build_retro_slices",
]
