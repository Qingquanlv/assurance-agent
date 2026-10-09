from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from graph_engine.attempts.execution_host.host_protocol import TaskActivityRpcIdentity, current_bound_identity
from graph_engine.attempts.models.keys import AttemptKey
from graph_engine.attempts.orchestration.checkpoint import AttemptCheckpoint, AttemptPhase
from graph_engine.attempts.resources.activity import (
    CheckpointBackedTaskActivityPort,
    MAX_ACTIVITY_VALUE_BYTES,
    TaskActivityConflict,
    TaskActivityReferenceInvalid,
)
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.persistence.attempt_checkpoint import (
    AttemptCheckpointIntegrityError,
    MemoryAttemptCheckpointStore,
)
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.plugin_api import TaskActivityPort, TaskOutcome, TaskWorkspaceIdentity
from tests.attempt_checkpoints import checkpoint


_LOCK = "a" * 64
_FINGERPRINT = {"endpoint": "https://localhost", "profile": "v1"}
_REFERENCE = {"session_id": "ses_1"}


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


def _start_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop


def _checkpoint_port(
    *,
    identity_overrides: dict[str, object] | None = None,
) -> tuple[CheckpointBackedTaskActivityPort, MemoryAttemptCheckpointStore, AttemptKey]:
    journal = MemoryAttemptCheckpointStore()
    attempt_key = AttemptKey(digest="a" * 64)
    authorization_id = "b" * 64
    graph_revision = "c" * 64
    future = asyncio.run_coroutine_threadsafe(
        journal.commit(
            replace(
                checkpoint(
                    attempt_key,
                    fencing_token=1,
                    contract_digest="d" * 64,
                    input_digest="e" * 64,
                    graph_revision=graph_revision,
                    invocation_id="inv-1",
                    public_entrypoint="main",
                    semantic_node_id="run",
                ),
                fencing_token=1,
                authorization_id=authorization_id,
                phase=AttemptPhase.RECONCILE,
                activity_id="activity-1",
                activity_state="prepared",
            ),
            expected_revision=0,
            fencing_token=1,
        ),
        _OWNER_LOOP,
    )
    future.result(timeout=5)
    workspace = _identity()

    async def _assert_live_fence() -> None:
        return None

    live = current_bound_identity(
        attempt_key_digest=attempt_key.digest,
        authorization_id=authorization_id,
        workspace_identity_digest=workspace.identity_digest,
        request_digest="0" * 64,
        graph_revision=graph_revision,
        product_lock_digest=_LOCK,
        handler_id="test.echo.run",
    )
    fields = dict(live)
    if identity_overrides:
        fields.update(identity_overrides)
    identity = TaskActivityRpcIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="a1",
        attempt=1,
        activity_id="activity-1",
        **fields,  # type: ignore[arg-type]
    )
    port = CheckpointBackedTaskActivityPort(
        checkpoints=journal,
        attempt_key=attempt_key,
        identity=identity,
        workspace_identity=workspace,
        assert_live_fence=_assert_live_fence,
        owner_loop=_OWNER_LOOP,
        remaining_deadline=5.0,
        expected_request_digest=str(live["request_digest"]),
        expected_product_lock_digest=str(live["product_lock_digest"]),
        expected_handler_id=str(live["handler_id"]),
    )
    return port, journal, attempt_key


_OWNER_LOOP = _start_loop()


def test_checkpoint_backed_port_commits_dispatch_and_bind() -> None:
    port, journal, attempt_key = _checkpoint_port()
    first = port.mark_dispatch_started(_FINGERPRINT)
    assert first.state == "dispatch_started"
    second = port.bind(_REFERENCE)
    assert second.state == "bound"
    snapshot = asyncio.run_coroutine_threadsafe(journal.load(attempt_key), _OWNER_LOOP).result(timeout=5)
    assert snapshot is not None
    assert snapshot.activity_state == "bound"


