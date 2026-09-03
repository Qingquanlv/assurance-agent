from __future__ import annotations

from bootstrap_fixtures import synthetic_invocation_started
from pathlib import Path

import pytest

from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import TaskActivityPort, TaskOutcome, TaskWorkspaceIdentity
from graph_engine.attempts.activity import (
    LedgerTaskActivityPort,
    MAX_ACTIVITY_VALUE_BYTES,
    TaskActivityConflict,
    TaskActivityIndeterminate,
    TaskActivityReferenceInvalid,
)
from graph_engine.attempts.activity import (
    GraphStarted,
    NodeActivated,
    TaskActivityPrepared,
    TaskActivityTerminalObserved,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskLeaseAcquired,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.attempts.host_protocol import TaskActivityRpcIdentity
from graph_engine.attempts.activity import Ledger, LedgerConflictError
from graph_engine.attempts.activity import fold_events


_LOCK = "a" * 64
_FINGERPRINT = {"endpoint": "https://localhost", "profile": "v1"}
_REFERENCE = {"session_id": "ses_1"}
_ACTIVE_LEDGER: Ledger | None = None


def _identity(*, attempt: int = 1) -> TaskWorkspaceIdentity:
    payload = {
        "task_id": "task-1",
        "attempt": attempt,
        "attempt_id": f"attempt-{attempt}",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": canonical_digest({"attempt": attempt, "kind": "write-root"}),
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(**payload, identity_digest=canonical_digest(payload))


def _rpc_identity(*, activity_id: str = "activity-1", attempt: int = 1) -> TaskActivityRpcIdentity:
    return TaskActivityRpcIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="a1",
        attempt=attempt,
        activity_id=activity_id,
    )


def _prepared_events() -> tuple[object, ...]:
    return (
        synthetic_invocation_started(lock_digest=_LOCK),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        TokenOffered(
            token_id="tok-1",
            graph_instance_id="root",
            source=None,
            target="task",
            payload=None,
        ),
        TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="task"),
        NodeActivated(
            activation_id="a1",
            graph_instance_id="root",
            node_id="task",
            token_ids=("tok-1",),
        ),
        TaskAttemptStarted(activation_id="a1", attempt=1, lease_expires_at="11"),
        TaskLeaseAcquired(
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            owner_id="worker-1",
            acquired_at=1.0,
            heartbeat_at=1.0,
            expires_at=11.0,
        ),
        TaskActivityPrepared(
            activity_id="activity-1",
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            request_digest="0" * 64,
            workspace_identity=_identity(),
        ),
    )


def _prepared_port(tmp_path: Path) -> tuple[LedgerTaskActivityPort, Ledger]:
    global _ACTIVE_LEDGER
    ledger = Ledger(tmp_path / "ledger")
    events = _prepared_events()
    ledger.append_batch(events, expected_next_seq=1)  # type: ignore[arg-type]
    port = LedgerTaskActivityPort(ledger=ledger, identity=_rpc_identity())
    _ACTIVE_LEDGER = ledger
    return port, ledger


def _advance_attempt_elsewhere(activity_id: str) -> None:
    assert _ACTIVE_LEDGER is not None
    ledger = _ACTIVE_LEDGER
    envelopes = ledger.read_all()
    projection = fold_events(envelopes)
    activity = next(
        attempt.activity
        for activation in projection.activations
        for attempt in activation.attempts
        if attempt.activity is not None and attempt.activity.activity_id == activity_id
    )
    assert activity is not None
    outcome = TaskOutcome.failed("transient", "attempt superseded")
    next_attempt = 2
    proof = "e" * 64 if activity.state == "dispatch_started" else None
    terminal = TaskActivityTerminalObserved(
        activity_id=activity_id,
        outcome=outcome,
        outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
        terminal_proof_digest=proof,
        staged_write_set_digest="d" * 64,
    )
    failure = outcome.failure
    assert failure is not None
    ledger.append_batch(
        (
            terminal,
            TaskAttemptFailed(
                activation_id="a1",
                attempt=1,
                failure=failure,
                staged_write_set_digest="d" * 64,
            ),
            TaskAttemptStarted(activation_id="a1", attempt=next_attempt, lease_expires_at="21"),
            TaskLeaseAcquired(
                task_id="task-1",
                activation_id="a1",
                attempt=next_attempt,
                owner_id="worker-2",
                acquired_at=2.0,
                heartbeat_at=2.0,
                expires_at=21.0,
            ),
            TaskActivityPrepared(
                activity_id="activity-2",
                task_id="task-1",
                activation_id="a1",
                attempt=next_attempt,
                request_digest="1" * 64,
                workspace_identity=_identity(attempt=2),
            ),
        ),
        expected_next_seq=envelopes[-1].seq + 1,
    )


def test_exact_dispatch_repeat_does_not_append(tmp_path: Path) -> None:
    port, ledger = _prepared_port(tmp_path)
    first = port.mark_dispatch_started({"endpoint": "https://localhost", "profile": "v1"})
    before = ledger.read_bytes()
    second = port.mark_dispatch_started({"endpoint": "https://localhost", "profile": "v1"})
    assert second == first
    assert ledger.read_bytes() == before
    assert first.state == "dispatch_started"


def test_canonical_fingerprint_repeat_does_not_append(tmp_path: Path) -> None:
    port, ledger = _prepared_port(tmp_path)
    first = port.mark_dispatch_started({"profile": "v1", "endpoint": "https://localhost"})
    before = ledger.read_bytes()
    second = port.mark_dispatch_started({"endpoint": "https://localhost", "profile": "v1"})
    assert second == first
    assert ledger.read_bytes() == before


