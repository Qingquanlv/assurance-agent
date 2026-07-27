"""Deterministic reconciliation of Retro Candidates into Improvement Ledger events.

Public API:
    ImprovementReconciliationPlan
    ImprovementAcceptStatus
    plan_link_or_propose(...)
    reconcile_improvement_candidates(...)
    run_improvement_reconcile(...)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from assurance_agent.artifacts.models.improvements import (
    ImprovementCandidate,
    ImprovementCandidateDocument,
    ImprovementLedgerProjection,
    ImprovementSourceRefs,
)
from assurance_agent.exceptions import AaError
from assurance_agent.retro.candidates import (
    CANDIDATE_DOCUMENT_NAME,
    CandidateBatchInvalid,
    candidate_batch_digest,
    context_sha256,
    read_candidate_document,
    validate_candidate_document,
)
from assurance_agent.retro.types import RetroContext
from assurance_agent.artifacts.models.retro_v3 import (
    ImprovementCandidateV3,
    ImprovementCandidateDocumentV3,
    RetroContextV3,
)
from assurance_agent.artifacts.models.improvement_outbox import ImprovementOutboxEntry
from assurance_agent.artifacts.models.retro_batch import RetroPipelineFailure
from assurance_agent.workflow.graph.project_locks import ProjectResourceLockManager
from assurance_agent.workflow.improvements.events import (
    ImprovementEvent,
    ImprovementEvidenceLinkedEvent,
    ImprovementProposedEvent,
    ImprovementSupersededEvent,
    read_improvement_events,
)
from assurance_agent.workflow.improvements.identity import (
    improvement_event_id,
    improvement_fingerprint,
    improvement_id_for_fingerprint,
)
from assurance_agent.workflow.improvements.ledger import (
    ProjectImprovementStore,
    atomic_write_json,
)
from assurance_agent.workflow.improvements.projection import project_improvements
from assurance_agent.workflow.improvements.review_subject import (
    build_review_subject,
    publish_review_subject,
)

_LOCK_TOKEN = "project:improvement-registry"
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ImprovementReconciliationPlan(BaseModel):
    model_config = _FROZEN

    retro_id: str
    candidate_batch_digest: str
    idempotency_key: str
    improvement_ids: tuple[str, ...] = ()
    events: tuple[ImprovementEvent, ...] = ()


class ImprovementAcceptStatus(BaseModel):
    """Current-run receipt written to ``accept-status.json``."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    retro_id: str
    context_sha256: str
    candidate_batch_digest: str
    idempotency_key: str
    result: Literal["accepted", "failed"]
    improvement_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    error: str | None = None


def _event_idempotency_key(batch_key: str, event_type: str, ordinal: int) -> str:
    return f"{batch_key}:{event_type}:{ordinal}"


def _source_refs_covered(existing: ImprovementSourceRefs, incoming: ImprovementSourceRefs) -> bool:
    for field in (
        "problem_ids",
        "occurrence_ids",
        "issue_event_ids",
        "workflow_evidence_ids",
        "eval_run_ids",
    ):
        if set(getattr(incoming, field)) - set(getattr(existing, field)):
            return False
    return True


