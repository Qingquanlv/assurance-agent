from __future__ import annotations
from dataclasses import replace
from graph_engine.attempts.checkpoint import AttemptPhase
from graph_engine.canonical import canonical_digest

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from graph_engine.attempts.activity import checkpoint_backed_activity_factory
from graph_engine.attempts.host_protocol import (
    AttemptRootDescriptor,
    TaskActivityRpcIdentity,
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostExecuteCall,
    current_bound_identity,
)
from graph_engine.attempts.host_receipts import TerminalReceiptStore
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.production_host import (
    UnsupportedProductionPlatform,
    _ProcessSupervisor,
    create_production_task_execution_host,
    invocation_activity_receipts_root,
)
from graph_engine.attempts.secret_sources import empty_runtime_authorization
from graph_engine.attempts.workspace import TaskWorkspaceStore
from graph_engine.persistence.attempt_checkpoint import MemoryAttemptCheckpointStore
from graph_engine.plugin_api import (
    InvocationMetadata,
    ResourceClaims,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)


class _OpenCodeLikeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request
        assert context.activity is not None
        context.activity.mark_dispatch_started({"provider": "opencode", "endpoint": "https://example.test"})
        context.activity.bind({"session_id": "ses_p9"})
        (context.write_root / "done.txt").write_text("ok\n", encoding="utf-8")
        return TaskOutcome.succeeded({"ok": True})


@dataclass
class ProductionHostFixture:
    attempt_checkpoints: MemoryAttemptCheckpointStore
    attempt_key: AttemptKey
    legacy_ledger_path: Path
    host: object
    call: TaskHostExecuteCall

    async def execute_one_opencode_call(self) -> TaskHostCallResult:
        return await self.host.execute(self.call)  # type: ignore[no-any-return]


@pytest.fixture
def production_host_fixture(tmp_path: Path) -> ProductionHostFixture:
    from tests.attempt_checkpoints import checkpoint

    project_root = tmp_path / "project"
    project_root.mkdir()
    store = TaskWorkspaceStore(project_root, tmp_path / "attempts", tmp_path / "promotion-receipts")
    workspace = store.begin(task_id="task-1", attempt=1, output_paths=("done.txt",))
    attempt_key = AttemptKey(digest="a" * 64)
    journal = MemoryAttemptCheckpointStore()
    authorization_id = "b" * 64
    graph_revision = "c" * 64
    asyncio.run(
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
        )
    )
    request = TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="run",
        capability_id="runtime.opencode.execute",
        target_capability_id="runtime.opencode.execute",
        binding_data={},
        resource_ids=(),
        resource_digests={},
        resources=ResourceClaims(),
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest="f" * 64,
            composition_digest=canonical_digest({"lock_digest": "f" * 64}),
            entrypoint="main",
        ),
        attempt=1,
        input={},
    )
    bound = current_bound_identity(
        attempt_key_digest=attempt_key.digest,
        authorization_id=authorization_id,
        workspace_identity_digest=workspace.identity.identity_digest,
        request_digest=canonical_digest(request.model_dump(mode="json")),
        graph_revision=graph_revision,
        product_lock_digest=request.invocation.lock_digest,
        handler_id="runtime.opencode.execute",
    )
    call = TaskHostExecuteCall(
        identity=TaskHostCallIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id="activity-1",
            operation="execute",
            **bound,  # type: ignore[arg-type]
        ),
        capability_id="runtime.opencode.execute",
        capability_entrypoint="tests.runtime.test_production_host:_OpenCodeLikeHandler.execute",
        request=request,
        attempt_root=AttemptRootDescriptor(
            workspace_identity=workspace.identity,
            project_root_identity=workspace.project_root_identity,
            write_root_identity=workspace.write_root_identity,
            project_root_digest=workspace.identity.project_digest,
            write_root_digest=workspace.identity.write_root_digest,
            baseline_digest=canonical_digest(
                [item.model_dump(mode="json") for item in workspace.identity.baseline_files]
            ),
        ),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id="activity-1",
            **bound,  # type: ignore[arg-type]
        ),
        authorized_secret_handles=(),
    )
    loop = asyncio.new_event_loop()

    async def _assert_live_fence() -> None:
        return None

    receipts_root = invocation_activity_receipts_root(tmp_path, "inv-1")
    receipts_root.parent.mkdir(parents=True, exist_ok=True)
    receipts = TerminalReceiptStore.create(receipts_root)
    host = create_production_task_execution_host(
        authorization=empty_runtime_authorization(),
        handlers={"runtime.opencode.execute": _OpenCodeLikeHandler()},
        store=store,
        receipts=receipts,
        activity_factory=checkpoint_backed_activity_factory(
            checkpoints=journal,
            attempt_key=attempt_key,
            owner_loop=loop,
            assert_live_fence=_assert_live_fence,
        ),
        invocation_root=tmp_path,
    )
    thread = __import__("threading").Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return ProductionHostFixture(
        attempt_checkpoints=journal,
        attempt_key=attempt_key,
        legacy_ledger_path=tmp_path / "invocations" / "inv-1" / "ledger",
        host=host,
        call=call,
    )