def test_checkpoint_backed_port_rejects_fingerprint_drift() -> None:
    port, _, _ = _checkpoint_port()
    port.mark_dispatch_started(_FINGERPRINT)
    with pytest.raises(TaskActivityConflict):
        port.mark_dispatch_started({"endpoint": "https://example.invalid", "profile": "v1"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt_key_digest", "0" * 64),
        ("authorization_id", "1" * 64),
        ("fencing_token", 99),
        ("phase", "prepare"),
        ("workspace_identity_digest", "2" * 64),
        ("request_digest", "3" * 64),
        ("graph_revision", "4" * 64),
        ("product_lock_digest", "5" * 64),
        ("handler_id", "runtime.other.execute"),
        ("host_implementation_digest", "6" * 64),
        ("host_implementation_id", "graph.engine.other-host"),
    ],
)
def test_checkpoint_backed_port_rejects_mismatched_bound_fields(field: str, value: object) -> None:
    port, _, _ = _checkpoint_port(identity_overrides={field: value})
    with pytest.raises((TaskActivityConflict, StaleFencingToken)):
        port.mark_dispatch_started(_FINGERPRINT)


def test_checkpoint_backed_port_rejects_stale_fence() -> None:
    journal = MemoryAttemptCheckpointStore()
    attempt_key = AttemptKey(digest="a" * 64)
    asyncio.run_coroutine_threadsafe(
        journal.commit(
            replace(
                checkpoint(
                    attempt_key,
                    fencing_token=2,
                    contract_digest="d" * 64,
                    input_digest="e" * 64,
                    graph_revision="c" * 64,
                    invocation_id="inv-1",
                    public_entrypoint="main",
                    semantic_node_id="run",
                ),
                fencing_token=2,
                authorization_id="b" * 64,
                phase=AttemptPhase.RECONCILE,
                activity_id="activity-1",
                activity_state="prepared",
            ),
            expected_revision=0,
            fencing_token=2,
        ),
        _OWNER_LOOP,
    ).result(timeout=5)
    workspace = _identity()

    async def _assert_live_fence() -> None:
        raise StaleFencingToken("fencing token is stale")

    port = CheckpointBackedTaskActivityPort(
        checkpoints=journal,
        attempt_key=attempt_key,
        identity=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="a1",
            attempt=1,
            activity_id="activity-1",
            **current_bound_identity(  # type: ignore[arg-type]
                attempt_key_digest=attempt_key.digest,
                authorization_id="b" * 64,
                workspace_identity_digest=workspace.identity_digest,
                request_digest="0" * 64,
                graph_revision="c" * 64,
                product_lock_digest=_LOCK,
                handler_id="test.echo.run",
                fencing_token=1,
            ),
        ),
        workspace_identity=workspace,
        assert_live_fence=_assert_live_fence,
        owner_loop=_OWNER_LOOP,
        remaining_deadline=5.0,
    )
    with pytest.raises(StaleFencingToken):
        port.mark_dispatch_started(_FINGERPRINT)


def _load_checkpoint(store: MemoryAttemptCheckpointStore, key: AttemptKey) -> AttemptCheckpoint:
    snapshot = asyncio.run_coroutine_threadsafe(store.load(key), _OWNER_LOOP).result(timeout=5)
    assert snapshot is not None
    return snapshot


def test_dispatch_repeat_preserves_the_checkpoint_revision() -> None:
    port, store, key = _checkpoint_port()
    first = port.mark_dispatch_started({"profile": "v1", "endpoint": "https://localhost"})
    before = _load_checkpoint(store, key)
    assert port.mark_dispatch_started(_FINGERPRINT) == first
    assert _load_checkpoint(store, key) == before


def test_bind_repeat_preserves_the_checkpoint_revision() -> None:
    port, store, key = _checkpoint_port()
    port.mark_dispatch_started(_FINGERPRINT)
    first = port.bind(_REFERENCE)
    before = _load_checkpoint(store, key)
    assert port.bind(_REFERENCE) == first
    assert _load_checkpoint(store, key) == before


def test_changed_reference_is_invalid() -> None:
    port, store, key = _checkpoint_port()
    port.mark_dispatch_started(_FINGERPRINT)
    port.bind(_REFERENCE)
    before = _load_checkpoint(store, key)
    with pytest.raises(TaskActivityReferenceInvalid, match="changed after bind"):
        port.bind({"session_id": "ses_2"})
    assert _load_checkpoint(store, key) == before


@pytest.mark.parametrize("operation", ["dispatch", "bind"])
@pytest.mark.parametrize(
    "value", [None, {"value": "x" * (MAX_ACTIVITY_VALUE_BYTES + 1)}, {"value": float("nan")}]
)
def test_invalid_activity_values_do_not_commit(operation: str, value: JSONValue) -> None:
    port, store, key = _checkpoint_port()
    if operation == "bind":
        port.mark_dispatch_started(_FINGERPRINT)
    mutate = port.bind if operation == "bind" else port.mark_dispatch_started
    before = _load_checkpoint(store, key)
    with pytest.raises(TaskActivityReferenceInvalid):
        mutate(value)
    assert _load_checkpoint(store, key) == before