def plan_link_or_propose(
    candidate: ImprovementCandidate,
    existing_id: str | None,
    *,
    current: ImprovementLedgerProjection,
    idempotency_key: str,
    ordinal: int,
    next_seq: int,
    retro_id: str,
    context_digest: str,
    candidate_batch_digest: str,
    ts: str,
    review_subject_sha256: str | None = None,
) -> list[ImprovementEvent]:
    """Derive propose and/or evidence-link events for one Candidate."""
    fingerprint = improvement_fingerprint(candidate)
    improvement_id = existing_id or improvement_id_for_fingerprint(fingerprint)

    if existing_id is not None:
        existing = current.improvements[existing_id]
        if _source_refs_covered(existing.source_refs, candidate.source_refs):
            return []
        event_type = "improvement_evidence_linked"
        event_key = _event_idempotency_key(idempotency_key, event_type, ordinal)
        return [
            ImprovementEvidenceLinkedEvent(
                schema_version="1.0",
                seq=next_seq,
                event_id=improvement_event_id(idempotency_key, event_type, ordinal),
                idempotency_key=event_key,
                ts=ts,
                improvement_id=existing_id,
                expected_improvement_version=existing.version,
                type="improvement_evidence_linked",
                source_refs=candidate.source_refs,
                retro_id=retro_id,
                candidate_id=candidate.candidate_id,
                context_sha256=context_digest,
                candidate_batch_digest=candidate_batch_digest,
                review_subject_sha256=review_subject_sha256,
            )
        ]

    events: list[ImprovementEvent] = []
    propose_type = "improvement_proposed"
    propose_key = _event_idempotency_key(idempotency_key, propose_type, ordinal)
    events.append(
        ImprovementProposedEvent(
            schema_version="1.0",
            seq=next_seq,
            event_id=improvement_event_id(idempotency_key, propose_type, ordinal),
            idempotency_key=propose_key,
            ts=ts,
            improvement_id=improvement_id,
            expected_improvement_version=0,
            type="improvement_proposed",
            fingerprint=fingerprint,
            fingerprint_version="1",
            kind=candidate.kind,
            delivery=candidate.delivery,
            source_refs=candidate.source_refs,
            target=candidate.target,
            rationale=candidate.rationale,
            proposed_change=candidate.proposed_change,
            knowledge_delta=candidate.knowledge_delta,
            verification=candidate.verification,
            risk=candidate.risk,
            confidence=candidate.confidence,
            retro_id=retro_id,
            candidate_id=candidate.candidate_id,
            context_sha256=context_digest,
            candidate_batch_digest=candidate_batch_digest,
            supersedes=candidate.supersedes,
            review_subject_sha256=review_subject_sha256,
        )
    )

    if candidate.supersedes and candidate.supersedes in current.improvements:
        old = current.improvements[candidate.supersedes]
        superseded_type = "improvement_superseded"
        superseded_key = _event_idempotency_key(idempotency_key, superseded_type, ordinal)
        events.append(
            ImprovementSupersededEvent(
                schema_version="1.0",
                seq=next_seq + 1,
                event_id=improvement_event_id(idempotency_key, superseded_type, ordinal),
                idempotency_key=superseded_key,
                ts=ts,
                improvement_id=candidate.supersedes,
                expected_improvement_version=old.version,
                type="improvement_superseded",
                superseded_by=improvement_id,
                reason=f"superseded by {improvement_id} via {candidate.candidate_id}",
            )
        )
    return events


def reconcile_improvement_candidates(
    candidates: ImprovementCandidateDocument | ImprovementCandidateDocumentV3,
    context: RetroContext | RetroContextV3,
    current: ImprovementLedgerProjection,
    *,
    pipeline_failures: tuple[RetroPipelineFailure, ...] = (),
) -> ImprovementReconciliationPlan:
    """Validate a Candidate batch and derive deterministic ledger events."""
    validate_candidate_document(context, candidates)
    batch_digest = candidate_batch_digest(candidates)
    key = f"{context.retro_id}:{batch_digest}"
    context_digest = context_sha256(context)
    ts = context.generated_at
    events: list[ImprovementEvent] = []
    improvement_ids: set[str] = set()
    for ordinal, candidate in enumerate(candidates.candidates):
        fingerprint = improvement_fingerprint(candidate)
        existing = current.by_fingerprint.get(fingerprint)
        improvement_id = existing or improvement_id_for_fingerprint(fingerprint)
        improvement_ids.add(improvement_id)
        subject_sha256 = None
        if isinstance(candidate, ImprovementCandidateV3) and isinstance(context, RetroContextV3):
            _, subject_sha256, _ = build_review_subject(
                candidate,
                context,
                improvement_id=improvement_id,
                candidate_batch_digest=batch_digest,
                pipeline_failures=pipeline_failures,
            )
        events.extend(
            plan_link_or_propose(
                candidate,
                existing,
                current=current,
                idempotency_key=key,
                ordinal=ordinal,
                next_seq=current.last_seq + len(events) + 1,
                retro_id=context.retro_id,
                context_digest=context_digest,
                candidate_batch_digest=batch_digest,
                ts=ts,
                review_subject_sha256=subject_sha256,
            )
        )
    return ImprovementReconciliationPlan(
        retro_id=context.retro_id,
        candidate_batch_digest=batch_digest,
        idempotency_key=key,
        improvement_ids=tuple(sorted(improvement_ids)),
        events=tuple(events),
    )


