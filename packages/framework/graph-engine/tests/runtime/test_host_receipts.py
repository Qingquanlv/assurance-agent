from __future__ import annotations

import os
from pathlib import Path

import pytest

from graph_engine.attempts.host_protocol import (
    TaskHostCallIdentity,
    TaskHostTerminalReceipt,
    current_bound_identity,
)
from graph_engine.attempts.host_receipts import (
    TerminalReceiptError,
    TerminalReceiptStore,
    _identity_filename,
    prove_call_quiescent,
)
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import StagedWriteSet, TaskActivitySnapshot, TaskOutcome, TaskWorkspaceIdentity


_FINGERPRINT = {"endpoint": "https://127.0.0.1:1", "profile": "test"}
_REFERENCE = {"id": "ext-1"}


def _digest(value: object) -> str:
    return canonical_digest(value)  # type: ignore[arg-type]


def _identity(
    *,
    operation: str = "execute",
    activity_id: str = "activity-1",
    attempt: int = 1,
    **overrides: object,
) -> TaskHostCallIdentity:
    workspace = _workspace_identity()
    bound = current_bound_identity(
        attempt_key_digest="a" * 64,
        authorization_id="b" * 64,
        workspace_identity_digest=workspace.identity_digest,
        request_digest="0" * 64,
        graph_revision="c" * 64,
        product_lock_digest="a" * 64,
        handler_id="test.echo.run",
    )
    bound.update(overrides)
    return TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="a1",
        attempt=attempt,
        activity_id=activity_id,
        operation=operation,  # type: ignore[arg-type]
        **bound,  # type: ignore[arg-type]
    )


def _outcome() -> TaskOutcome:
    return TaskOutcome.succeeded({"ok": True})


