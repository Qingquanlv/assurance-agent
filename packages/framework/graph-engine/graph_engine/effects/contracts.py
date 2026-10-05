from __future__ import annotations

from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import canonical_digest


GRAPH_NAMES_NOT_EFFECT_KINDS = {
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
}

IN_ATTEMPT_SETTLEMENT = "in_attempt"


def effect_idempotency_key(attempt_key: AttemptKey, effect_ordinal: int) -> str:
    if effect_ordinal < 1:
        raise ValueError("effect ordinal must be positive")
    return canonical_digest(
        {
            "attempt_key": attempt_key.digest,
            "effect_ordinal": effect_ordinal,
        }
    )


__all__ = [
    "GRAPH_NAMES_NOT_EFFECT_KINDS",
    "IN_ATTEMPT_SETTLEMENT",
    "effect_idempotency_key",
]
