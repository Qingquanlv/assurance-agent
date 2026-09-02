from __future__ import annotations

import hashlib
import json
from pathlib import Path

from graph_engine.composition import InvocationLock
from graph_engine.errors import GraphEngineError
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphCompleted,
    GraphStarted,
    InvocationFinished,
    InvocationStarted,
    NodeActivated,
    NodeInterrupted,
    TaskAttemptStarted,
    TaskAttemptStopped,
)
from graph_engine.runtime.ledger import Ledger, LedgerPublicationIndeterminate
from graph_engine.runtime.models import InvocationProjection, fold_events
from graph_engine.runtime.seed import EMPTY_RUNTIME_AUTHORIZATION_DIGEST, empty_invocation_seed


class LegacyEvidenceError(GraphEngineError):
    """Raised when leftover-v2 historical evidence cannot be authenticated."""


def authenticate_invocation_lock_v2(raw: bytes) -> InvocationLock:
    """Authenticate leftover InvocationLock v2 from stored canonical bytes."""
    if not raw:
        raise LegacyEvidenceError("invocation lock v2 is empty")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LegacyEvidenceError("invocation lock v2 is not JSON") from error
    if not isinstance(payload, dict):
        raise LegacyEvidenceError("invocation lock v2 is not an object")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        lock = InvocationLock.model_validate({**payload, "canonical_bytes": raw, "digest": digest})
    except Exception as error:
        raise LegacyEvidenceError("invocation lock v2 failed authentication") from error
    if lock.canonical_bytes != raw:
        raise LegacyEvidenceError("invocation lock v2 is not byte-exact")
    return lock


def read_legacy_ledger(root: Path) -> tuple[EventEnvelope, ...]:
    """Read leftover ledger batches without appending or reconciling."""
    if root.is_dir():
        try:
            names = [name for name in root.iterdir() if name.name.startswith(".pending-")]
        except OSError as error:
            raise LedgerPublicationIndeterminate(
                "leftover ledger publication outcome is indeterminate"
            ) from error
        if any(path.is_file() and not path.is_symlink() for path in names):
            raise LedgerPublicationIndeterminate("leftover ledger publication outcome is indeterminate")
    return Ledger(root).read_all()


def fold_legacy_events(envelopes: tuple[EventEnvelope, ...]) -> InvocationProjection:
    """Fold leftover events for historical export, archive, and lock show."""
    return fold_events(envelopes)


__all__ = [
    "EMPTY_RUNTIME_AUTHORIZATION_DIGEST",
    "EventEnvelope",
    "GraphCompleted",
    "GraphStarted",
    "InvocationFinished",
    "InvocationStarted",
    "Ledger",
    "LedgerPublicationIndeterminate",
    "LegacyEvidenceError",
    "NodeActivated",
    "NodeInterrupted",
    "TaskAttemptStarted",
    "TaskAttemptStopped",
    "authenticate_invocation_lock_v2",
    "empty_invocation_seed",
    "fold_legacy_events",
    "read_legacy_ledger",
]