def _workspace_identity() -> TaskWorkspaceIdentity:
    payload = {
        "task_id": "task-1",
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": "b" * 64,
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(
        **payload,
        identity_digest=_digest(payload),
    )


def _staged(identity: TaskWorkspaceIdentity) -> StagedWriteSet:
    payload = {"identity_digest": identity.identity_digest, "files": []}
    return StagedWriteSet(
        identity_digest=identity.identity_digest,
        files=(),
        staged_digest=_digest(payload),
    )


def _receipt(
    identity: TaskHostCallIdentity,
    activity: TaskActivitySnapshot,
    outcome: TaskOutcome,
    quiescence: str,
    *,
    staged: StagedWriteSet | None = None,
    host_call_id: int,
) -> TaskHostTerminalReceipt:
    assert identity.activity_id is not None
    workspace_identity = activity.workspace_identity
    staged_write_set = staged or _staged(workspace_identity)
    return TaskHostTerminalReceipt(
        host_implementation_digest=identity.host_implementation_digest,
        wire_schema_version=identity.wire_schema_version,
        invocation_id=identity.invocation_id,
        task_id=identity.task_id,
        activation_id=identity.activation_id,
        attempt=identity.attempt,
        activity_id=identity.activity_id,
        operation=identity.operation,
        attempt_key_digest=identity.attempt_key_digest,
        authorization_id=identity.authorization_id,
        fencing_token=identity.fencing_token,
        phase=identity.phase,
        graph_revision=identity.graph_revision,
        product_lock_digest=identity.product_lock_digest,
        handler_id=identity.handler_id,
        request_digest=activity.request_digest,
        workspace_identity_digest=workspace_identity.identity_digest,
        project_root_digest=workspace_identity.project_digest,
        write_root_digest=workspace_identity.write_root_digest,
        baseline_digest=_digest([item.model_dump(mode="json") for item in workspace_identity.baseline_files]),
        staged_write_set_digest=staged_write_set.staged_digest,
        dispatch_fingerprint_digest=activity.dispatch_fingerprint_digest,
        reference_digest=activity.reference_digest,
        outcome=outcome,
        outcome_digest=_digest(outcome.model_dump(mode="json")),
        terminal_proof_digest=None,
        quiescence_proof_digest=quiescence,
        host_call_id=host_call_id,
    )


def _prepared_snapshot() -> TaskActivitySnapshot:
    return TaskActivitySnapshot(
        activity_id="activity-1",
        request_digest="0" * 64,
        workspace_identity=_workspace_identity(),
        state="bound",
        dispatch_fingerprint=_FINGERPRINT,
        dispatch_fingerprint_digest=_digest(_FINGERPRINT),
        reference=_REFERENCE,
        reference_digest=_digest(_REFERENCE),
    )


def test_terminal_receipt_sink_is_host_call_scoped(tmp_path: Path) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    sink = store.sink_for(_identity())
    public = {name for name in dir(sink) if not name.startswith("_")}
    assert public == {"install", "host_call_id"}
    assert not hasattr(sink, "read")
    assert not hasattr(sink, "delete")
    assert not hasattr(sink, "parent")
    assert not hasattr(sink, "root")
    assert not hasattr(sink, "path")
    assert sink.host_call_id == 1


def test_receipt_store_rejects_partial_symlink_linked_changed_multiple_foreign_and_nonmonotonic(
    tmp_path: Path,
) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    identity = _identity()
    activity = _prepared_snapshot()
    outcome = _outcome()
    quiescence = prove_call_quiescent()
    sink = store.sink_for(identity)
    sink.install(_receipt(identity, activity, outcome, quiescence, host_call_id=1))
    authenticated = store.authenticate(identity)
    assert len(authenticated) == 1

    foreign = _identity(activity_id="activity-2")
    with pytest.raises(TerminalReceiptError, match="foreign"):
        sink.install(_receipt(foreign, activity, outcome, quiescence, host_call_id=1))

    second = store.sink_for(identity)
    with pytest.raises(TerminalReceiptError, match="multiple"):
        second.install(_receipt(identity, activity, outcome, quiescence, host_call_id=2))

    names = os.listdir(store.root)
    final = next(name for name in names if not name.startswith("."))
    os.chmod(store.root / final, 0o600)
    (store.root / final).write_bytes(b'{"changed":true}')
    with pytest.raises(TerminalReceiptError, match="changed|invalid"):
        store.authenticate(identity)


def test_prepare_receipt_is_not_promotable(tmp_path: Path) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    pending = store.root / ".pending-prepare.json"
    pending.write_bytes(b'{"schema_version":"1","kind":"prepare"}')
    assert store.authenticate(_identity()) == ()


def test_quiescence_rejects_live_writers() -> None:
    with pytest.raises(TerminalReceiptError, match="quiescent"):
        prove_call_quiescent(writer_identities=("writer-1",))


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt_key_digest", "9" * 64),
        ("authorization_id", "8" * 64),
        ("fencing_token", 9),
        ("phase", "prepare"),
        ("workspace_identity_digest", "7" * 64),
        ("request_digest", "6" * 64),
        ("graph_revision", "5" * 64),
        ("product_lock_digest", "4" * 64),
        ("handler_id", "runtime.other.execute"),
        ("host_implementation_digest", "3" * 64),
    ],
)
def test_terminal_receipt_rejects_mismatched_identity_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    identity = _identity()
    activity = _prepared_snapshot()
    outcome = _outcome()
    sink = store.sink_for(identity)
    sink.install(_receipt(identity, activity, outcome, prove_call_quiescent(), host_call_id=1))
    mismatched = _identity(**{field: value})
    with pytest.raises(TerminalReceiptError, match="foreign|stale"):
        store.authenticate(mismatched)


def test_terminal_receipt_rejects_stale_fence(tmp_path: Path) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    identity = _identity(fencing_token=2)
    activity = _prepared_snapshot()
    sink = store.sink_for(identity)
    sink.install(_receipt(identity, activity, _outcome(), prove_call_quiescent(), host_call_id=1))
    with pytest.raises(TerminalReceiptError, match="foreign|stale"):
        store.authenticate(_identity(fencing_token=1))


def test_prior_receipt_schema_is_not_parsed(tmp_path: Path) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    identity = _identity()
    planted = store.root / _identity_filename(identity)
    planted.write_text('{"schema_version":"2","wire_schema_version":"1"}', encoding="utf-8")
    with pytest.raises(TerminalReceiptError, match="invalid"):
        store.authenticate(identity)
