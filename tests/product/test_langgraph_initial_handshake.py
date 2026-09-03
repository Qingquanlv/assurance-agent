from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableConfig

from assurance_product import open_sqlite_checkpointer
from assurance_product.change_workspace import ChangeWorkspace
from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer
from graph_engine.persistence.checkpoint_store import CheckpointOutboxDraft
from graph_engine.persistence.journal import (
    CheckpointIntegrityError,
    InvocationStarted,
    strict_checkpoint_serializer,
)


REVISION = "a" * 64
LOCK = "b" * 64
INPUT = "c" * 64
FENCE = 1


def _workspace(tmp_path: Path) -> ChangeWorkspace:
    project = (tmp_path / "project").resolve()
    (project / "qa" / "changes" / "CH-1").mkdir(parents=True)
    return ChangeWorkspace.prepare(project, "CH-1")


def _started(
    *,
    fencing_token: int = FENCE,
    product_lock_digest: str = LOCK,
    root_input_digest: str = INPUT,
    graph_revision: str = REVISION,
) -> InvocationStarted:
    return InvocationStarted(
        invocation_id="inv-1",
        thread_id="inv-1",
        graph_revision=graph_revision,
        product_lock_digest=product_lock_digest,
        root_input_digest=root_input_digest,
        fencing_token=fencing_token,
    )


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "checkpoint_ns": "",
            "checkpoint_id": "cp-initial",
            "assurance_revision_id": REVISION,
            "assurance_product_lock_digest": LOCK,
            "assurance_root_input_digest": INPUT,
            "assurance_fencing_token": FENCE,
        }
    }


def _checkpoint() -> dict[str, object]:
    return {
        "v": 2,
        "id": "cp-initial",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"change_id": "chg-1"},
        "channel_versions": {"change_id": 1},
        "versions_seen": {},
        "updated_channels": ["change_id"],
    }


def _draft(
    *,
    product_lock_digest: str = LOCK,
    root_input_digest: str = INPUT,
    graph_revision: str = REVISION,
    metadata_bytes: bytes,
    checkpoint_type: str,
    metadata_type: str,
) -> CheckpointOutboxDraft:
    return CheckpointOutboxDraft(
        invocation_id="inv-1",
        thread_id="inv-1",
        checkpoint_id="cp-initial",
        parent_checkpoint_id=None,
        kind="checkpoint",
        task_identity="",
        graph_revision=graph_revision,
        product_lock_digest=product_lock_digest,
        root_input_digest=root_input_digest,
        fencing_token=FENCE,
        metadata_bytes=metadata_bytes,
        checkpoint_type=checkpoint_type,
        metadata_type=metadata_type,
    )


async def _put_initial_checkpoint(
    store: object,
    *,
    product_lock_digest: str = LOCK,
    root_input_digest: str = INPUT,
    graph_revision: str = REVISION,
) -> tuple[bytes, str]:
    serde = strict_checkpoint_serializer()
    checkpoint_type, checkpoint_bytes = serde.dumps_typed(_checkpoint())
    metadata_type, metadata_bytes = serde.dumps_typed({"source": "update", "step": -1, "parents": {}})
    stored_config, outbox_id = await store.put_checkpoint(  # type: ignore[union-attr]
        _config(),
        checkpoint_bytes,
        _draft(
            product_lock_digest=product_lock_digest,
            root_input_digest=root_input_digest,
            graph_revision=graph_revision,
            metadata_bytes=metadata_bytes,
            checkpoint_type=checkpoint_type,
            metadata_type=metadata_type,
        ),
    )
    assert stored_config["configurable"]["checkpoint_id"] == "cp-initial"
    return checkpoint_bytes, outbox_id


def test_reopen_completes_matching_pair_after_checkpoint_outbox(tmp_path: Path) -> None:
    asyncio.run(_reopen_completes_matching_pair_after_checkpoint_outbox(tmp_path))