def _publish_review_subjects(
    project_root: Path,
    candidates: ImprovementCandidateDocument | ImprovementCandidateDocumentV3,
    context: RetroContext | RetroContextV3,
    current: ImprovementLedgerProjection,
    *,
    pipeline_failures: tuple[RetroPipelineFailure, ...] = (),
) -> None:
    if not isinstance(candidates, ImprovementCandidateDocumentV3) or not isinstance(context, RetroContextV3):
        return
    batch_digest = candidate_batch_digest(candidates)
    for candidate in candidates.candidates:
        fingerprint = improvement_fingerprint(candidate)
        improvement_id = current.by_fingerprint.get(fingerprint) or improvement_id_for_fingerprint(
            fingerprint
        )
        _, subject_sha256, subject_bytes = build_review_subject(
            candidate,
            context,
            improvement_id=improvement_id,
            candidate_batch_digest=batch_digest,
            pipeline_failures=pipeline_failures,
        )
        publish_review_subject(project_root, subject_sha256, subject_bytes)


def _batch_events(events: list[ImprovementEvent], *, batch_key: str) -> list[ImprovementEvent]:
    """Locate ledger events belonging to ``retro_id:batch_digest`` idempotency key."""
    prefix = f"{batch_key}:"
    return [event for event in events if event.idempotency_key.startswith(prefix)]


def _receipt_from_events(
    *,
    retro_id: str,
    context_digest: str,
    batch_digest: str,
    idempotency_key: str,
    events: list[ImprovementEvent],
    improvement_ids: tuple[str, ...] | None = None,
) -> ImprovementAcceptStatus:
    canonical_ids = (
        improvement_ids
        if improvement_ids is not None
        else tuple(sorted({event.improvement_id for event in events}))
    )
    event_ids = tuple(event.event_id for event in events)
    return ImprovementAcceptStatus(
        retro_id=retro_id,
        context_sha256=context_digest,
        candidate_batch_digest=batch_digest,
        idempotency_key=idempotency_key,
        result="accepted",
        improvement_ids=canonical_ids,
        event_ids=event_ids,
    )


def _write_accept_status(retro_dir: Path, status: ImprovementAcceptStatus) -> None:
    atomic_write_json(
        retro_dir / "accept-status.json",
        status.model_dump(mode="json"),
    )


def _write_review_queue_md(retro_dir: Path, *, retro_id: str, improvement_ids: tuple[str, ...]) -> None:
    lines = [
        f"# Improvement review queue — {retro_id}",
        "",
    ]
    if improvement_ids:
        lines.append("Canonical Improvements from this Retro batch:")
        lines.append("")
        for improvement_id in improvement_ids:
            lines.append(f"- `{improvement_id}`")
    else:
        lines.append("No Improvements from this Retro batch.")
    lines.append("")
    (retro_dir / "review-queue.md").write_text("\n".join(lines), encoding="utf-8")


def _failed_status(
    *,
    retro_id: str,
    context_digest: str,
    batch_digest: str,
    idempotency_key: str,
    error: str,
) -> ImprovementAcceptStatus:
    return ImprovementAcceptStatus(
        retro_id=retro_id,
        context_sha256=context_digest,
        candidate_batch_digest=batch_digest,
        idempotency_key=idempotency_key,
        result="failed",
        error=error,
    )


def _raw_candidate_file_digest(retro_dir: Path) -> str:
    """Digest raw Candidate file bytes when the document cannot be schema-parsed."""
    path = retro_dir / CANDIDATE_DOCUMENT_NAME
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_failed_receipt(
    retro_dir: Path,
    *,
    retro_id: str,
    context_digest: str,
    batch_digest: str,
    error: str,
) -> ImprovementAcceptStatus:
    key = f"{retro_id}:{batch_digest}"
    status = _failed_status(
        retro_id=retro_id,
        context_digest=context_digest,
        batch_digest=batch_digest,
        idempotency_key=key,
        error=error,
    )
    _write_accept_status(retro_dir, status)
    return status