def test_bind_before_dispatch_conflicts() -> None:
    port, store, key = _checkpoint_port()
    before = _load_checkpoint(store, key)
    with pytest.raises(TaskActivityConflict, match="cannot apply"):
        port.bind(_REFERENCE)
    assert _load_checkpoint(store, key) == before


def test_stale_port_cannot_bind_after_attempt_adoption() -> None:
    port, store, key = _checkpoint_port()
    port.mark_dispatch_started(_FINGERPRINT)
    snapshot = _load_checkpoint(store, key)
    asyncio.run_coroutine_threadsafe(
        store.commit(
            replace(snapshot, fencing_token=2), expected_revision=snapshot.revision, fencing_token=2
        ),
        _OWNER_LOOP,
    ).result(timeout=5)
    with pytest.raises(StaleFencingToken):
        port.bind(_REFERENCE)
    assert _load_checkpoint(store, key).activity_reference is None


def test_port_cannot_bind_after_terminal_observation() -> None:
    port, store, key = _checkpoint_port()
    port.mark_dispatch_started(_FINGERPRINT)
    snapshot = _load_checkpoint(store, key)
    outcome = TaskOutcome.stopped("cancelled").model_dump(mode="json")
    asyncio.run_coroutine_threadsafe(
        store.commit(
            replace(
                snapshot,
                phase=AttemptPhase.COMMIT,
                activity_state="terminal_observed",
                activity_outcome=outcome,
                activity_outcome_digest=canonical_digest(outcome),
            ),
            expected_revision=snapshot.revision,
            fencing_token=1,
        ),
        _OWNER_LOOP,
    ).result(timeout=5)
    with pytest.raises(TaskActivityConflict, match="live attempt"):
        port.bind(_REFERENCE)
    assert _load_checkpoint(store, key).activity_reference is None


def test_port_is_not_an_arbitrary_writer() -> None:
    port, _, _ = _checkpoint_port()
    assert isinstance(port, TaskActivityPort)
    assert {name for name in dir(port) if not name.startswith("_")} == {
        "activity_id",
        "snapshot",
        "mark_dispatch_started",
        "bind",
    }
    with pytest.raises(AttributeError):
        port.expected_revision = 99  # type: ignore[attr-defined]


@pytest.mark.parametrize("operation", ["dispatch", "bind"])
@pytest.mark.parametrize("identical", [True, False])
def test_checkpoint_cas_conflict_authenticates_the_committed_value(
    monkeypatch: pytest.MonkeyPatch, operation: str, identical: bool
) -> None:
    port, store, key = _checkpoint_port()
    if operation == "bind":
        port.mark_dispatch_started(_FINGERPRINT)
    before = _load_checkpoint(store, key)
    original_commit = store.commit
    durability_checks: list[AttemptKey] = []

    async def publish_then_conflict(
        candidate: AttemptCheckpoint, *, expected_revision: int, fencing_token: int
    ) -> AttemptCheckpoint:
        if not identical:
            competing = {"different": True}
            if operation == "dispatch":
                candidate = replace(
                    candidate,
                    activity_dispatch_fingerprint=competing,
                    activity_dispatch_fingerprint_digest=canonical_digest(competing),
                )
            else:
                candidate = replace(
                    candidate,
                    activity_reference=competing,
                    activity_reference_digest=canonical_digest(competing),
                )
        await original_commit(candidate, expected_revision=expected_revision, fencing_token=fencing_token)
        raise AttemptCheckpointIntegrityError("lost compare-and-swap race")

    async def ensure_durable(attempt_key: AttemptKey) -> None:
        durability_checks.append(attempt_key)

    monkeypatch.setattr(store, "commit", publish_then_conflict)
    monkeypatch.setattr(store, "ensure_durable", ensure_durable)
    mutate = port.bind if operation == "bind" else port.mark_dispatch_started
    value = _REFERENCE if operation == "bind" else _FINGERPRINT
    if identical:
        result = mutate(value)
        assert result.state == ("bound" if operation == "bind" else "dispatch_started")
        assert durability_checks == [key]
    else:
        error = TaskActivityReferenceInvalid if operation == "bind" else TaskActivityConflict
        with pytest.raises(error):
            mutate(value)
        assert durability_checks == []
    assert _load_checkpoint(store, key).revision == before.revision + 1
