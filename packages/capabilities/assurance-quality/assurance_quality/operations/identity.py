"""Canonical issue identity — evidence only, never document formatting."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from assurance_quality.contracts.issues import (
    AffectedSurface,
    FingerprintInputs,
    IssueCandidate,
    IssueCandidateDocument,
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


def canonical_sha256(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_prefix(full_hex: str) -> str:
    return full_hex[:DIGEST_PREFIX_LENGTH]


def format_sha256_digest(full_hex: str) -> str:
    return f"sha256:{full_hex}"


def event_id(idempotency_key: str) -> str:
    return "EVT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def candidate_document_digest(
    candidates: IssueCandidateDocument | Mapping[str, object],
) -> str:
    payload = (
        candidates.model_dump(mode="json")
        if isinstance(candidates, IssueCandidateDocument)
        else dict(candidates)
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    return format_sha256_digest(hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def per_candidate_digest(candidate: IssueCandidate | Mapping[str, object]) -> str:
    payload = candidate.model_dump(mode="json") if isinstance(candidate, IssueCandidate) else dict(candidate)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    return format_sha256_digest(hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _normalize_tokens(value: str, *, field: str) -> str:
    tokens = [token for token in _TOKEN_SPLIT.split(value.strip().lower()) if token]
    if not tokens:
        raise ValueError(f"{field} must not be empty after normalization")
    return "_".join(tokens)


def normalize_symptom(value: str) -> str:
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


def normalize_surface_identity(kind: str, value: str) -> str:
    if kind == "endpoint":
        return _normalize_endpoint(value)
    if kind == "module":
        return _normalize_module(value)
    return _normalize_tokens(value, field="surface")


def normalize_qualifiers(qualifiers: list[str] | None) -> list[str]:
    if not qualifiers:
        return []
    return sorted(normalize_symptom(item) for item in qualifiers)


def fingerprint_canonical_object(
    *,
    affected_surface: AffectedSurface,
    fingerprint_inputs: FingerprintInputs,
    version: str,
) -> dict[str, object]:
    return {
        "version": version,
        "surface_kind": affected_surface.kind,
        "surface_identity": normalize_surface_identity(affected_surface.kind, affected_surface.value),
        "symptom": normalize_symptom(fingerprint_inputs.symptom),
        "qualifiers": normalize_qualifiers(fingerprint_inputs.qualifiers),
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
        "signature": normalize_symptom(observation_input.signature),
    }
    return f"OBS-{digest_prefix(canonical_sha256(canonical))}"


def occurrence_id(change_id: str, batch_id: str, candidate_digest: str) -> str:
    return f"OCC-{digest_prefix(canonical_sha256({'change_id': change_id, 'batch_id': batch_id, 'candidate_digest': candidate_digest}))}"


def problem_fingerprint(
    *,
    affected_surface: AffectedSurface,
    fingerprint_inputs: FingerprintInputs,
    version: Literal["1"] = "1",
) -> ProblemFingerprint:
    canonical = fingerprint_canonical_object(
        affected_surface=affected_surface,
        fingerprint_inputs=fingerprint_inputs,
        version=version,
    )
    preimage = ProblemFingerprintPreimage.model_validate(canonical)
    return ProblemFingerprint(
        version=version,
        digest=format_sha256_digest(canonical_sha256(canonical)),
        preimage=preimage,
    )


def problem_id(fingerprint: ProblemFingerprint) -> str:
    return f"PROB-{digest_prefix(fingerprint.digest.removeprefix('sha256:'))}"


def review_id(problem_id_value: str, expected_version: int) -> str:
    return "REV-" + digest_prefix(
        canonical_sha256({"problem_id": problem_id_value, "expected_problem_version": expected_version})
    )
