"""Deterministic Observation, Occurrence, and Problem identity helpers.

Public IDs use ``OBS-``, ``OCC-``, and ``PROB-`` prefixes followed by the first
``DIGEST_PREFIX_LENGTH`` lowercase hex characters of a canonical SHA-256 digest.
Problem fingerprints store the full digest as ``sha256:<hex>``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

from assurance_agent.artifacts.models.issues import (
    AffectedSurface,
    FingerprintInputs,
    IssueSeverity,
    ProblemFingerprint,
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
    return ProblemFingerprint(
        version=version,
        digest=_format_sha256_digest(_canonical_sha256(canonical)),
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