def run_improvement_reconcile(project_root: Path, *, retro_id: str) -> ImprovementAcceptStatus:
    """Validate → lock → append/rebuild → write current-run accept receipt.

    Validation of the complete Candidate document happens before the project lock.
    Under ``project:improvement-registry``, the ledger is reloaded, the plan is
    recomputed, events are appended, and ``accept-status.json`` is written.
    Validation failure writes a failed receipt only in the current Retro run.
    """
    retro_dir = project_root / "qa" / "retro" / retro_id
    context_path = retro_dir / "context.json"
    if not context_path.is_file():
        raise AaError(f"context.json missing: {context_path}")

    try:
        context_raw = json.loads(context_path.read_text(encoding="utf-8"))
        context = (
            RetroContextV3.model_validate(context_raw)
            if isinstance(context_raw, dict) and context_raw.get("schema_version") == "3"
            else RetroContext.model_validate(context_raw)
        )
    except Exception as exc:
        raise AaError(f"context.json invalid: {context_path}: {exc}") from exc

    context_digest = context_sha256(context)
    try:
        candidates = read_candidate_document(
            retro_dir,
            expected_schema="3" if isinstance(context, RetroContextV3) else "2",
        )
    except CandidateBatchInvalid as exc:
        # Schema-invalid documents never reach semantic validate; still write a
        # failed current-run receipt with a stable raw-file digest.
        return _write_failed_receipt(
            retro_dir,
            retro_id=retro_id,
            context_digest=context_digest,
            batch_digest=_raw_candidate_file_digest(retro_dir),
            error=str(exc),
        )

    batch_digest = candidate_batch_digest(candidates)
    key = f"{retro_id}:{batch_digest}"

    try:
        validate_candidate_document(context, candidates)
    except CandidateBatchInvalid as exc:
        return _write_failed_receipt(
            retro_dir,
            retro_id=retro_id,
            context_digest=context_digest,
            batch_digest=batch_digest,
            error=str(exc),
        )

    locks = ProjectResourceLockManager(project_root)
    with locks.acquire((_LOCK_TOKEN,), timeout_seconds=30.0):
        store = ProjectImprovementStore(project_root)
        existing = read_improvement_events(store.root / "events.jsonl")
        prior = _batch_events(existing, batch_key=key)
        if prior:
            current = project_improvements(existing)
            _publish_review_subjects(project_root, candidates, context, current)
            plan = reconcile_improvement_candidates(candidates, context, current)
            status = _receipt_from_events(
                retro_id=retro_id,
                context_digest=context_digest,
                batch_digest=batch_digest,
                idempotency_key=key,
                events=prior,
                improvement_ids=plan.improvement_ids,
            )
            _write_accept_status(retro_dir, status)
            _write_review_queue_md(retro_dir, retro_id=retro_id, improvement_ids=status.improvement_ids)
            return status

        current = project_improvements(existing)
        _publish_review_subjects(project_root, candidates, context, current)
        plan = reconcile_improvement_candidates(candidates, context, current)
        if plan.events:
            store.append_and_rebuild(plan.events)

        status = _receipt_from_events(
            retro_id=retro_id,
            context_digest=context_digest,
            batch_digest=batch_digest,
            idempotency_key=plan.idempotency_key,
            events=list(plan.events),
            improvement_ids=plan.improvement_ids,
        )
        _write_accept_status(retro_dir, status)
        _write_review_queue_md(retro_dir, retro_id=retro_id, improvement_ids=status.improvement_ids)
        return status


def reconcile_outbox_entry(project_root: Path, entry: ImprovementOutboxEntry) -> ImprovementAcceptStatus:
    """Reconcile one self-contained outbox entry without reading Retro history."""
    context = entry.context
    candidates = ImprovementCandidateDocumentV3(
        retro_id=entry.retro_id,
        context_sha256=entry.context_sha256,
        candidates=(entry.candidate,),
    )
    validate_candidate_document(context, candidates)
    batch_digest = candidate_batch_digest(candidates)
    key = f"{entry.retro_id}:{batch_digest}"
    locks = ProjectResourceLockManager(project_root)
    with locks.acquire((_LOCK_TOKEN,), timeout_seconds=30.0):
        store = ProjectImprovementStore(project_root)
        existing = read_improvement_events(store.root / "events.jsonl")
        prior = _batch_events(existing, batch_key=key)
        current = project_improvements(existing)
        pipeline_failures = (entry.pipeline_failure,) if entry.pipeline_failure is not None else ()
        _publish_review_subjects(
            project_root,
            candidates,
            context,
            current,
            pipeline_failures=pipeline_failures,
        )
        plan = reconcile_improvement_candidates(
            candidates,
            context,
            current,
            pipeline_failures=pipeline_failures,
        )
        if not prior and plan.events:
            store.append_and_rebuild(plan.events)
            prior = list(plan.events)
        return _receipt_from_events(
            retro_id=entry.retro_id,
            context_digest=entry.context_sha256,
            batch_digest=batch_digest,
            idempotency_key=key,
            events=prior,
            improvement_ids=plan.improvement_ids,
        )