def test_changed_fingerprint_is_conflict(tmp_path: Path) -> None:
    port, _ = _prepared_port(tmp_path)
    port.mark_dispatch_started(_FINGERPRINT)
    with pytest.raises(TaskActivityConflict):
        port.mark_dispatch_started({"endpoint": "https://example.invalid", "profile": "v1"})


def test_exact_bind_repeat_does_not_append(tmp_path: Path) -> None:
    port, ledger = _prepared_port(tmp_path)
    port.mark_dispatch_started(_FINGERPRINT)
    first = port.bind({"session_id": "ses_1"})
    before = ledger.read_bytes()
    second = port.bind({"session_id": "ses_1"})
    assert second == first
    assert ledger.read_bytes() == before
    assert first.state == "bound"


def test_changed_reference_is_invalid(tmp_path: Path) -> None:
    port, _ = _prepared_port(tmp_path)
    port.mark_dispatch_started(_FINGERPRINT)
    port.bind(_REFERENCE)
    with pytest.raises(TaskActivityReferenceInvalid):
        port.bind({"session_id": "ses_2"})


def test_oversized_fingerprint_is_rejected(tmp_path: Path) -> None:
    port, ledger = _prepared_port(tmp_path)
    before = ledger.read_bytes()
    with pytest.raises(TaskActivityReferenceInvalid):
        port.mark_dispatch_started({"endpoint": "x" * (MAX_ACTIVITY_VALUE_BYTES + 1)})
    assert ledger.read_bytes() == before


def test_oversized_reference_is_rejected(tmp_path: Path) -> None:
    port, ledger = _prepared_port(tmp_path)
    port.mark_dispatch_started(_FINGERPRINT)
    before = ledger.read_bytes()
    with pytest.raises(TaskActivityReferenceInvalid):
        port.bind({"session_id": "x" * (MAX_ACTIVITY_VALUE_BYTES + 1)})
    assert ledger.read_bytes() == before


def test_null_fingerprint_is_rejected(tmp_path: Path) -> None:
    port, _ = _prepared_port(tmp_path)
    with pytest.raises(TaskActivityReferenceInvalid):
        port.mark_dispatch_started(None)


def test_bind_before_dispatch_conflicts(tmp_path: Path) -> None:
    port, ledger = _prepared_port(tmp_path)
    before = ledger.read_bytes()
    with pytest.raises(TaskActivityConflict):
        port.bind(_REFERENCE)
    assert ledger.read_bytes() == before


def test_stale_port_cannot_bind_another_attempt(tmp_path: Path) -> None:
    port, _ = _prepared_port(tmp_path)
    port.mark_dispatch_started(_FINGERPRINT)
    _advance_attempt_elsewhere(port.activity_id)
    with pytest.raises(TaskActivityConflict):
        port.bind({"session_id": "ses_1"})


def test_port_is_not_an_arbitrary_writer(tmp_path: Path) -> None:
    port, _ = _prepared_port(tmp_path)
    public = {name for name in dir(port) if not name.startswith("_")}
    assert public.isdisjoint(
        {
            "append",
            "ledger",
            "store",
            "root",
            "path",
            "select_attempt",
            "for_attempt",
            "observe_terminal",
            "mark_terminal",
            "publish_terminal",
            "expected_next_seq",
        }
    )
    assert not hasattr(port, "append")
    assert getattr(port, "ledger", None) is None
    assert isinstance(port, TaskActivityPort)
    with pytest.raises(AttributeError):
        port.expected_next_seq = 99  # type: ignore[attr-defined]


def test_cas_conflict_with_identical_event_is_idempotent(
    tmp_path: Path,
) -> None:
    port, ledger = _prepared_port(tmp_path)
    original = ledger.append_batch

    def publish_then_conflict(events: object, expected_next_seq: int) -> object:
        published = original(events, expected_next_seq)  # type: ignore[arg-type]
        raise LedgerConflictError("lost race after publish")
        return published

    ledger.append_batch = publish_then_conflict  # type: ignore[method-assign]
    snapshot = port.mark_dispatch_started(_FINGERPRINT)
    assert snapshot.state == "dispatch_started"
    kinds = [item.event.kind for item in Ledger(ledger.root).read_all()]
    assert kinds.count("task_activity_dispatch_started") == 1


def test_ambiguous_publication_authenticates_exact_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, ledger = _prepared_port(tmp_path)

    def fail_after_install(name: str) -> None:
        if name == "final_installed":
            raise OSError("append result unavailable")

    monkeypatch.setattr("graph_engine.runtime.ledger._append_boundary", fail_after_install)
    snapshot = port.mark_dispatch_started(_FINGERPRINT)
    assert snapshot.state == "dispatch_started"
    kinds = [item.event.kind for item in Ledger(ledger.root).read_all()]
    assert kinds.count("task_activity_dispatch_started") == 1


def test_unreadable_publication_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, ledger = _prepared_port(tmp_path)
    original_read = ledger.read_all
    failed = False

    def fail_after_install(name: str) -> None:
        nonlocal failed
        if name == "final_installed":
            failed = True
            raise OSError("append result unavailable")

    def unreadable() -> object:
        if failed:
            raise OSError("ledger unreadable")
        return original_read()

    monkeypatch.setattr("graph_engine.runtime.ledger._append_boundary", fail_after_install)
    ledger.read_all = unreadable  # type: ignore[method-assign]
    with pytest.raises(TaskActivityIndeterminate):
        port.mark_dispatch_started(_FINGERPRINT)
