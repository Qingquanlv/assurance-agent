"""Compatibility facade over ``assurance_agent.evidence.issue_identity``."""

from __future__ import annotations

from assurance_agent.evidence.issue_identity import (
    DIGEST_PREFIX_LENGTH,
    ObservationIdentityInput,
    candidate_document_digest,
    event_id,
    fingerprint_digest_for_version,
    occurrence_id,
    observation_id,
    per_candidate_digest,
    problem_fingerprint,
    problem_id,
    reconciliation_idempotency_key,
    recomputable_issue_event_idempotency_key,
)

__all__ = [
    "DIGEST_PREFIX_LENGTH",
    "ObservationIdentityInput",
    "candidate_document_digest",
    "event_id",
    "fingerprint_digest_for_version",
    "occurrence_id",
    "observation_id",
    "per_candidate_digest",
    "problem_fingerprint",
    "problem_id",
    "reconciliation_idempotency_key",
    "recomputable_issue_event_idempotency_key",
]
