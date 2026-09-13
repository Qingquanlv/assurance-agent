from __future__ import annotations

import asyncio
import inspect
import sqlite3
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from assurance_product import open_sqlite_checkpointer
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.sqlite_checkpointer import (
    SQLITE_DEPLOYMENT_MODE,
    SqliteCheckpointStoreTransaction,
)
from graph_engine.persistence.checkpoint_store import CheckpointOutboxDraft
from graph_engine.persistence.journal import CheckpointAnchorState, strict_checkpoint_serializer


REVISION = "a" * 64
LOCK = "b" * 64
INPUT = "c" * 64
FENCE = 4


def _workspace(tmp_path: Path) -> ChangeWorkspace:
    project = (tmp_path / "project").resolve()
    (project / "qa").mkdir(parents=True)
    return ChangeWorkspace.prepare(project, "CH-1")


def _identity(*, fencing_token: int = FENCE) -> CheckpointAnchorState:
    return CheckpointAnchorState(
        invocation_id="inv-1",
        thread_id="inv-1",
        graph_revision=REVISION,
        product_lock_digest=LOCK,
        root_input_digest=INPUT,
        fencing_token=fencing_token,
    )


def _config(*, checkpoint_id: str = "cp-1") -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
            "assurance_revision_id": REVISION,
            "assurance_product_lock_digest": LOCK,
            "assurance_root_input_digest": INPUT,
            "assurance_fencing_token": FENCE,
        }
    }


def _checkpoint(*, checkpoint_id: str = "cp-2") -> Checkpoint:
    return {
        "v": 2,
        "id": checkpoint_id,
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"result": {"ok": True}},
        "channel_versions": {"result": 1},
        "versions_seen": {},
        "updated_channels": ["result"],
    }


def _checkpoint_draft(*, metadata_bytes: bytes) -> CheckpointOutboxDraft:
    return CheckpointOutboxDraft(
        invocation_id="inv-1",
        thread_id="inv-1",
        checkpoint_id="cp-2",
        parent_checkpoint_id="cp-1",
        kind="checkpoint",
        task_identity="",
        graph_revision=REVISION,
        product_lock_digest=LOCK,
        root_input_digest=INPUT,
        fencing_token=FENCE,
        metadata_bytes=metadata_bytes,
        checkpoint_type="msgpack",
        metadata_type="msgpack",
    )


def test_product_backend_factory_exports_single_host_sqlite() -> None:
    assert SQLITE_DEPLOYMENT_MODE == "single-host"
    assert inspect.isfunction(open_sqlite_checkpointer)
    assert "observers" not in inspect.signature(open_sqlite_checkpointer).parameters
    source = inspect.getsource(SqliteCheckpointStoreTransaction)
    assert "aput(" not in source
    assert "aput_writes(" not in source


def test_checkpointer_rejects_use_before_observers_sealed(tmp_path: Path) -> None:
    asyncio.run(_checkpointer_rejects_use_before_observers_sealed(tmp_path))


async def _checkpointer_rejects_use_before_observers_sealed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as backend:
        with pytest.raises(RuntimeError, match="sealed"):
            backend.checkpointer(_identity())


def test_observer_mutation_is_rejected_after_seal(tmp_path: Path) -> None:
    asyncio.run(_observer_mutation_is_rejected_after_seal(tmp_path))


