from __future__ import annotations

import inspect
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

from graph_engine.persistence.checkpoint_store import (
    CheckpointOutboxDraft,
    CheckpointOutboxRecord,
    CheckpointStoreTransactionPort,
    MemoryCheckpointStore,
)
from graph_engine.persistence.journal import (
    CheckpointAnchor,
    CheckpointAnchorState,
    CheckpointIntegrityError,
    InvocationStarted,
)

REVISION = "a" * 64
LOCK = "b" * 64
INPUT = "c" * 64
FENCE = 4


def identity(*, fencing_token: int = FENCE) -> CheckpointAnchorState:
    return CheckpointAnchorState(
        invocation_id="inv-1",
        thread_id="inv-1",
        graph_revision=REVISION,
        product_lock_digest=LOCK,
        root_input_digest=INPUT,
        fencing_token=fencing_token,
    )


def started(*, fencing_token: int = FENCE) -> InvocationStarted:
    return InvocationStarted(
        invocation_id="inv-1",
        thread_id="inv-1",
        graph_revision=REVISION,
        product_lock_digest=LOCK,
        root_input_digest=INPUT,
        fencing_token=fencing_token,
    )


def run_config(*, checkpoint_id: str = "cp-1") -> RunnableConfig:
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


def sample_checkpoint(*, checkpoint_id: str = "cp-2") -> Checkpoint:
    return {
        "v": 2,
        "id": checkpoint_id,
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"result": {"ok": True}},
        "channel_versions": {"result": 1},
        "versions_seen": {},
        "updated_channels": ["result"],
    }


def sample_metadata() -> CheckpointMetadata:
    return {"source": "loop", "step": 1, "parents": {}}


def checkpoint_draft(
    *,
    checkpoint_id: str = "cp-2",
    parent_checkpoint_id: str | None = "cp-1",
    fencing_token: int = FENCE,
    metadata_bytes: bytes = b"meta",
) -> CheckpointOutboxDraft:
    return CheckpointOutboxDraft(
        invocation_id="inv-1",
        thread_id="inv-1",
        checkpoint_id=checkpoint_id,
        parent_checkpoint_id=parent_checkpoint_id,
        kind="checkpoint",
        task_identity="",
        graph_revision=REVISION,
        product_lock_digest=LOCK,
        root_input_digest=INPUT,
        fencing_token=fencing_token,
        metadata_bytes=metadata_bytes,
    )


def pending_write_draft(
    *,
    checkpoint_id: str = "cp-1",
    task_identity: str = "task-a:push-0",
    fencing_token: int = FENCE,
    write_items: tuple[tuple[str, str, str, bytes, str], ...] = (),
) -> CheckpointOutboxDraft:
    return CheckpointOutboxDraft(
        invocation_id="inv-1",
        thread_id="inv-1",
        checkpoint_id=checkpoint_id,
        parent_checkpoint_id=None,
        kind="pending_write",
        task_identity=task_identity,
        graph_revision=REVISION,
        product_lock_digest=LOCK,
        root_input_digest=INPUT,
        fencing_token=fencing_token,
        write_items=write_items,
    )


class MemoryCheckpointAnchorJournal:
    def __init__(self) -> None:
        self._started: dict[str, InvocationStarted] = {}
        self._anchors: dict[tuple[str, str], CheckpointAnchor] = {}

    def only_pending_write_anchor(self) -> CheckpointAnchor:
        pending = [anchor for anchor in self._anchors.values() if anchor.pending_write_bytes]
        if len(pending) != 1:
            raise AssertionError(f"expected exactly one pending-write anchor, found {len(pending)}")
        return pending[0]

    def pending_write_anchors(self) -> tuple[CheckpointAnchor, ...]:
        return tuple(anchor for anchor in self._anchors.values() if anchor.pending_write_bytes)

    def advance_fence(self, invocation_id: str, fencing_token: int) -> None:
        existing = self._started[invocation_id]
        self._started[invocation_id] = InvocationStarted(
            invocation_id=existing.invocation_id,
            thread_id=existing.thread_id,
            graph_revision=existing.graph_revision,
            product_lock_digest=existing.product_lock_digest,
            root_input_digest=existing.root_input_digest,
            fencing_token=fencing_token,
        )

    async def start_invocation(self, record: InvocationStarted, *, fencing_token: int) -> None:
        await self.assert_current_fence(record.invocation_id, fencing_token)
        existing = self._started.get(record.invocation_id)
        if existing is None:
            self._started[record.invocation_id] = record
            return
        if existing != record:
            raise CheckpointIntegrityError("invocation identity drifted")

    async def append_checkpoint_anchor(self, anchor: CheckpointAnchor, *, fencing_token: int) -> None:
        await self.assert_current_fence(anchor.invocation_id, fencing_token)
        started_record = self._started.get(anchor.invocation_id)
        if started_record is None:
            raise CheckpointIntegrityError("invocation has not started")
        if anchor.anchor_state() != started_record.anchor_state():
            raise CheckpointIntegrityError("checkpoint identity drifted from invocation")
        key = (anchor.thread_id, anchor.checkpoint_id)
        existing = self._anchors.get(key)
        if existing is None:
            self._anchors[key] = anchor
            return
        if existing != anchor:
            raise CheckpointIntegrityError("checkpoint identity drifted")

    async def read_checkpoint_anchor(self, thread_id: str, checkpoint_id: str) -> CheckpointAnchor | None:
        return self._anchors.get((thread_id, checkpoint_id))

    async def assert_current_fence(self, invocation_id: str, fencing_token: int) -> None:
        started_record = self._started.get(invocation_id)
        if started_record is None:
            if fencing_token < 1:
                raise CheckpointIntegrityError("fencing token is stale")
            return
        if started_record.fencing_token != fencing_token:
            raise CheckpointIntegrityError("fencing token is stale")


