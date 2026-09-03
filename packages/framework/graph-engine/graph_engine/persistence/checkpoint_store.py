from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
from typing import Literal, Protocol
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import WRITES_IDX_MAP


OutboxKind = Literal["checkpoint", "pending_write"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class CheckpointOutboxDraft:
    invocation_id: str
    thread_id: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    kind: OutboxKind
    task_identity: str
    graph_revision: str
    product_lock_digest: str
    root_input_digest: str
    fencing_token: int
    checkpoint_ns: str = ""
    metadata_bytes: bytes = b""
    checkpoint_type: str = "msgpack"
    metadata_type: str = "msgpack"
    write_items: tuple[tuple[str, str, str, bytes, str], ...] = ()

    @property
    def journal_checkpoint_id(self) -> str:
        if self.kind == "pending_write":
            payload = b"\0".join(item[3] for item in self.write_items)
            digest = hashlib.sha256(payload).hexdigest()
            return f"{self.checkpoint_id}::write::{self.task_identity}::{digest}"
        return self.checkpoint_id


@dataclass(frozen=True, slots=True)
class CheckpointOutboxRecord:
    outbox_id: str
    invocation_id: str
    thread_id: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    kind: OutboxKind
    task_identity: str
    graph_revision: str
    product_lock_digest: str
    root_input_digest: str
    fencing_token: int
    checkpoint_ns: str
    journal_checkpoint_id: str
    checkpoint_bytes: bytes
    pending_write_bytes: tuple[bytes, ...]
    metadata_bytes: bytes
    checkpoint_type: str
    metadata_type: str
    write_items: tuple[tuple[str, str, str, bytes, str], ...]
    journal_anchored_at: str | None
    observers_delivered_at: str | None
    abandoned_at: str | None


@dataclass(frozen=True, slots=True)
class RawCheckpointRow:
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    checkpoint_type: str
    checkpoint_bytes: bytes
    metadata_type: str
    metadata_bytes: bytes


@dataclass(frozen=True, slots=True)
class RawPendingWriteRow:
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    task_id: str
    task_path: str
    channel: str
    value_type: str
    value_bytes: bytes
    index: int


class CheckpointStoreTransactionPort(Protocol):
    async def put_checkpoint(
        self,
        config: RunnableConfig,
        checkpoint_bytes: bytes,
        draft: CheckpointOutboxDraft,
    ) -> tuple[RunnableConfig, str]: ...

    async def put_pending_writes(
        self,
        config: RunnableConfig,
        write_bytes: tuple[bytes, ...],
        draft: CheckpointOutboxDraft,
    ) -> str: ...

    async def mark_journal_anchored(self, outbox_id: str) -> None: ...

    async def mark_observers_delivered(self, outbox_id: str) -> None: ...

    async def mark_abandoned(self, outbox_id: str) -> None: ...

    async def read_raw_checkpoint(
        self,
        thread_id: str,
        checkpoint_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> RawCheckpointRow | None: ...

    async def list_raw_checkpoints(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str | None = None,
    ) -> tuple[RawCheckpointRow, ...]: ...

    async def read_raw_pending_writes(
        self,
        thread_id: str,
        checkpoint_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> tuple[RawPendingWriteRow, ...]: ...

    async def scan_incomplete_outbox(self, thread_id: str) -> tuple[CheckpointOutboxRecord, ...]: ...

    async def get_outbox(self, outbox_id: str) -> CheckpointOutboxRecord | None: ...

    async def list_outbox(self, thread_id: str) -> tuple[CheckpointOutboxRecord, ...]: ...


def _record_from_draft(
    *,
    outbox_id: str,
    draft: CheckpointOutboxDraft,
    checkpoint_bytes: bytes,
    pending_write_bytes: tuple[bytes, ...],
    checkpoint_ns: str,
) -> CheckpointOutboxRecord:
    return CheckpointOutboxRecord(
        outbox_id=outbox_id,
        invocation_id=draft.invocation_id,
        thread_id=draft.thread_id,
        checkpoint_id=draft.checkpoint_id,
        parent_checkpoint_id=draft.parent_checkpoint_id,
        kind=draft.kind,
        task_identity=draft.task_identity,
        graph_revision=draft.graph_revision,
        product_lock_digest=draft.product_lock_digest,
        root_input_digest=draft.root_input_digest,
        fencing_token=draft.fencing_token,
        checkpoint_ns=checkpoint_ns,
        journal_checkpoint_id=draft.journal_checkpoint_id,
        checkpoint_bytes=checkpoint_bytes,
        pending_write_bytes=pending_write_bytes,
        metadata_bytes=draft.metadata_bytes,
        checkpoint_type=draft.checkpoint_type,
        metadata_type=draft.metadata_type,
        write_items=draft.write_items,
        journal_anchored_at=None,
        observers_delivered_at=None,
        abandoned_at=None,
    )


class MemoryCheckpointStore:
    def __init__(self) -> None:
        self._checkpoints: dict[tuple[str, str, str], RawCheckpointRow] = {}
        self._writes: dict[tuple[str, str, str], dict[tuple[str, int], RawPendingWriteRow]] = {}
        self._outbox: dict[str, CheckpointOutboxRecord] = {}

    def _namespace(self, config: RunnableConfig, draft: CheckpointOutboxDraft) -> str:
        configurable = config.get("configurable") or {}
        return draft.checkpoint_ns or str(configurable.get("checkpoint_ns", ""))

    async def put_checkpoint(
        self,
        config: RunnableConfig,
        checkpoint_bytes: bytes,
        draft: CheckpointOutboxDraft,
    ) -> tuple[RunnableConfig, str]:
        namespace = self._namespace(config, draft)
        outbox_id = uuid4().hex
        self._checkpoints[(draft.thread_id, namespace, draft.checkpoint_id)] = RawCheckpointRow(
            thread_id=draft.thread_id,
            checkpoint_ns=namespace,
            checkpoint_id=draft.checkpoint_id,
            parent_checkpoint_id=draft.parent_checkpoint_id,
            checkpoint_type=draft.checkpoint_type,
            checkpoint_bytes=checkpoint_bytes,
            metadata_type=draft.metadata_type,
            metadata_bytes=draft.metadata_bytes,
        )
        self._outbox[outbox_id] = _record_from_draft(
            outbox_id=outbox_id,
            draft=draft,
            checkpoint_bytes=checkpoint_bytes,
            pending_write_bytes=(),
            checkpoint_ns=namespace,
        )
        stored: RunnableConfig = {
            "configurable": {
                "thread_id": draft.thread_id,
                "checkpoint_ns": namespace,
                "checkpoint_id": draft.checkpoint_id,
            }
        }
        return stored, outbox_id

    async def put_pending_writes(
        self,
        config: RunnableConfig,
        write_bytes: tuple[bytes, ...],
        draft: CheckpointOutboxDraft,
    ) -> str:
        namespace = self._namespace(config, draft)
        outer_key = (draft.thread_id, namespace, draft.checkpoint_id)
        existing = self._writes.setdefault(outer_key, {})
        for index, item in enumerate(draft.write_items):
            task_id, channel, value_type, value_payload, task_path = item
            inner_key = (task_id, WRITES_IDX_MAP.get(channel, index))
            if inner_key[1] >= 0 and inner_key in existing:
                continue
            existing[inner_key] = RawPendingWriteRow(
                thread_id=draft.thread_id,
                checkpoint_ns=namespace,
                checkpoint_id=draft.checkpoint_id,
                task_id=task_id,
                task_path=task_path,
                channel=channel,
                value_type=value_type,
                value_bytes=value_payload,
                index=index,
            )
        outbox_id = uuid4().hex
        self._outbox[outbox_id] = _record_from_draft(
            outbox_id=outbox_id,
            draft=draft,
            checkpoint_bytes=b"",
            pending_write_bytes=write_bytes,
            checkpoint_ns=namespace,
        )
        return outbox_id

    async def mark_journal_anchored(self, outbox_id: str) -> None:
        record = self._outbox[outbox_id]
        if record.journal_anchored_at is not None:
            return
        self._outbox[outbox_id] = replace(record, journal_anchored_at=_now())

    async def mark_observers_delivered(self, outbox_id: str) -> None:
        record = self._outbox[outbox_id]
        if record.observers_delivered_at is not None:
            return
        self._outbox[outbox_id] = replace(record, observers_delivered_at=_now())

    async def mark_abandoned(self, outbox_id: str) -> None:
        record = self._outbox[outbox_id]
        if record.abandoned_at is not None:
            return
        self._outbox[outbox_id] = replace(record, abandoned_at=_now())

    async def read_raw_checkpoint(
        self,
        thread_id: str,
        checkpoint_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> RawCheckpointRow | None:
        return self._checkpoints.get((thread_id, checkpoint_ns, checkpoint_id))

    async def list_raw_checkpoints(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str | None = None,
    ) -> tuple[RawCheckpointRow, ...]:
        rows = [
            row
            for (stored_thread, stored_ns, _), row in self._checkpoints.items()
            if stored_thread == thread_id and (checkpoint_ns is None or stored_ns == checkpoint_ns)
        ]
        return tuple(sorted(rows, key=lambda item: item.checkpoint_id, reverse=True))

    async def read_raw_pending_writes(
        self,
        thread_id: str,
        checkpoint_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> tuple[RawPendingWriteRow, ...]:
        writes = self._writes.get((thread_id, checkpoint_ns, checkpoint_id), {})
        return tuple(writes[key] for key in sorted(writes, key=lambda item: (item[0], item[1])))

    async def scan_incomplete_outbox(self, thread_id: str) -> tuple[CheckpointOutboxRecord, ...]:
        return tuple(
            record
            for record in self._outbox.values()
            if record.thread_id == thread_id
            and record.abandoned_at is None
            and (record.journal_anchored_at is None or record.observers_delivered_at is None)
        )

    async def get_outbox(self, outbox_id: str) -> CheckpointOutboxRecord | None:
        return self._outbox.get(outbox_id)

    async def list_outbox(self, thread_id: str) -> tuple[CheckpointOutboxRecord, ...]:
        return tuple(record for record in self._outbox.values() if record.thread_id == thread_id)

    def drop_checkpoint_bytes(self, thread_id: str, checkpoint_id: str, *, checkpoint_ns: str = "") -> None:
        row = self._checkpoints.get((thread_id, checkpoint_ns, checkpoint_id))
        if row is None:
            return
        self._checkpoints[(thread_id, checkpoint_ns, checkpoint_id)] = replace(row, checkpoint_bytes=b"")

    def replace_checkpoint_bytes(
        self,
        thread_id: str,
        checkpoint_id: str,
        checkpoint_bytes: bytes,
        *,
        checkpoint_ns: str = "",
    ) -> None:
        row = self._checkpoints[(thread_id, checkpoint_ns, checkpoint_id)]
        self._checkpoints[(thread_id, checkpoint_ns, checkpoint_id)] = replace(
            row, checkpoint_bytes=checkpoint_bytes
        )

    def replace_parent_checkpoint_id(
        self,
        thread_id: str,
        checkpoint_id: str,
        parent_checkpoint_id: str | None,
        *,
        checkpoint_ns: str = "",
    ) -> None:
        row = self._checkpoints[(thread_id, checkpoint_ns, checkpoint_id)]
        self._checkpoints[(thread_id, checkpoint_ns, checkpoint_id)] = replace(
            row, parent_checkpoint_id=parent_checkpoint_id
        )

    def replace_outbox_identity(self, thread_id: str, checkpoint_id: str, **fields: object) -> None:
        for outbox_id, record in self._outbox.items():
            if (
                record.thread_id == thread_id
                and record.checkpoint_id == checkpoint_id
                and record.kind == "checkpoint"
            ):
                self._outbox[outbox_id] = replace(record, **fields)  # type: ignore[arg-type]
                return
        raise KeyError(f"checkpoint outbox not found for {thread_id}/{checkpoint_id}")


__all__ = [
    "CheckpointOutboxDraft",
    "CheckpointOutboxRecord",
    "CheckpointStoreTransactionPort",
    "MemoryCheckpointStore",
    "RawCheckpointRow",
    "RawPendingWriteRow",
]
