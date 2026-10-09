from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint

from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.sqlite_attempt_checkpoint import SqliteAttemptCheckpointStore
from assurance_product.sqlite_checkpointer import open_sqlite_checkpointer
from graph_engine.attempts.checkpoint import ActiveSystemInterrupt
from graph_engine.attempts.checkpoint_bridge import AttemptCheckpointObserver
from graph_engine.attempts.keys import AttemptKey
from graph_engine.persistence.journal import InvocationStarted
from graph_engine.persistence.runner_lease import StaleFencingToken
from graph_engine.stategraph.checkpoint_bridge import CHECKPOINT_MARKERS_STATE_KEY, CheckpointBridgeMarker
from tests.attempt_checkpoints import checkpoint


class CrashBeforeAttemptObserver:
    async def on_anchored(self, notice: object) -> None:
        raise RuntimeError("crash before Attempt observer")


@pytest.mark.parametrize("completion", [False, True], ids=["issuance", "completion"])
@pytest.mark.parametrize("stale_caller", [False, True], ids=["current-owner", "stale-owner"])
def test_historical_anchor_recovery_uses_current_call_authority(
    tmp_path: Path, completion: bool, stale_caller: bool
) -> None:
    asyncio.run(_recover(tmp_path, completion=completion, stale_caller=stale_caller))


async def _recover(tmp_path: Path, *, completion: bool, stale_caller: bool) -> None:
    workspace = ChangeWorkspace.prepare(tmp_path.resolve(), "CH-1")
    issued = ActiveSystemInterrupt(
        generation=1,
        ordinal=0,
        envelope_digest="b" * 64,
        issuance_checkpoint_id="cp-issued" if completion else None,
    )
    saved = checkpoint(
        AttemptKey(digest="a" * 64),
        active_interrupt=issued,
        active_interrupts=(issued,),
        fencing_token=1,
    )
    started = InvocationStarted(
        invocation_id="inv-1",
        thread_id="inv-1",
        graph_revision=saved.graph_revision,
        product_lock_digest="f" * 64,
        root_input_digest=saved.input_digest,
        fencing_token=1,
    )
    marker = CheckpointBridgeMarker(
        kind="system_interrupt_completed" if completion else "system_interrupt_issued",
        attempt_key=saved.attempt_key.digest,
        generation=issued.generation,
        ordinal=issued.ordinal,
        envelope_digest=issued.envelope_digest,
    )
    config: RunnableConfig = {"configurable": {"thread_id": "inv-1", "assurance_fencing_token": 1}}
    graph_checkpoint: Checkpoint = {
        "v": 2,
        "id": "cp-recover",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {CHECKPOINT_MARKERS_STATE_KEY: [marker.model_dump(mode="json")]},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": [],
    }
    async with open_sqlite_checkpointer(workspace) as backend:
        lease = await backend.lease.acquire("inv-1", owner_id="first")
        try:
            store = SqliteAttemptCheckpointStore(backend)
            saved = await store.commit(saved, expected_revision=0, fencing_token=lease.fencing_token)
            await backend.journal.start_invocation(started, fencing_token=lease.fencing_token)
            backend.install_observers((CrashBeforeAttemptObserver(),))
            backend.seal_observers()
            with pytest.raises(RuntimeError, match="crash before Attempt observer"):
                await backend.checkpointer(started.anchor_state()).aput(
                    config, graph_checkpoint, {"source": "loop", "step": 1, "parents": {}}, {}
                )
            original_anchor = await backend.journal.read_checkpoint_anchor("inv-1", "cp-recover")
            assert original_anchor is not None
            incomplete = await backend.store.scan_incomplete_outbox("inv-1")
            assert len(incomplete) == 1
            assert incomplete[0].journal_anchored_at is not None
            assert incomplete[0].observers_delivered_at is None
        finally:
            await backend.lease.release(lease)

    async with open_sqlite_checkpointer(workspace) as backend:
        lease = await backend.lease.acquire("inv-1", owner_id="restarted")
        try:
            assert lease.fencing_token > original_anchor.fencing_token
            store = SqliteAttemptCheckpointStore(backend)
            backend.install_observers((AttemptCheckpointObserver(store),))
            backend.seal_observers()
            identity = replace(
                started.anchor_state(),
                fencing_token=original_anchor.fencing_token if stale_caller else lease.fencing_token,
            )
            saver = backend.checkpointer(identity)
            if stale_caller:
                with pytest.raises(StaleFencingToken):
                    await saver.arecover(thread_id="inv-1")
                assert await store.load(saved.attempt_key) == saved
                assert len(await backend.store.scan_incomplete_outbox("inv-1")) == 1
            else:
                await saver.arecover(thread_id="inv-1")
                recovered = await store.load(saved.attempt_key)
                assert recovered is not None
                assert recovered.fencing_token == lease.fencing_token
                retained = recovered.active_interrupts[0]
                if completion:
                    assert retained.issuance_checkpoint_id == "cp-issued"
                    assert retained.completion_checkpoint_id == "cp-recover"
                    assert retained.retired
                    assert recovered.active_interrupt is None
                else:
                    assert retained.issuance_checkpoint_id == "cp-recover"
                    assert not retained.retired
                    assert recovered.active_interrupt == retained
                assert await backend.store.scan_incomplete_outbox("inv-1") == ()
                await saver.arecover(thread_id="inv-1")
                assert await store.load(saved.attempt_key) == recovered
            assert await backend.journal.read_checkpoint_anchor("inv-1", "cp-recover") == original_anchor
        finally:
            await backend.lease.release(lease)