class RecordingObserver:
    def __init__(self) -> None:
        self.notices: list[Any] = []

    async def on_anchored(self, notice: Any) -> None:
        self.notices.append(notice)


def test_store_port_separates_checkpoint_and_pending_write_transactions() -> None:
    methods = {
        name
        for name, value in inspect.getmembers(CheckpointStoreTransactionPort)
        if callable(value) and not name.startswith("_")
    }
    assert methods == {
        "put_checkpoint",
        "put_pending_writes",
        "mark_journal_anchored",
        "mark_observers_delivered",
        "mark_abandoned",
        "read_raw_checkpoint",
        "list_raw_checkpoints",
        "read_raw_pending_writes",
        "scan_incomplete_outbox",
        "get_outbox",
        "list_outbox",
    }


@pytest.fixture
def store() -> MemoryCheckpointStore:
    return MemoryCheckpointStore()


async def test_put_checkpoint_commits_data_row_and_outbox_together(store: MemoryCheckpointStore) -> None:
    stored_config, outbox_id = await store.put_checkpoint(
        run_config(),
        b"checkpoint-bytes",
        checkpoint_draft(),
    )
    assert stored_config["configurable"]["checkpoint_id"] == "cp-2"
    raw = await store.read_raw_checkpoint("inv-1", "cp-2")
    assert raw is not None
    assert raw.checkpoint_bytes == b"checkpoint-bytes"
    record = await store.get_outbox(outbox_id)
    assert isinstance(record, CheckpointOutboxRecord)
    assert record.kind == "checkpoint"
    assert record.checkpoint_bytes == b"checkpoint-bytes"
    assert record.journal_anchored_at is None
    assert record.observers_delivered_at is None
    assert record.abandoned_at is None


async def test_put_pending_writes_commits_write_row_and_outbox_together(
    store: MemoryCheckpointStore,
) -> None:
    write_bytes = (b"write-a",)
    outbox_id = await store.put_pending_writes(
        run_config(),
        write_bytes,
        pending_write_draft(
            write_items=(("task-a", "result", "msgpack", b"write-a", "push-0"),),
        ),
    )
    writes = await store.read_raw_pending_writes("inv-1", "cp-1")
    assert writes
    assert writes[0].value_bytes == b"write-a"
    record = await store.get_outbox(outbox_id)
    assert record is not None
    assert record.kind == "pending_write"
    assert record.pending_write_bytes == write_bytes
    assert record.task_identity == "task-a:push-0"
    assert record.journal_checkpoint_id.startswith("cp-1::write::task-a:push-0::")


async def test_mark_journal_anchored_and_observers_delivered_are_idempotent(
    store: MemoryCheckpointStore,
) -> None:
    _, outbox_id = await store.put_checkpoint(run_config(), b"checkpoint-bytes", checkpoint_draft())
    await store.mark_journal_anchored(outbox_id)
    await store.mark_journal_anchored(outbox_id)
    await store.mark_observers_delivered(outbox_id)
    await store.mark_observers_delivered(outbox_id)
    record = await store.get_outbox(outbox_id)
    assert record is not None
    assert record.journal_anchored_at is not None
    assert record.observers_delivered_at is not None
    incomplete = await store.scan_incomplete_outbox("inv-1")
    assert incomplete == ()


async def test_raw_read_sees_unanchored_rows_and_scan_finds_incomplete(
    store: MemoryCheckpointStore,
) -> None:
    _, outbox_id = await store.put_checkpoint(run_config(), b"checkpoint-bytes", checkpoint_draft())
    raw = await store.read_raw_checkpoint("inv-1", "cp-2")
    assert raw is not None
    listed = await store.list_raw_checkpoints("inv-1")
    assert [row.checkpoint_id for row in listed] == ["cp-2"]
    incomplete = await store.scan_incomplete_outbox("inv-1")
    assert [record.outbox_id for record in incomplete] == [outbox_id]
    await store.mark_abandoned(outbox_id)
    abandoned = await store.get_outbox(outbox_id)
    assert abandoned is not None
    assert abandoned.abandoned_at is not None
