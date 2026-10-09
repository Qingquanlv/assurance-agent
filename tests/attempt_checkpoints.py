"""Typed checkpoint fixtures shared by framework and product behavior tests."""

from __future__ import annotations
from typing import Any
from graph_engine.attempts.checkpoint import AttemptCheckpoint, AttemptPhase, AttemptResult
from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import canonical_digest


def checkpoint(attempt_key: AttemptKey | None = None, **changes: Any) -> AttemptCheckpoint:
    key = attempt_key or AttemptKey(digest=canonical_digest(changes))
    values: dict[str, Any] = dict(
        attempt_key=key,
        revision=0,
        fencing_token=4,
        phase=AttemptPhase.AUTHORIZE,
        contract_digest="b" * 64,
        input_digest="c" * 64,
        graph_revision="a" * 64,
        invocation_id="inv-1",
        public_entrypoint="execute",
        semantic_node_id="execution.run",
    )
    values.update(changes)
    return AttemptCheckpoint(**values)


def completed_checkpoint(
    attempt_key: AttemptKey | None = None, *, terminal: AttemptResult, **identity: Any
) -> AttemptCheckpoint:
    key = attempt_key or AttemptKey(digest=canonical_digest(identity))
    values: dict[str, Any] = dict(
        phase=AttemptPhase.DONE,
        revision=8,
        fencing_token=4,
        terminal_fencing_token=4,
        terminal_revision=7,
        authorization_id="e" * 64,
        terminal=terminal,
        released=True,
    )
    if terminal.resolution_kind == "committed":
        values.update(
            activity_id=key.digest,
            activity_state="terminal_observed",
            activity_outcome=terminal.output,
            activity_outcome_digest=canonical_digest(terminal.output),
            prepared_digest="d" * 64,
            promotion_receipt_id=terminal.receipt_id,
            promotion_receipt_digest=terminal.receipt_digest,
            promotion_staged_digest="f" * 64,
        )
    values.update(identity)
    return checkpoint(key, **values)