async def test_host_activity_rpc_uses_attempt_checkpoints(
    production_host_fixture: ProductionHostFixture,
) -> None:
    result = await production_host_fixture.execute_one_opencode_call()
    snapshot = await production_host_fixture.attempt_checkpoints.load(production_host_fixture.attempt_key)
    assert result.outcome is not None
    assert snapshot is not None
    assert snapshot.activity_state == "bound"
    assert not production_host_fixture.legacy_ledger_path.exists()
    found = production_host_fixture.host.read_terminal_receipts(production_host_fixture.call.identity)
    assert found
    started = datetime.fromisoformat(found[0].started_at)
    completed = datetime.fromisoformat(found[0].completed_at)
    assert started.tzinfo is not None
    assert completed >= started


def test_host_persist_phase_delta_installs_receipt(
    production_host_fixture: ProductionHostFixture,
) -> None:
    from graph_engine.attempts.context import AttemptExecutionContext, AuthorizedAttemptScope
    from graph_engine.canonical import canonical_digest
    from graph_engine.plugin_api import TaskOutcome

    host = production_host_fixture.host
    workspace = host._bound.store.begin(  # type: ignore[attr-defined]
        task_id="task-phase",
        attempt=1,
        output_paths=("qa/runtime.txt",),
    )
    (workspace.write_root / "qa").mkdir(parents=True, exist_ok=True)
    (workspace.write_root / "qa" / "runtime.txt").write_text("delta\n", encoding="utf-8")
    scope = AuthorizedAttemptScope(
        execution=AttemptExecutionContext(
            invocation_id="inv-1",
            public_entrypoint="main",
            semantic_node_id="run",
            attempt_key=production_host_fixture.attempt_key,
            fencing_token=1,
            authorization_id="b" * 64,
        ),
        workspace=workspace,
    )
    persist = getattr(host, "persist_phase_delta")
    ref = persist(
        scope=scope,
        phase="runtime",
        task_id="e" * 64,
        handler_id="runtime.opencode.execute",
        staged_paths=("qa/runtime.txt",),
        outcome=TaskOutcome.succeeded({"ok": True}),
        graph_revision="c" * 64,
        product_lock_digest="f" * 64,
    )
    identity = production_host_fixture.call.identity.model_copy(
        update={
            "task_id": "e" * 64,
            "activation_id": "run",
            "activity_id": "e" * 64,
            "authorization_id": "b" * 64,
            "phase": "runtime",
            "graph_revision": "c" * 64,
            "product_lock_digest": "f" * 64,
            "handler_id": "runtime.opencode.execute",
            "workspace_identity_digest": workspace.identity.identity_digest,
            "request_digest": canonical_digest(
                {
                    "phase": "runtime",
                    "task_id": "e" * 64,
                    "staged_paths": ["qa/runtime.txt"],
                }
            ),
        }
    )
    found = host.read_terminal_receipts(identity)
    assert found
    assert found[0].staged_write_set_digest == canonical_digest({"paths": ["qa/runtime.txt"]})
    assert ref.receipt_digest == canonical_digest(found[0].model_dump(mode="json"))
    started = datetime.fromisoformat(found[0].started_at)
    completed = datetime.fromisoformat(found[0].completed_at)
    assert started.tzinfo is not None
    assert completed.tzinfo is not None
    assert completed >= started