async def _observer_mutation_is_rejected_after_seal(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as backend:
        backend.install_observers(())
        backend.seal_observers()
        with pytest.raises(RuntimeError, match="sealed"):
            backend.install_observers(())
        saver = backend.checkpointer(_identity())
        assert saver._observers == ()


def test_install_then_seal_registers_observers(tmp_path: Path) -> None:
    asyncio.run(_install_then_seal_registers_observers(tmp_path))


async def _install_then_seal_registers_observers(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    class _Observer:
        async def on_anchored(self, notice: object) -> None:
            del notice

    observer = _Observer()
    async with open_sqlite_checkpointer(workspace) as backend:
        backend.install_observers((observer,))  # type: ignore[arg-type]
        backend.seal_observers()
        saver = backend.checkpointer(_identity())
        assert saver._observers == (observer,)


def test_factory_opens_absolute_runtime_owned_control_path(tmp_path: Path) -> None:
    asyncio.run(_factory_opens_absolute_runtime_owned_control_path(tmp_path))


async def _factory_opens_absolute_runtime_owned_control_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    async with open_sqlite_checkpointer(workspace) as backend:
        assert workspace.paths.langgraph_root.is_dir()
        assert not workspace.paths.langgraph_root.is_symlink()
        assert workspace.paths.langgraph_checkpoints.is_file()
        assert not workspace.paths.langgraph_checkpoints.is_symlink()
        assert workspace.paths.langgraph_checkpoints.is_absolute()
        assert workspace.paths.langgraph_leases.is_dir()
        assert workspace.paths.langgraph_identities.is_dir()
        assert await backend.full_synchronous()
        assert getattr(backend.serializer, "pickle_fallback") is False
        backend.seal_observers()
        saver = backend.checkpointer(_identity())
        assert getattr(saver.serde, "pickle_fallback") is False
        typed = saver.serde.dumps_typed({"invocation_id": "inv-1", "round": 1})
        assert typed[0] == "msgpack"
        with pytest.raises((TypeError, ValueError)):
            saver.serde.dumps_typed(object())
    workspace.initialize()


def test_sql_adapter_matches_upstream_put_get_list_and_pending_writes(tmp_path: Path) -> None:
    asyncio.run(_sql_adapter_matches_upstream(tmp_path))


async def _sql_adapter_matches_upstream(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    serde = strict_checkpoint_serializer()
    checkpoint = _checkpoint()
    checkpoint_type, checkpoint_bytes = serde.dumps_typed(checkpoint)
    write_value = {"ok": True}
    write_type, write_bytes = serde.dumps_typed(write_value)
    metadata: CheckpointMetadata = {"source": "loop", "step": 1, "parents": {}}
    config = _config()
    write_draft = CheckpointOutboxDraft(
        invocation_id="inv-1",
        thread_id="inv-1",
        checkpoint_id="cp-1",
        parent_checkpoint_id=None,
        kind="pending_write",
        task_identity="task-a:push-0",
        graph_revision=REVISION,
        product_lock_digest=LOCK,
        root_input_digest=INPUT,
        fencing_token=FENCE,
        write_items=(("task-a", "result", write_type, write_bytes, "push-0"),),
    )

    async with open_sqlite_checkpointer(workspace) as backend:
        stored_config, outbox_id = await backend.store.put_checkpoint(
            config,
            checkpoint_bytes,
            _checkpoint_draft(metadata_bytes=b"meta"),
        )
        configurable = stored_config.get("configurable") or {}
        assert configurable["checkpoint_id"] == "cp-2"
        assert outbox_id
        raw = await backend.store.read_raw_checkpoint("inv-1", "cp-2")
        assert raw is not None
        assert raw.checkpoint_bytes == checkpoint_bytes
        assert raw.checkpoint_type == checkpoint_type
        listed = await backend.store.list_raw_checkpoints("inv-1")
        assert [row.checkpoint_id for row in listed] == ["cp-2"]
        write_outbox = await backend.store.put_pending_writes(config, (write_bytes,), write_draft)
        assert write_outbox
        writes = await backend.store.read_raw_pending_writes("inv-1", "cp-1")
        assert writes
        assert writes[0].value_bytes == write_bytes
        assert writes[0].channel == "result"
        incomplete = await backend.store.scan_incomplete_outbox("inv-1")
        assert {record.outbox_id for record in incomplete} == {outbox_id, write_outbox}

    upstream_path = tmp_path / "upstream.sqlite3"
    async with AsyncSqliteSaver.from_conn_string(str(upstream_path)) as upstream:
        upstream.serde = serde
        await upstream.aput(config, checkpoint, metadata, {"result": 1})
        await upstream.aput_writes(config, [("result", write_value)], "task-a", "push-0")

    ours = _table_rows(workspace.paths.langgraph_checkpoints)
    theirs = _table_rows(upstream_path)
    assert ours["checkpoints"][0][0:6] == theirs["checkpoints"][0][0:6]
    assert ours["writes"][0][0:7] == theirs["writes"][0][0:7]


def _table_rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path) as conn:
        checkpoints = list(
            conn.execute(
                "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint "
                "FROM checkpoints ORDER BY checkpoint_id"
            )
        )
        writes = list(
            conn.execute(
                "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type "
                "FROM writes ORDER BY task_id, idx"
            )
        )
    return {"checkpoints": checkpoints, "writes": writes}
