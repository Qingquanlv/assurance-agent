from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from graph_engine.attempts.activity import journal_backed_activity_factory
from graph_engine.attempts.events import ActivityPrepared, AttemptOpened, ResourcesAuthorized
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
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
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
    attempt_journal: MemoryAttemptJournal
    attempt_key: AttemptKey
    legacy_ledger_path: Path
    host: object
    call: TaskHostExecuteCall

    async def execute_one_opencode_call(self) -> TaskHostCallResult:
        return await self.host.execute(self.call)  # type: ignore[no-any-return]


@pytest.fixture
def production_host_fixture(tmp_path: Path) -> ProductionHostFixture:
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = TaskWorkspaceStore(project_root, tmp_path / "attempts", tmp_path / "promotion-receipts")
    workspace = store.begin(task_id="task-1", attempt=1, output_paths=("done.txt",))
    attempt_key = AttemptKey(digest="a" * 64)
    journal = MemoryAttemptJournal()
    authorization_id = "b" * 64
    graph_revision = "c" * 64
    asyncio.run(
        journal.append(
            attempt_key,
            (
                AttemptOpened(
                    contract_digest="d" * 64,
                    input_digest="e" * 64,
                    graph_revision=graph_revision,
                    invocation_id="inv-1",
                    public_entrypoint="main",
                    semantic_node_id="run",
                ),
                ResourcesAuthorized(authorization_id=authorization_id),
                ActivityPrepared(activity_id="activity-1"),
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
        activity_factory=journal_backed_activity_factory(
            journal=journal,
            attempt_key=attempt_key,
            owner_loop=loop,
            assert_live_fence=_assert_live_fence,
        ),
        invocation_root=tmp_path,
    )
    thread = __import__("threading").Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return ProductionHostFixture(
        attempt_journal=journal,
        attempt_key=attempt_key,
        legacy_ledger_path=tmp_path / "invocations" / "inv-1" / "ledger",
        host=host,
        call=call,
    )


async def test_host_activity_rpc_uses_attempt_journal(production_host_fixture: ProductionHostFixture) -> None:
    result = await production_host_fixture.execute_one_opencode_call()
    snapshot = await production_host_fixture.attempt_journal.load(production_host_fixture.attempt_key)
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