def test_production_host_rejects_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(UnsupportedProductionPlatform, match="Linux and macOS"):
        _ProcessSupervisor.for_platform()


@pytest.mark.parametrize("has_authority", [True, False])
def test_runtime_envelope_is_durable_before_dispatch(
    production_host_fixture, tmp_path, has_authority
) -> None:
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
    from assurance_product.retained_host import RetainedHost
    from assurance_product.worker_lifecycle import (
        acquire_execution,
        control_root,
        update_owner,
        ExecutionConflict,
    )
    import json

    fixture = production_host_fixture
    project = tmp_path / "project"
    workspace = ChangeWorkspace.prepare(Path(project).resolve(), "run")
    authority = {"fixture": "authorized-stop-only-runtime"}
    authority_digest = canonical_digest(authority)
    dispatched = False

    async def scenario():
        nonlocal dispatched
        async with open_sqlite_checkpointer(workspace) as backend:

            class CrashingHost:
                async def execute(self, call):
                    nonlocal dispatched
                    dispatched = True
                    cursor = await backend._conn.execute(
                        "SELECT payload, stop_authority_digest FROM assurance_host_calls"
                    )
                    row = await cursor.fetchone()
                    assert row is not None
                    assert json.loads(bytes(row[0])) == call.model_dump(mode="json")
                    assert row[1] == authority_digest
                    record = json.loads(
                        (control_root(workspace.paths.project_root) / "owner.json").read_text()
                    )
                    assert record["stop_authority"] == authority
                    assert record["calls"]
                    raise RuntimeError("dispatch crash")

            host = RetainedHost(CrashingHost(), backend, fixture.attempt_checkpoints)
            if has_authority:
                with pytest.raises(RuntimeError, match="dispatch crash"):
                    await host.execute(fixture.call)
            else:
                with pytest.raises(ExecutionConflict, match="requires retained cancellation authority"):
                    await host.execute(fixture.call)
                cursor = await backend._conn.execute("SELECT call_digest FROM assurance_host_calls")
                assert await cursor.fetchall() == []

    with acquire_execution(workspace.paths.project_root, "inv-1") as owner:
        if has_authority:
            # Unit fixture supplies the prerequisite that production runtime
            # ports construct from authenticated installed sources.
            update_owner(owner, lambda record: record.update(stop_authority=authority))
            owner.stop_authority_digest = authority_digest
        asyncio.run(scenario())
    assert dispatched is has_authority
    calls = json.loads((control_root(workspace.paths.project_root) / "owner.json").read_text())["calls"]
    assert bool(calls) is has_authority


