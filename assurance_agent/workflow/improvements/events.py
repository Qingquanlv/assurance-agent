"""Strict append-only event adapters for the Project Improvement Ledger.

The ledger is a JSONL file. Every line is a JSON object containing the envelope
fields (schema_version, seq, event_id, idempotency_key, ts, improvement_id,
expected_improvement_version) and the typed event payload identified by ``type``.

Adapters use ``extra="forbid"`` discriminated unions: an unknown ``type`` value
or an undeclared field raises ``ValidationError`` and the whole read is rejected
as a ledger integrity failure.

Public API (all other names are implementation details):
    IMPROVEMENT_EVENT_ADAPTER
    ImprovementEvent
    ImprovementLedgerIntegrityError
    read_improvement_events(path) -> list[ImprovementEvent]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from assurance_agent.artifacts.models.common import NonEmptyStr
from assurance_agent.artifacts.models.data_knowledge import PersistedDataKnowledgeProposal
from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementKind,
    ImprovementSourceRefs,
    ImprovementVerification,
)
from assurance_agent.exceptions import AaError

EvalOutcome = Literal["passed", "regressed", "awaiting_baseline", "error"]


class ImprovementLedgerIntegrityError(AaError):
    """Raised when an Improvement ledger file fails strict validation during a read."""


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class _BaseImprovementEvent(BaseModel):
    """Common envelope fields shared by every Improvement event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    seq: int = Field(ge=1)
    event_id: NonEmptyStr
    idempotency_key: NonEmptyStr
    ts: NonEmptyStr
    improvement_id: NonEmptyStr
    expected_improvement_version: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Event variants
# ---------------------------------------------------------------------------


class ImprovementProposedEvent(_BaseImprovementEvent):
    type: Literal["improvement_proposed"]
    fingerprint: NonEmptyStr
    fingerprint_version: Literal["1"] = "1"
    kind: ImprovementKind
    delivery: DeliveryKind
    source_refs: ImprovementSourceRefs
    target: NonEmptyStr
    rationale: NonEmptyStr
    proposed_change: NonEmptyStr
    knowledge_delta: PersistedDataKnowledgeProposal | None = None
    verification: ImprovementVerification
    risk: Literal["low", "medium", "high"]
    confidence: Literal["low", "medium", "high"]
    retro_id: NonEmptyStr
    candidate_id: NonEmptyStr
    context_sha256: NonEmptyStr
    candidate_batch_digest: NonEmptyStr
    supersedes: NonEmptyStr | None = None
    review_subject_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class ImprovementEvidenceLinkedEvent(_BaseImprovementEvent):
    type: Literal["improvement_evidence_linked"]
    source_refs: ImprovementSourceRefs
    retro_id: NonEmptyStr
    candidate_id: NonEmptyStr
    context_sha256: NonEmptyStr
    candidate_batch_digest: NonEmptyStr
    review_subject_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class ImprovementReviewApprovedEvent(_BaseImprovementEvent):
    type: Literal["improvement_review_approved"]
    who: NonEmptyStr
    reason: NonEmptyStr
    review_id: NonEmptyStr


class ImprovementAutoReviewApprovedEvent(_BaseImprovementEvent):
    type: Literal["improvement_auto_review_approved"]
    review_id: NonEmptyStr
    subject_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    assessment_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy_version: NonEmptyStr
    reviewer: Literal["skill:aa-improvement-reviewer"] = "skill:aa-improvement-reviewer"


class ImprovementAutoReviewRecordedEvent(_BaseImprovementEvent):
    type: Literal["improvement_auto_review_recorded"]
    review_id: NonEmptyStr
    subject_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    assessment_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy_version: NonEmptyStr
    verdict: Literal["changes_requested", "needs_human_review", "reject_advice", "review_error"]
    reason_code: NonEmptyStr


class ImprovementReviewRejectedEvent(_BaseImprovementEvent):
    type: Literal["improvement_review_rejected"]
    who: NonEmptyStr
    reason: NonEmptyStr
    review_id: NonEmptyStr