async def _reopen_completes_matching_pair_after_checkpoint_outbox(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as first:
        await _put_initial_checkpoint(first.store)
        await first.remember_entrypoint("inv-1", "execute")

    async with open_sqlite_checkpointer(workspace) as second:
        recovered = await second.recover_handshake("inv-1")
        assert recovered is not None
        assert recovered.thread_id == recovered.invocation_id == "inv-1"
        assert recovered.product_lock_digest == LOCK
        assert recovered.root_input_digest == INPUT
        assert recovered.graph_revision == REVISION
        assert await second.read_entrypoint("inv-1") == "execute"
        second.seal_observers()
        saver = second.checkpointer(recovered.anchor_state())
        await saver.arecover(thread_id="inv-1")
        loaded = await saver.aget_tuple(_config())
        assert loaded is not None
        assert loaded.checkpoint["id"] == "cp-initial"
        configurable = loaded.config.get("configurable") or {}
        assert configurable["thread_id"] == "inv-1"


def test_reopen_fails_closed_after_invocation_started_without_checkpoint(tmp_path: Path) -> None:
    asyncio.run(_reopen_fails_closed_after_invocation_started_without_checkpoint(tmp_path))


async def _reopen_fails_closed_after_invocation_started_without_checkpoint(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as first:
        await first.journal.start_invocation(_started(), fencing_token=FENCE)
        await first.remember_entrypoint("inv-1", "execute")

    async with open_sqlite_checkpointer(workspace) as second:
        recovered = await second.recover_handshake("inv-1")
        assert recovered is not None
        assert recovered == _started()
        assert await second.read_entrypoint("inv-1") == "execute"
        second.seal_observers()
        saver = second.checkpointer(recovered.anchor_state())
        await saver.arecover(thread_id="inv-1")
        assert await saver.aget_tuple(_config()) is None


def test_reopen_never_exposes_resumable_checkpoint_with_divergent_identity(
    tmp_path: Path,
) -> None:
    asyncio.run(_reopen_never_exposes_resumable_checkpoint_with_divergent_identity(tmp_path))


async def _reopen_never_exposes_resumable_checkpoint_with_divergent_identity(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as first:
        await first.journal.start_invocation(_started(), fencing_token=FENCE)
        await _put_initial_checkpoint(
            first.store,
            product_lock_digest="e" * 64,
            root_input_digest="f" * 64,
            graph_revision="d" * 64,
        )

    async with open_sqlite_checkpointer(workspace) as second:
        with pytest.raises(CheckpointIntegrityError):
            await second.recover_handshake("inv-1")
        started = await second.journal.read_invocation_started("inv-1")
        assert started == _started()
        saver = AnchoredCheckpointer(
            store=second.store,
            journal=second.journal,
            identity=_started().anchor_state(),
            observers=(),
        )
        assert await saver.aget_tuple(_config()) is None


def test_remember_entrypoint_same_value_is_noop_different_value_fails_closed(tmp_path: Path) -> None:
    asyncio.run(_remember_entrypoint_same_value_is_noop_different_value_fails_closed(tmp_path))


async def _remember_entrypoint_same_value_is_noop_different_value_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as backend:
        await backend.remember_entrypoint("inv-1", "execute")
        await backend.remember_entrypoint("inv-1", "execute")
        assert await backend.read_entrypoint("inv-1") == "execute"
        with pytest.raises(CheckpointIntegrityError):
            await backend.remember_entrypoint("inv-1", "other")
        assert await backend.read_entrypoint("inv-1") == "execute"


def test_start_invocation_refuses_second_start_that_rewrites_identity(tmp_path: Path) -> None:
    asyncio.run(_start_invocation_refuses_second_start_that_rewrites_identity(tmp_path))


async def _start_invocation_refuses_second_start_that_rewrites_identity(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as backend:
        await backend.journal.start_invocation(_started(), fencing_token=FENCE)
        await backend.journal.start_invocation(_started(), fencing_token=FENCE)
        assert await backend.journal.read_invocation_started("inv-1") == _started()
        with pytest.raises(CheckpointIntegrityError):
            await backend.journal.start_invocation(
                _started(product_lock_digest="e" * 64),
                fencing_token=FENCE,
            )
        with pytest.raises(CheckpointIntegrityError):
            await backend.journal.start_invocation(
                _started(root_input_digest="f" * 64),
                fencing_token=FENCE,
            )
        with pytest.raises(CheckpointIntegrityError):
            await backend.journal.start_invocation(
                _started(graph_revision="d" * 64),
                fencing_token=FENCE,
            )
        assert await backend.journal.read_invocation_started("inv-1") == _started()


def test_lease_acquire_before_arecover_does_not_abandon_initial_handshake(tmp_path: Path) -> None:
    asyncio.run(_lease_acquire_before_arecover_does_not_abandon_initial_handshake(tmp_path))


async def _lease_acquire_before_arecover_does_not_abandon_initial_handshake(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as first:
        first_lease = await first.lease.acquire("inv-1", owner_id="runner-a")
        await _put_initial_checkpoint(first.store)
        await first.remember_entrypoint("inv-1", "execute")
        await first.lease.release(first_lease)

    async with open_sqlite_checkpointer(workspace) as second:
        recovered = await second.recover_handshake("inv-1")
        assert recovered is not None
        lease = await second.lease.acquire("inv-1", owner_id="runner-b")
        second.seal_observers()
        saver = second.checkpointer(recovered.anchor_state())
        await saver.arecover(thread_id="inv-1")
        loaded = await saver.aget_tuple(_config())
        assert loaded is not None
        assert loaded.checkpoint["id"] == "cp-initial"
        live = [
            record
            for record in await second.store.list_outbox("inv-1")
            if record.kind == "checkpoint" and record.checkpoint_id == "cp-initial"
        ]
        assert len(live) == 1
        assert live[0].abandoned_at is None
        assert await second.journal.read_checkpoint_anchor("inv-1", "cp-initial") is not None
        await second.lease.release(lease)


def test_held_lease_then_recover_does_not_abandon_unanchored_initial_outbox(tmp_path: Path) -> None:
    asyncio.run(_held_lease_then_recover_does_not_abandon_unanchored_initial_outbox(tmp_path))


async def _held_lease_then_recover_does_not_abandon_unanchored_initial_outbox(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as first:
        first_lease = await first.lease.acquire("inv-1", owner_id="runner-a")
        await first.journal.start_invocation(_started(), fencing_token=FENCE)
        await _put_initial_checkpoint(first.store)
        await first.remember_entrypoint("inv-1", "execute")
        await first.lease.release(first_lease)

    async with open_sqlite_checkpointer(workspace) as second:
        lease = await second.lease.acquire("inv-1", owner_id="runner-b")
        recovered = await second.recover_handshake("inv-1")
        assert recovered is not None
        second.seal_observers()
        saver = second.checkpointer(recovered.anchor_state())
        await saver.arecover(thread_id="inv-1")
        loaded = await saver.aget_tuple(_config())
        assert loaded is not None
        assert loaded.checkpoint["id"] == "cp-initial"
        live = [
            record
            for record in await second.store.list_outbox("inv-1")
            if record.kind == "checkpoint" and record.checkpoint_id == "cp-initial"
        ]
        assert len(live) == 1
        assert live[0].abandoned_at is None
        await second.lease.release(lease)


def test_recover_handshake_refuses_after_newer_fence_instead_of_abandoning(tmp_path: Path) -> None:
    asyncio.run(_recover_handshake_refuses_after_newer_fence_instead_of_abandoning(tmp_path))


async def _recover_handshake_refuses_after_newer_fence_instead_of_abandoning(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as first:
        await first.journal.start_invocation(_started(), fencing_token=FENCE)
        await _put_initial_checkpoint(first.store)

    async with open_sqlite_checkpointer(workspace) as second:
        await second.journal.advance_fence("inv-1", 2)
        with pytest.raises(CheckpointIntegrityError):
            await second.recover_handshake("inv-1")
        live = [
            record
            for record in await second.store.list_outbox("inv-1")
            if record.kind == "checkpoint" and record.checkpoint_id == "cp-initial"
        ]
        assert len(live) == 1
        assert live[0].abandoned_at is None
        assert await second.journal.read_checkpoint_anchor("inv-1", "cp-initial") is None


def test_lease_acquire_after_fail_closed_recover_retries_integrity_error(tmp_path: Path) -> None:
    asyncio.run(_lease_acquire_after_fail_closed_recover_retries_integrity_error(tmp_path))


async def _lease_acquire_after_fail_closed_recover_retries_integrity_error(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as first:
        await first.journal.start_invocation(_started(), fencing_token=FENCE)
        await _put_initial_checkpoint(
            first.store,
            product_lock_digest="e" * 64,
            root_input_digest="f" * 64,
            graph_revision="d" * 64,
        )

    async with open_sqlite_checkpointer(workspace) as second:
        with pytest.raises(CheckpointIntegrityError):
            await second.lease.acquire("inv-1", owner_id="runner-b")
        with pytest.raises(CheckpointIntegrityError):
            await second.lease.acquire("inv-1", owner_id="runner-b")
