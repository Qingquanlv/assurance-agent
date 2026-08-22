"""Shared schema-v2 bootstrap identity for synthetic runtime test fixtures."""

from __future__ import annotations

from graph_engine.runtime.events import InvocationStarted
from graph_engine.runtime.seed import EMPTY_RUNTIME_AUTHORIZATION_DIGEST, empty_invocation_seed

_EMPTY_SEED = empty_invocation_seed()
# Legacy fold-only histories seed projection head to this tree before the first commit.
SYNTHETIC_INITIAL_TREE_ID = "0" * 64
# Some scheduler fold fixtures still advance from this explicit baseline.
SYNTHETIC_BASELINE_TREE_ID = "b" * 64


def synthetic_invocation_started(
    *,
    invocation_id: str = "inv-1",
    lock_digest: str = "a" * 64,
    entrypoint: str = "main",
    runtime_authorization_digest: str = EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
    root_input_digest: str | None = None,
    initial_tree_id: str | None = None,
) -> InvocationStarted:
    """Build ``InvocationStarted`` with canonical empty-seed v2 identity fields."""

    return InvocationStarted(
        invocation_id=invocation_id,
        lock_digest=lock_digest,
        entrypoint=entrypoint,
        event_schema_version="2",
        runtime_authorization_digest=runtime_authorization_digest,
        root_input_digest=_EMPTY_SEED.root_input_digest if root_input_digest is None else root_input_digest,
        initial_tree_id=SYNTHETIC_INITIAL_TREE_ID if initial_tree_id is None else initial_tree_id,
    )
