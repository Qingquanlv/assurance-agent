"""Evidence-only bootstrap identity for leftover host/activity tests."""

from __future__ import annotations

import graph_engine.evidence.ledger as leftover_ledger
from graph_engine.evidence.events import InvocationStarted
from graph_engine.evidence.seed import EMPTY_RUNTIME_AUTHORIZATION_DIGEST, empty_invocation_seed

_EMPTY_SEED = empty_invocation_seed()


def synthetic_invocation_started(
    *,
    invocation_id: str = "inv-1",
    lock_digest: str = "a" * 64,
    entrypoint: str = "main",
    runtime_authorization_digest: str = EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
    root_input_digest: str | None = None,
) -> InvocationStarted:
    """Build ``InvocationStarted`` with canonical empty-seed v2 identity fields."""

    return InvocationStarted(
        invocation_id=invocation_id,
        lock_digest=lock_digest,
        entrypoint=entrypoint,
        event_schema_version="2",
        runtime_authorization_digest=runtime_authorization_digest,
        root_input_digest=_EMPTY_SEED.root_input_digest if root_input_digest is None else root_input_digest,
    )


__all__ = [
    "empty_invocation_seed",
    "leftover_ledger",
    "synthetic_invocation_started",
]