class ImprovementReworkRequestedEvent(_BaseImprovementEvent):
    type: Literal["improvement_rework_requested"]
    who: NonEmptyStr
    reason: NonEmptyStr
    review_id: NonEmptyStr


class ImprovementEvalRequestedEvent(_BaseImprovementEvent):
    type: Literal["improvement_eval_requested"]
    eval_run_id: NonEmptyStr
    suites: list[NonEmptyStr] = Field(min_length=1)
    staged_sha256: NonEmptyStr
    baseline_sha256: NonEmptyStr | None = None


class ImprovementEvalCompletedEvent(_BaseImprovementEvent):
    type: Literal["improvement_eval_completed"]
    eval_run_id: NonEmptyStr
    outcome: EvalOutcome
    report_sha256: NonEmptyStr
    staged_sha256: NonEmptyStr
    baseline_sha256: NonEmptyStr | None = None
    error: NonEmptyStr | None = None


class ImprovementExportedEvent(_BaseImprovementEvent):
    type: Literal["improvement_exported"]
    artifact_path: NonEmptyStr
    artifact_sha256: NonEmptyStr


class ImprovementAppliedEvent(_BaseImprovementEvent):
    type: Literal["improvement_applied"]
    target: NonEmptyStr
    before_sha256: NonEmptyStr
    after_sha256: NonEmptyStr
    receipt_sha256: NonEmptyStr


class ImprovementRolledBackEvent(_BaseImprovementEvent):
    type: Literal["improvement_rolled_back"]
    target: NonEmptyStr
    restored_sha256: NonEmptyStr
    reason: NonEmptyStr


class ImprovementSupersededEvent(_BaseImprovementEvent):
    type: Literal["improvement_superseded"]
    superseded_by: NonEmptyStr
    reason: NonEmptyStr


ImprovementEvent = Annotated[
    ImprovementProposedEvent
    | ImprovementEvidenceLinkedEvent
    | ImprovementAutoReviewApprovedEvent
    | ImprovementAutoReviewRecordedEvent
    | ImprovementReviewApprovedEvent
    | ImprovementReviewRejectedEvent
    | ImprovementReworkRequestedEvent
    | ImprovementEvalRequestedEvent
    | ImprovementEvalCompletedEvent
    | ImprovementExportedEvent
    | ImprovementAppliedEvent
    | ImprovementRolledBackEvent
    | ImprovementSupersededEvent,
    Field(discriminator="type"),
]

IMPROVEMENT_EVENT_ADAPTER: TypeAdapter[ImprovementEvent] = TypeAdapter(ImprovementEvent)


# ---------------------------------------------------------------------------
# Strict JSONL reader
# ---------------------------------------------------------------------------


def read_improvement_events(path: Path) -> list[ImprovementEvent]:
    """Read and strictly validate an Improvement events.jsonl file."""
    if not path.exists():
        return []

    events: list[ImprovementEvent] = []
    seen_event_ids: dict[str, str] = {}
    expected_seq = 1

    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            raise ImprovementLedgerIntegrityError(f"{path} line {line_no}: blank hole in ledger")

        try:
            data = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ImprovementLedgerIntegrityError(f"{path} line {line_no}: invalid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise ImprovementLedgerIntegrityError(f"{path} line {line_no}: event is not a JSON object")

        seq = data.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != expected_seq:
            raise ImprovementLedgerIntegrityError(
                f"{path} line {line_no}: expected seq {expected_seq}, got {seq!r}"
            )

        try:
            event = IMPROVEMENT_EVENT_ADAPTER.validate_python(data)
        except ValidationError as exc:
            raise ImprovementLedgerIntegrityError(f"{path} line {line_no}: invalid event: {exc}") from exc

        prior = seen_event_ids.get(event.event_id)
        if prior is not None and prior != raw_line:
            raise ImprovementLedgerIntegrityError(
                f"{path} line {line_no}: duplicate event_id {event.event_id!r} with different bytes"
            )
        if prior is not None:
            raise ImprovementLedgerIntegrityError(
                f"{path} line {line_no}: duplicate event_id {event.event_id!r}"
            )

        seen_event_ids[event.event_id] = raw_line
        events.append(event)
        expected_seq += 1

    return events
