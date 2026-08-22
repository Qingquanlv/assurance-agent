"""Deterministic improvement identity and effect-key formulas."""

from __future__ import annotations

import hashlib
import json
import unicodedata

from assurance_improvement.contracts.effects import ImprovementEffectIntentV1
from assurance_improvement.contracts.improvements import ImprovementCandidate


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


def delivery_effect_key(intent: ImprovementEffectIntentV1) -> str:
    if intent.version is None or intent.target_kind is None or intent.target_digest is None:
        raise ValueError("delivery intent requires version, target_kind, and target_digest")
    return f"{intent.improvement_id}:{intent.version}:{intent.target_kind}:{intent.target_digest}"


def promotion_effect_key(intent: ImprovementEffectIntentV1) -> str:
    if intent.version is None or intent.promotion_digest is None:
        raise ValueError("promotion intent requires version and promotion_digest")
    return f"{intent.improvement_id}:{intent.version}:{intent.promotion_digest}"


def archive_effect_key(intent: ImprovementEffectIntentV1) -> str:
    if intent.invocation_id is None or intent.archive_digest is None:
        raise ValueError("archive intent requires invocation_id and archive_digest")
    return f"{intent.invocation_id}:{intent.archive_digest}"
