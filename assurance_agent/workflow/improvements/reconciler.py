"""Deterministic reconciliation of Retro Candidates into Improvement Ledger events.

Public API:
    ImprovementReconciliationPlan
    ImprovementAcceptStatus
    plan_link_or_propose(...)
    reconcile_improvement_candidates(...)
    run_improvement_reconcile(...)
"""

from __future__ import annotations

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
    CandidateBatchInvalid,
    candidate_batch_digest,
    context_sha256,
    read_candidate_document,
    validate_candidate_document,
)
from assurance_agent.retro.types import RetroContext
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

_LOCK_TOKEN = "project:improvement-registry"
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ImprovementReconciliationPlan(BaseModel):
    model_config = _FROZEN

    retro_id: str
    candidate_batch_digest: str
    idempotency_key: str
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


def _source_refs_covered(
    existing: ImprovementSourceRefs, incoming: ImprovementSourceRefs
) -> bool:
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
    candidates: ImprovementCandidateDocument,
    context: RetroContext,
    current: ImprovementLedgerProjection,
) -> ImprovementReconciliationPlan:
    """Validate a Candidate batch and derive deterministic ledger events."""
    validate_candidate_document(context, candidates)
    batch_digest = candidate_batch_digest(candidates)
    key = f"{context.retro_id}:{batch_digest}"
    context_digest = context_sha256(context)
    ts = context.generated_at
    events: list[ImprovementEvent] = []
    for ordinal, candidate in enumerate(candidates.candidates):
        fingerprint = improvement_fingerprint(candidate)
        existing = current.by_fingerprint.get(fingerprint)
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
            )
        )
    return ImprovementReconciliationPlan(
        retro_id=context.retro_id,
        candidate_batch_digest=batch_digest,
        idempotency_key=key,
        events=tuple(events),
    )


def _batch_events(
    events: list[ImprovementEvent], *, batch_key: str
) -> list[ImprovementEvent]:
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
) -> ImprovementAcceptStatus:
    improvement_ids = tuple(sorted({event.improvement_id for event in events}))
    event_ids = tuple(event.event_id for event in events)
    return ImprovementAcceptStatus(
        retro_id=retro_id,
        context_sha256=context_digest,
        candidate_batch_digest=batch_digest,
        idempotency_key=idempotency_key,
        result="accepted",
        improvement_ids=improvement_ids,
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
        context = RetroContext.model_validate(
            json.loads(context_path.read_text(encoding="utf-8"))
        )
    except Exception as exc:
        raise AaError(f"context.json invalid: {context_path}: {exc}") from exc

    candidates = read_candidate_document(retro_dir)
    context_digest = context_sha256(context)
    batch_digest = candidate_batch_digest(candidates)
    key = f"{retro_id}:{batch_digest}"

    try:
        validate_candidate_document(context, candidates)
    except CandidateBatchInvalid as exc:
        status = _failed_status(
            retro_id=retro_id,
            context_digest=context_digest,
            batch_digest=batch_digest,
            idempotency_key=key,
            error=str(exc),
        )
        _write_accept_status(retro_dir, status)
        return status

    locks = ProjectResourceLockManager(project_root)
    with locks.acquire((_LOCK_TOKEN,), timeout_seconds=30.0):
        store = ProjectImprovementStore(project_root)
        existing = read_improvement_events(store.root / "events.jsonl")
        prior = _batch_events(existing, batch_key=key)
        if prior:
            status = _receipt_from_events(
                retro_id=retro_id,
                context_digest=context_digest,
                batch_digest=batch_digest,
                idempotency_key=key,
                events=prior,
            )
            _write_accept_status(retro_dir, status)
            _write_review_queue_md(
                retro_dir, retro_id=retro_id, improvement_ids=status.improvement_ids
            )
            return status

        current = project_improvements(existing)
        plan = reconcile_improvement_candidates(candidates, context, current)
        if plan.events:
            store.append_and_rebuild(plan.events)

        status = _receipt_from_events(
            retro_id=retro_id,
            context_digest=context_digest,
            batch_digest=batch_digest,
            idempotency_key=plan.idempotency_key,
            events=list(plan.events),
        )
        _write_accept_status(retro_dir, status)
        _write_review_queue_md(
            retro_dir, retro_id=retro_id, improvement_ids=status.improvement_ids
        )
        return status
