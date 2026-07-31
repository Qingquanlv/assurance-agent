"""Canonical issue identity helpers below the workflow layer.

Pure digest/ID construction only: no filesystem or workflow imports.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from assurance_agent.artifacts.models.issue_events import (
    ChangeIssueEvent,
    IssueAnalysisCompletedEvent,
    IssueAnalysisFailedEvent,
    ObservationRecordedEvent,
    OccurrenceDetectedEvent,
    OccurrenceLinkedEvent,
    ProblemAssessmentConfirmedEvent,
    ProblemDetectedEvent,
    ProblemEvent,
    ProblemMarkedNotAnIssueEvent,
    ProblemMergedEvent,
    ProblemMergeSuggestedEvent,
    ProblemOccurrenceLinkedEvent,
    ProblemRegressedEvent,
    ProblemReopenedEvent,
    ProblemResolvedEvent,
    ProblemRiskAcceptedEvent,
    ProblemVerificationRequestedEvent,
    ProblemWorkStartedEvent,
    ProjectSyncPendingEvent,
)
from assurance_agent.artifacts.models.issues import (
    AffectedSurface,
    FingerprintInputs,
    IssueCandidate,
    IssueCandidateDocument,
    IssueSeverity,
    ProblemFingerprint,
    ProblemFingerprintPreimage,
)

DIGEST_PREFIX_LENGTH = 16

_TOKEN_SPLIT = re.compile(r"[\s\-./]+")


@dataclass(frozen=True, slots=True)
class ObservationIdentityInput:
    change_id: str
    batch_id: str
    kind: str
    target: str
    case_id: str | None
    source_artifact: str
    source_json_pointer: str
    signature: str
    schema_version: str = "1"


def _canonical_sha256(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_prefix(full_hex: str) -> str:
    return full_hex[:DIGEST_PREFIX_LENGTH]


def _format_sha256_digest(full_hex: str) -> str:
    return f"sha256:{full_hex}"


def event_id(idempotency_key: str) -> str:
    return "EVT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def candidate_document_digest(
    candidates: IssueCandidateDocument | Mapping[str, object],
) -> str:
    """Digest the authored JSON document after key-order/format normalization only."""
    payload = (
        candidates.model_dump(mode="json")
        if isinstance(candidates, IssueCandidateDocument)
        else dict(candidates)
    )
    canonical = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )
    return _format_sha256_digest(hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def per_candidate_digest(candidate: IssueCandidate | Mapping[str, object]) -> str:
    payload = candidate.model_dump(mode="json") if isinstance(candidate, IssueCandidate) else dict(candidate)
    canonical = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )
    return _format_sha256_digest(hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _normalize_tokens(value: str, *, field: str) -> str:
    tokens = [token for token in _TOKEN_SPLIT.split(value.strip().lower()) if token]
    if not tokens:
        raise ValueError(f"{field} must not be empty after normalization")
    return "_".join(tokens)


def _normalize_symptom(value: str) -> str:
    return _normalize_tokens(value, field="symptom")


def _normalize_module(value: str) -> str:
    return _normalize_tokens(value, field="surface")


def _normalize_endpoint(value: str) -> str:
    parts = value.strip().split(None, 1)
    if len(parts) != 2:
        raise ValueError("endpoint surface must include method and path")
    method, path = parts
    normalized_method = method.strip().upper()
    normalized_path = re.sub(r"\s+", "", path.strip()).rstrip("/")
    if not normalized_method or not normalized_path:
        raise ValueError("surface must not be empty after normalization")
    return f"{normalized_method} {normalized_path}"


def _normalize_surface_identity(kind: str, value: str) -> str:
    if kind == "endpoint":
        return _normalize_endpoint(value)
    if kind == "module":
        return _normalize_module(value)
    return _normalize_tokens(value, field="surface")


def _normalize_qualifiers(qualifiers: list[str] | None) -> list[str]:
    if not qualifiers:
        return []
    return sorted(_normalize_symptom(item) for item in qualifiers)


def _fingerprint_canonical_object(
    *,
    affected_surface: AffectedSurface,
    fingerprint_inputs: FingerprintInputs,
    version: str,
) -> dict[str, object]:
    return {
        "version": version,
        "surface_kind": affected_surface.kind,
        "surface_identity": _normalize_surface_identity(
            affected_surface.kind,
            affected_surface.value,
        ),
        "symptom": _normalize_symptom(fingerprint_inputs.symptom),
        "qualifiers": _normalize_qualifiers(fingerprint_inputs.qualifiers),
    }


def observation_id(observation_input: ObservationIdentityInput) -> str:
    canonical = {
        "schema_version": observation_input.schema_version,
        "change_id": observation_input.change_id,
        "batch_id": observation_input.batch_id,
        "kind": observation_input.kind,
        "target": observation_input.target,
        "case_id": observation_input.case_id,
        "source": {
            "artifact": observation_input.source_artifact,
            "json_pointer": observation_input.source_json_pointer,
        },
        "signature": _normalize_symptom(observation_input.signature),
    }
    return f"OBS-{_digest_prefix(_canonical_sha256(canonical))}"


def occurrence_id(change_id: str, batch_id: str, candidate_digest: str) -> str:
    canonical = {
        "change_id": change_id,
        "batch_id": batch_id,
        "candidate_digest": candidate_digest,
    }
    return f"OCC-{_digest_prefix(_canonical_sha256(canonical))}"


def reconciliation_idempotency_key(
    change_id: str,
    batch_id: str,
    candidate_digest: str,
) -> str:
    return _canonical_sha256(
        {
            "change_id": change_id,
            "batch_id": batch_id,
            "candidate_digest": candidate_digest,
        }
    )


def problem_fingerprint(
    *,
    affected_surface: AffectedSurface,
    fingerprint_inputs: FingerprintInputs,
    version: Literal["1"] = "1",
    title: str | None = None,
    root_cause_hypothesis: str | None = None,
    confidence: float | None = None,
    severity: IssueSeverity | None = None,
    change_id: str | None = None,
) -> ProblemFingerprint:
    del title, root_cause_hypothesis, confidence, severity, change_id
    canonical = _fingerprint_canonical_object(
        affected_surface=affected_surface,
        fingerprint_inputs=fingerprint_inputs,
        version=version,
    )
    preimage = ProblemFingerprintPreimage.model_validate(canonical)
    return ProblemFingerprint(
        version=version,
        digest=_format_sha256_digest(_canonical_sha256(canonical)),
        preimage=preimage,
    )


def problem_id(fingerprint: ProblemFingerprint) -> str:
    hex_digest = fingerprint.digest.removeprefix("sha256:")
    return f"PROB-{_digest_prefix(hex_digest)}"


def fingerprint_digest_for_version(
    *,
    affected_surface: AffectedSurface,
    fingerprint_inputs: FingerprintInputs,
    version: str,
) -> str:
    """Return the full SHA-256 hex digest for arbitrary fingerprint versions."""
    canonical = _fingerprint_canonical_object(
        affected_surface=affected_surface,
        fingerprint_inputs=fingerprint_inputs,
        version=version,
    )
    return _canonical_sha256(canonical)


def recomputable_issue_event_idempotency_key(
    event: ChangeIssueEvent | ProblemEvent,
) -> str | None:
    """Reconstruct the writer idempotency key when the full preimage is persisted.

    Returns ``None`` for legacy / incomplete payloads (for example merge suggestions
    that omit the per-candidate digest, or problem events that never persisted it).
    """
    if isinstance(event, ObservationRecordedEvent):
        return f"observation_recorded:{event.change_id}:{event.batch_id}:{event.observation.observation_id}"
    if isinstance(event, IssueAnalysisCompletedEvent):
        digest = event.analysis_status.candidate_digest
        if digest is None:
            return None
        return f"issue_analysis_completed:{event.change_id}:{event.batch_id}:{digest}"
    if isinstance(event, IssueAnalysisFailedEvent):
        return (
            f"issue_analysis_failed:{event.change_id}:{event.batch_id}:"
            f"{event.analysis_status.evidence_bundle_digest}"
        )
    if isinstance(event, OccurrenceDetectedEvent):
        return (
            f"occurrence_detected:{event.change_id}:{event.batch_id}:"
            f"{event.occurrence.analysis.candidate_digest}"
        )
    if isinstance(event, OccurrenceLinkedEvent):
        return (
            f"occurrence_linked:{event.change_id}:{event.batch_id}:"
            f"{event.occurrence.analysis.candidate_digest}"
        )
    if isinstance(event, ProjectSyncPendingEvent):
        return f"project_sync_pending:{event.change_id}:{event.batch_id}:{event.candidate_digest}"
    if isinstance(event, ProblemDetectedEvent):
        # Writer key includes per-candidate digest, which is not on this envelope.
        return None
    if isinstance(event, ProblemOccurrenceLinkedEvent):
        return None
    if isinstance(event, ProblemRegressedEvent):
        return None
    if isinstance(event, ProblemResolvedEvent):
        return f"problem_resolved:{event.problem_id}:{event.batch_id}:{event.evidence_digest}"
    if isinstance(event, ProblemAssessmentConfirmedEvent):
        return (
            f"review:confirm_assessment:{event.problem_id}:"
            f"{event.expected_problem_version}:{event.evidence_digest}"
        )
    if isinstance(event, ProblemMarkedNotAnIssueEvent):
        return (
            f"review:mark_not_an_issue:{event.problem_id}:"
            f"{event.expected_problem_version}:{event.evidence_digest}"
        )
    if isinstance(event, ProblemRiskAcceptedEvent):
        return (
            f"review:accept_risk:{event.problem_id}:{event.expected_problem_version}:{event.evidence_digest}"
        )
    if isinstance(event, ProblemWorkStartedEvent):
        return (
            f"review:start_work:{event.problem_id}:{event.expected_problem_version}:{event.evidence_digest}"
        )
    if isinstance(event, ProblemReopenedEvent):
        return f"review:reopen:{event.problem_id}:{event.expected_problem_version}:{event.evidence_digest}"
    if isinstance(event, ProblemMergedEvent):
        return (
            f"review:merge:{event.problem_id}:{event.expected_problem_version}:"
            f"{event.target_problem_id}:{event.evidence_digest}"
        )
    if isinstance(event, ProblemVerificationRequestedEvent):
        return (
            f"review:submit_resolution:{event.problem_id}:{event.expected_problem_version}:"
            f"{event.change_id}:{event.batch_id}:{event.evidence_digest}"
        )
    if isinstance(event, ProblemMergeSuggestedEvent):
        return None
    return None