@pytest.mark.parametrize("status, expected", [("acknowledged", False), ("terminal", True)])
def test_stop_bridge_rebinds_envelope_and_requires_terminal_cancel(
    production_host_fixture, tmp_path, monkeypatch, status, expected
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
    from assurance_product.sqlite_resource_authorization import SqliteResourceAuthorizationStore
    from assurance_product.retained_host import confirm_owned_calls
    from assurance_product.bootstrap.status import write_run_manifest
    from graph_engine.attempts.resource_arbiter import ResourceArbiter
    from graph_engine.plugin_api import TaskActivityCancelResult
    from graph_engine.canonical import canonical_json_bytes

    fixture = production_host_fixture
    workspace = ChangeWorkspace.prepare((tmp_path / "project").resolve(), "inv-1")
    run_dir = tmp_path / "run"
    write_run_manifest(
        run_dir,
        {
            "project_dir": str(workspace.paths.project_root),
            "invocation_id": "inv-1",
            "product": "test",
            "binding_dist": "test",
            "binding_declaration": "test",
            "config_tree": "test",
        },
    )
    monkeypatch.setattr("assurance_product.cli._resolve_and_audit", lambda **kwargs: (object(), None))
    monkeypatch.setattr(
        "assurance_product.cli._authorize_secrets", lambda *args: empty_runtime_authorization()
    )
    monkeypatch.setattr(
        "assurance_product.bootstrap.spec.load_run_spec",
        lambda path: SimpleNamespace(opencode_token_env="TOKEN"),
    )

    async def scenario():
        async with open_sqlite_checkpointer(workspace) as backend:
            previous = await backend.lease.acquire("inv-1", owner_id="old")
            await backend.lease.release(previous)
            arbiter = ResourceArbiter(SqliteResourceAuthorizationStore(backend))
            grant = await arbiter.acquire(fixture.attempt_key, ResourceClaims(), fencing_token=1)
            snap = await fixture.attempt_checkpoints.load(fixture.attempt_key)
            fixture.attempt_checkpoints = MemoryAttemptCheckpointStore()
            await fixture.attempt_checkpoints.commit(
                replace(snap, revision=0, authorization_id=grant.authorization_id),
                expected_revision=0,
                fencing_token=1,
            )
            call = fixture.call.model_copy(
                update={
                    "identity": fixture.call.identity.model_copy(
                        update={"authorization_id": grant.authorization_id}
                    ),
                    "activity_rpc": fixture.call.activity_rpc.model_copy(
                        update={"authorization_id": grant.authorization_id}
                    ),
                }
            )
            digest = canonical_digest(call.model_dump(mode="json"))
            await backend._conn.execute(
                "INSERT INTO assurance_host_calls (call_digest, owner_nonce, attempt_key_digest, payload) VALUES (?, ?, ?, ?)",
                (
                    digest,
                    "nonce",
                    fixture.attempt_key.digest,
                    canonical_json_bytes(call.model_dump(mode="json")),
                ),
            )
            await backend._conn.commit()

            class StopHost:
                def read_terminal_receipts(self, identity):
                    assert identity == call.identity
                    return ()

                async def cancel(self, cancellation):
                    assert cancellation.request == call.request
                    assert cancellation.identity.fencing_token == 2
                    assert cancellation.activity_rpc.fencing_token == 2
                    assert cancellation.identity.operation == "cancel"
                    result = TaskActivityCancelResult(
                        status=status,
                        outcome=TaskOutcome.stopped("cancelled") if status == "terminal" else None,
                    )
                    return TaskHostCallResult(operation="cancel", cancel_result=result)

            fake = SimpleNamespace(
                backend=backend,
                attempt_checkpoints=fixture.attempt_checkpoints,
                host=StopHost(),
                revision_id=call.identity.graph_revision,
                product_lock_digest=call.identity.product_lock_digest,
            )

            @asynccontextmanager
            async def ports(*args, **kwargs):
                yield fake

            monkeypatch.setattr("assurance_product.runtime_ports.ProductRuntimePorts.open", ports)
            owner = {
                "workspace": str(workspace.paths.project_root),
                "invocation": "inv-1",
                "nonce": "nonce",
                "run_dir": str(run_dir),
                "calls": [digest],
            }
            assert await confirm_owned_calls(owner) is expected
            cursor = await backend._conn.execute("SELECT confirmed FROM assurance_host_calls")
            assert bool((await cursor.fetchone())[0]) is expected

    asyncio.run(scenario())
