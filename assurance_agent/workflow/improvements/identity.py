"""Deterministic Improvement fingerprint and ID helpers.

Fingerprints hash versioned kind, delivery, normalized target, and normalized
proposed-change intent. Retro metadata, rationale, confidence, risk, verification,
and source-ref order are intentionally omitted.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

from assurance_agent.artifacts.models.improvements import ImprovementCandidate


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def improvement_fingerprint(candidate: ImprovementCandidate, version: str = "1") -> str:
    payload = {
        "version": version,
        "kind": candidate.kind.value,
        "delivery": candidate.delivery.value,
        "target": _normalize(candidate.target),
        "intent": _normalize(candidate.proposed_change),
    }
    wire = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def improvement_id_for_fingerprint(fingerprint: str) -> str:
    return f"IMP-{fingerprint[:20].upper()}"


def improvement_event_id(idempotency_key: str, event_type: str, ordinal: int) -> str:
    raw = f"{idempotency_key}\x1f{event_type}\x1f{ordinal}".encode("utf-8")
    return f"IMPEVT-{hashlib.sha256(raw).hexdigest()[:24].upper()}"
