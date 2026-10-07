from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.base import WRITES_IDX_MAP
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from assurance_product.change_workspace import ChangePaths, ChangeWorkspace
from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer
from graph_engine.persistence.checkpoint_observer import CheckpointAnchorObserverPort
from graph_engine.persistence.checkpoint_store import (
    CheckpointOutboxDraft,
    CheckpointOutboxRecord,
    RawCheckpointRow,
    RawPendingWriteRow,
    _record_from_draft,
)
from graph_engine.persistence.journal import (
    CheckpointAnchor,
    CheckpointAnchorState,
    CheckpointIntegrityError,
    InvocationStarted,
    strict_checkpoint_serializer,
)
from graph_engine.persistence.runner_lease import (
    InvocationRunnerLeasePort,
    LocalInvocationRunnerLease,
    RunnerLease,
)


SQLITE_DEPLOYMENT_MODE = "single-host"

_UPSERT_CHECKPOINT_SQL = (
    "INSERT OR REPLACE INTO checkpoints "
    "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_REPLACE_WRITE_SQL = (
    "INSERT OR REPLACE INTO writes "
    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_IGNORE_WRITE_SQL = (
    "INSERT OR IGNORE INTO writes "
    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_OUTBOX_SELECT = (
    "SELECT outbox_id, invocation_id, thread_id, checkpoint_id, parent_checkpoint_id, kind, "
    "task_identity, graph_revision, product_lock_digest, root_input_digest, fencing_token, "
    "checkpoint_ns, journal_checkpoint_id, checkpoint_bytes, pending_write_bytes, "
    "metadata_bytes, checkpoint_type, metadata_type, write_items, "
    "journal_anchored_at, observers_delivered_at, abandoned_at FROM assurance_outbox"
)
_INSERT_OUTBOX_SQL = (
    "INSERT INTO assurance_outbox ("
    "outbox_id, invocation_id, thread_id, checkpoint_id, parent_checkpoint_id, kind, "
    "task_identity, graph_revision, product_lock_digest, root_input_digest, fencing_token, "
    "checkpoint_ns, journal_checkpoint_id, checkpoint_bytes, pending_write_bytes, "
    "metadata_bytes, checkpoint_type, metadata_type, write_items, "
    "journal_anchored_at, observers_delivered_at, abandoned_at"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)"
)
_ASSURANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS assurance_outbox (
    outbox_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    kind TEXT NOT NULL,
    task_identity TEXT NOT NULL,
    graph_revision TEXT NOT NULL,
    product_lock_digest TEXT NOT NULL,
    root_input_digest TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    journal_checkpoint_id TEXT NOT NULL,
    checkpoint_bytes BLOB NOT NULL,
    pending_write_bytes BLOB NOT NULL,
    metadata_bytes BLOB NOT NULL,
    checkpoint_type TEXT NOT NULL,
    metadata_type TEXT NOT NULL,
    write_items BLOB NOT NULL,
    journal_anchored_at TEXT,
    observers_delivered_at TEXT,
    abandoned_at TEXT
);
CREATE TABLE IF NOT EXISTS assurance_invocations (
    invocation_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    graph_revision TEXT NOT NULL,
    product_lock_digest TEXT NOT NULL,
    root_input_digest TEXT NOT NULL,
    fencing_token INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assurance_anchors (
    thread_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    checkpoint_bytes BLOB NOT NULL,
    pending_write_bytes BLOB NOT NULL,
    task_identity TEXT NOT NULL,
    graph_revision TEXT NOT NULL,
    product_lock_digest TEXT NOT NULL,
    root_input_digest TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    anchor_digest TEXT NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS assurance_entrypoints (
    invocation_id TEXT PRIMARY KEY,
    entrypoint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assurance_attempt_batches (
    attempt_key_digest TEXT NOT NULL,
    revision INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    record_digest TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (attempt_key_digest, revision)
);
CREATE TABLE IF NOT EXISTS assurance_attempt_generations (
    scope_digest TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    scope BLOB NOT NULL,
    attempt_key_digest TEXT NOT NULL UNIQUE,
    abandoned INTEGER NOT NULL DEFAULT 0,
    owner_nonce TEXT,
    input_payload BLOB NOT NULL,
    PRIMARY KEY (scope_digest, ordinal)
);
CREATE TABLE IF NOT EXISTS assurance_host_calls (
    call_digest TEXT PRIMARY KEY,
    owner_nonce TEXT NOT NULL,
    attempt_key_digest TEXT NOT NULL,
    payload BLOB NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assurance_attempt_durable (
    attempt_key_digest TEXT PRIMARY KEY,
    durable_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assurance_resource_authorizations (
    revision INTEGER PRIMARY KEY,
    schema_version TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    record_digest TEXT NOT NULL,
    payload BLOB NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_bytes_tuple(items: tuple[bytes, ...]) -> bytes:
    import base64
    import json

    return json.dumps([base64.b64encode(item).decode("ascii") for item in items]).encode("ascii")


def _decode_bytes_tuple(payload: bytes | None) -> tuple[bytes, ...]:
    import base64
    import json

    if not payload:
        return ()
    raw = json.loads(bytes(payload).decode("ascii"))
    return tuple(base64.b64decode(item) for item in raw)


def _encode_write_items(items: tuple[tuple[str, str, str, bytes, str], ...]) -> bytes:
    import base64
    import json

    return json.dumps(
        [
            [task_id, channel, value_type, base64.b64encode(value).decode("ascii"), task_path]
            for task_id, channel, value_type, value, task_path in items
        ]
    ).encode("ascii")


def _decode_write_items(payload: bytes | None) -> tuple[tuple[str, str, str, bytes, str], ...]:
    import base64
    import json

    if not payload:
        return ()
    raw = json.loads(bytes(payload).decode("ascii"))
    return tuple((item[0], item[1], item[2], base64.b64decode(item[3]), item[4]) for item in raw)


def _namespace(config: Any, draft: CheckpointOutboxDraft) -> str:
    configurable = config.get("configurable") or {}
    return draft.checkpoint_ns or str(configurable.get("checkpoint_ns", ""))


class SqliteCheckpointStoreTransaction:
    def __init__(self, conn: Any, lock: asyncio.Lock) -> None:
        self._conn = conn
        self._lock = lock

    async def put_checkpoint(
        self,
        config: Any,
        checkpoint_bytes: bytes,
        draft: CheckpointOutboxDraft,
    ) -> Any:
        namespace = _namespace(config, draft)
        outbox_id = uuid4().hex
        record = _record_from_draft(
            outbox_id=outbox_id,
            draft=draft,
            checkpoint_bytes=checkpoint_bytes,
            pending_write_bytes=(),
            checkpoint_ns=namespace,
        )
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._conn.execute(
                    _UPSERT_CHECKPOINT_SQL,
                    (
                        draft.thread_id,
                        namespace,
                        draft.checkpoint_id,
                        draft.parent_checkpoint_id,
                        draft.checkpoint_type,
                        checkpoint_bytes,
                        draft.metadata_bytes,
                    ),
                )
                await self._insert_outbox(record)
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        stored = {
            "configurable": {
                "thread_id": draft.thread_id,
                "checkpoint_ns": namespace,
                "checkpoint_id": draft.checkpoint_id,
            }
        }
        return stored, outbox_id

    async def put_pending_writes(
        self,
        config: Any,
        write_bytes: tuple[bytes, ...],
        draft: CheckpointOutboxDraft,
    ) -> str:
        namespace = _namespace(config, draft)
        outbox_id = uuid4().hex
        query = (
            _REPLACE_WRITE_SQL
            if all(item[1] in WRITES_IDX_MAP for item in draft.write_items)
            else _IGNORE_WRITE_SQL
        )
        record = _record_from_draft(
            outbox_id=outbox_id,
            draft=draft,
            checkpoint_bytes=b"",
            pending_write_bytes=write_bytes,
            checkpoint_ns=namespace,
        )
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                for index, item in enumerate(draft.write_items):
                    task_id, channel, value_type, value_payload, _task_path = item
                    await self._conn.execute(
                        query,
                        (
                            draft.thread_id,
                            namespace,
                            draft.checkpoint_id,
                            task_id,
                            WRITES_IDX_MAP.get(channel, index),
                            channel,
                            value_type,
                            value_payload,
                        ),
                    )
                await self._insert_outbox(record)
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        return outbox_id

    async def mark_journal_anchored(self, outbox_id: str) -> None:
        await self._mark(outbox_id, "journal_anchored_at")

    async def mark_observers_delivered(self, outbox_id: str) -> None:
        await self._mark(outbox_id, "observers_delivered_at")

    async def mark_abandoned(self, outbox_id: str) -> None:
        await self._mark(outbox_id, "abandoned_at")

    async def read_raw_checkpoint(
        self,
        thread_id: str,
        checkpoint_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> RawCheckpointRow | None:
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, "
                "checkpoint, metadata FROM checkpoints "
                "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                (thread_id, checkpoint_ns, checkpoint_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _raw_checkpoint(row)

    async def list_raw_checkpoints(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str | None = None,
    ) -> tuple[RawCheckpointRow, ...]:
        if checkpoint_ns is None:
            sql = (
                "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, "
                "checkpoint, metadata FROM checkpoints WHERE thread_id = ? "
                "ORDER BY checkpoint_id DESC"
            )
            params: tuple[object, ...] = (thread_id,)
        else:
            sql = (
                "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, "
                "checkpoint, metadata FROM checkpoints "
                "WHERE thread_id = ? AND checkpoint_ns = ? ORDER BY checkpoint_id DESC"
            )
            params = (thread_id, checkpoint_ns)
        async with self._lock:
            cursor = await self._conn.execute(sql, params)
            rows = await cursor.fetchall()
        return tuple(_raw_checkpoint(row) for row in rows)

    async def read_raw_pending_writes(
        self,
        thread_id: str,
        checkpoint_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> tuple[RawPendingWriteRow, ...]:
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT task_id, idx, channel, type, value FROM writes "
                "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ? "
                "ORDER BY task_id, idx",
                (thread_id, checkpoint_ns, checkpoint_id),
            )
            rows = await cursor.fetchall()
        paths = await self._task_paths(thread_id, checkpoint_id)
        return tuple(
            RawPendingWriteRow(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=checkpoint_id,
                task_id=str(row[0]),
                task_path=paths.get((str(row[0]), str(row[2])), ""),
                channel=str(row[2]),
                value_type=str(row[3] or ""),
                value_bytes=bytes(row[4] or b""),
                index=int(row[1]),
            )
            for row in rows
        )

    async def scan_incomplete_outbox(self, thread_id: str) -> tuple[CheckpointOutboxRecord, ...]:
        return tuple(
            record
            for record in await self.list_outbox(thread_id)
            if record.abandoned_at is None
            and (record.journal_anchored_at is None or record.observers_delivered_at is None)
        )

    async def get_outbox(self, outbox_id: str) -> CheckpointOutboxRecord | None:
        async with self._lock:
            cursor = await self._conn.execute(
                f"{_OUTBOX_SELECT} WHERE outbox_id = ?",
                (outbox_id,),
            )
            row = await cursor.fetchone()
        return _outbox_record(row) if row is not None else None

    async def list_outbox(self, thread_id: str) -> tuple[CheckpointOutboxRecord, ...]:
        async with self._lock:
            cursor = await self._conn.execute(
                f"{_OUTBOX_SELECT} WHERE thread_id = ?",
                (thread_id,),
            )
            rows = await cursor.fetchall()
        return tuple(_outbox_record(row) for row in rows)

    async def _insert_outbox(self, record: CheckpointOutboxRecord) -> None:
        await self._conn.execute(
            _INSERT_OUTBOX_SQL,
            (
                record.outbox_id,
                record.invocation_id,
                record.thread_id,
                record.checkpoint_id,
                record.parent_checkpoint_id,
                record.kind,
                record.task_identity,
                record.graph_revision,
                record.product_lock_digest,
                record.root_input_digest,
                record.fencing_token,
                record.checkpoint_ns,
                record.journal_checkpoint_id,
                record.checkpoint_bytes,
                _encode_bytes_tuple(record.pending_write_bytes),
                record.metadata_bytes,
                record.checkpoint_type,
                record.metadata_type,
                _encode_write_items(record.write_items),
            ),
        )

    async def _mark(self, outbox_id: str, column: str) -> None:
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._conn.execute(
                    f"UPDATE assurance_outbox SET {column} = ? WHERE outbox_id = ? AND {column} IS NULL",
                    (_now(), outbox_id),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

    async def _task_paths(self, thread_id: str, checkpoint_id: str) -> dict[tuple[str, str], str]:
        paths: dict[tuple[str, str], str] = {}
        for record in await self.list_outbox(thread_id):
            if record.checkpoint_id != checkpoint_id:
                continue
            for task_id, channel, _value_type, _value, task_path in record.write_items:
                paths[(task_id, channel)] = task_path
        return paths


class SqliteCheckpointAnchorJournal:
    def __init__(self, conn: Any, lock: asyncio.Lock) -> None:
        self._conn = conn
        self._lock = lock

    async def start_invocation(self, record: InvocationStarted, *, fencing_token: int) -> None:
        await self.assert_current_fence(record.invocation_id, fencing_token)
        existing = await self.read_invocation_started(record.invocation_id)
        if existing is None:
            async with self._lock:
                await self._conn.execute("BEGIN IMMEDIATE")
                try:
                    await self._conn.execute(
                        "INSERT INTO assurance_invocations ("
                        "invocation_id, thread_id, graph_revision, product_lock_digest, "
                        "root_input_digest, fencing_token"
                        ") VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            record.invocation_id,
                            record.thread_id,
                            record.graph_revision,
                            record.product_lock_digest,
                            record.root_input_digest,
                            record.fencing_token,
                        ),
                    )
                    await self._conn.commit()
                except BaseException:
                    await self._conn.rollback()
                    raise
            return
        if existing != record:
            raise CheckpointIntegrityError("invocation identity drifted")

    async def append_checkpoint_anchor(self, anchor: CheckpointAnchor, *, fencing_token: int) -> None:
        await self.assert_current_fence(anchor.invocation_id, fencing_token)
        started_record = await self.read_invocation_started(anchor.invocation_id)
        if started_record is None:
            raise CheckpointIntegrityError("invocation has not started")
        if anchor.anchor_state() != started_record.anchor_state():
            raise CheckpointIntegrityError("checkpoint identity drifted from invocation")
        existing = await self.read_checkpoint_anchor(anchor.thread_id, anchor.checkpoint_id)
        if existing is None:
            async with self._lock:
                await self._conn.execute("BEGIN IMMEDIATE")
                try:
                    await self._conn.execute(
                        "INSERT INTO assurance_anchors ("
                        "thread_id, checkpoint_id, invocation_id, parent_checkpoint_id, "
                        "checkpoint_bytes, pending_write_bytes, task_identity, graph_revision, "
                        "product_lock_digest, root_input_digest, fencing_token, anchor_digest"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            anchor.thread_id,
                            anchor.checkpoint_id,
                            anchor.invocation_id,
                            anchor.parent_checkpoint_id,
                            anchor.checkpoint_bytes,
                            _encode_bytes_tuple(anchor.pending_write_bytes),
                            anchor.task_identity,
                            anchor.graph_revision,
                            anchor.product_lock_digest,
                            anchor.root_input_digest,
                            anchor.fencing_token,
                            anchor.anchor_digest,
                        ),
                    )
                    await self._conn.commit()
                except BaseException:
                    await self._conn.rollback()
                    raise
            return
        if existing != anchor:
            raise CheckpointIntegrityError("checkpoint identity drifted")

    async def read_checkpoint_anchor(self, thread_id: str, checkpoint_id: str) -> CheckpointAnchor | None:
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT invocation_id, thread_id, checkpoint_id, parent_checkpoint_id, "
                "checkpoint_bytes, pending_write_bytes, task_identity, graph_revision, "
                "product_lock_digest, root_input_digest, fencing_token, anchor_digest "
                "FROM assurance_anchors WHERE thread_id = ? AND checkpoint_id = ?",
                (thread_id, checkpoint_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return CheckpointAnchor(
            invocation_id=str(row[0]),
            thread_id=str(row[1]),
            checkpoint_id=str(row[2]),
            parent_checkpoint_id=None if row[3] is None else str(row[3]),
            checkpoint_bytes=bytes(row[4] or b""),
            pending_write_bytes=_decode_bytes_tuple(row[5]),
            task_identity=str(row[6]),
            graph_revision=str(row[7]),
            product_lock_digest=str(row[8]),
            root_input_digest=str(row[9]),
            fencing_token=int(row[10]),
            anchor_digest=str(row[11]),
        )

    async def assert_current_fence(self, invocation_id: str, fencing_token: int) -> None:
        started_record = await self.read_invocation_started(invocation_id)
        if started_record is None:
            if fencing_token < 1:
                raise CheckpointIntegrityError("fencing token is stale")
            return
        if started_record.fencing_token != fencing_token:
            raise CheckpointIntegrityError("fencing token is stale")

    async def read_invocation_started(self, invocation_id: str) -> InvocationStarted | None:
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT invocation_id, thread_id, graph_revision, product_lock_digest, "
                "root_input_digest, fencing_token FROM assurance_invocations "
                "WHERE invocation_id = ?",
                (invocation_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return InvocationStarted(
            invocation_id=str(row[0]),
            thread_id=str(row[1]),
            graph_revision=str(row[2]),
            product_lock_digest=str(row[3]),
            root_input_digest=str(row[4]),
            fencing_token=int(row[5]),
        )

    async def advance_fence(self, invocation_id: str, fencing_token: int) -> None:
        existing = await self.read_invocation_started(invocation_id)
        if existing is None:
            return
        if fencing_token < existing.fencing_token:
            raise CheckpointIntegrityError("fencing token is stale")
        if fencing_token == existing.fencing_token:
            return
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._conn.execute(
                    "UPDATE assurance_invocations SET fencing_token = ? WHERE invocation_id = ?",
                    (fencing_token, invocation_id),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise


class _JournalLinkedLease:
    def __init__(
        self,
        lease: LocalInvocationRunnerLease,
        journal: SqliteCheckpointAnchorJournal,
    ) -> None:
        self._lease = lease
        self._journal = journal
        self._recover_handshake: Callable[[str], Awaitable[InvocationStarted | None]] | None = None

    def bind_recover(self, recover_handshake: Callable[[str], Awaitable[InvocationStarted | None]]) -> None:
        self._recover_handshake = recover_handshake

    async def acquire(self, invocation_id: str, *, owner_id: str) -> RunnerLease:
        record = await self._lease.acquire(invocation_id, owner_id=owner_id)
        try:
            if self._recover_handshake is not None:
                await self._recover_handshake(invocation_id)
            await self._journal.advance_fence(invocation_id, record.fencing_token)
        except BaseException:
            await self._lease.release(record)
            raise
        return record

    async def release(self, lease: RunnerLease) -> None:
        await self._lease.release(lease)

    async def assert_current(self, invocation_id: str, fencing_token: int) -> None:
        await self._lease.assert_current(invocation_id, fencing_token)

    def current(self, invocation_id: str) -> RunnerLease:
        return self._lease.current(invocation_id)


@dataclass(slots=True)
class AssuranceSqliteBackend:
    store: SqliteCheckpointStoreTransaction
    journal: SqliteCheckpointAnchorJournal
    serializer: Any
    observers: tuple[CheckpointAnchorObserverPort, ...]
    lease: InvocationRunnerLeasePort
    _conn: Any
    _observers_sealed: bool = False

    def install_observers(self, observers: Sequence[CheckpointAnchorObserverPort]) -> None:
        if self._observers_sealed:
            raise RuntimeError("observers are sealed")
        self.observers = tuple(observers)

    def seal_observers(self) -> None:
        self._observers_sealed = True

    def checkpointer(self, identity: CheckpointAnchorState) -> AnchoredCheckpointer:
        if not self._observers_sealed:
            raise RuntimeError("observers must be sealed before creating a checkpointer")
        return AnchoredCheckpointer(
            store=self.store,
            journal=self.journal,
            identity=identity,
            observers=self.observers,
            lease=self.lease,
            serde=self.serializer,
        )

    async def full_synchronous(self) -> bool:
        cursor = await self._conn.execute("PRAGMA synchronous")
        row = await cursor.fetchone()
        return row is not None and int(row[0]) == 2

    async def remember_entrypoint(self, invocation_id: str, entrypoint: str) -> None:
        async with self.store._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._conn.execute(
                    "SELECT entrypoint FROM assurance_entrypoints WHERE invocation_id = ?",
                    (invocation_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await self._conn.execute(
                        "INSERT INTO assurance_entrypoints (invocation_id, entrypoint) VALUES (?, ?)",
                        (invocation_id, entrypoint),
                    )
                elif str(row[0]) != entrypoint:
                    raise CheckpointIntegrityError("entrypoint identity drifted")
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

    async def pin_start(
        self,
        record: InvocationStarted,
        entrypoint: str,
        *,
        fencing_token: int,
    ) -> None:
        await self.journal.start_invocation(record, fencing_token=fencing_token)
        await self.remember_entrypoint(record.invocation_id, entrypoint)

    async def read_entrypoint(self, invocation_id: str) -> str | None:
        async with self.store._lock:
            cursor = await self._conn.execute(
                "SELECT entrypoint FROM assurance_entrypoints WHERE invocation_id = ?",
                (invocation_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else str(row[0])

    async def recover_handshake(self, invocation_id: str) -> InvocationStarted | None:
        started = await self.journal.read_invocation_started(invocation_id)
        live = [
            record
            for record in await self.store.list_outbox(invocation_id)
            if record.kind == "checkpoint" and record.abandoned_at is None
        ]
        if started is None and not live:
            return None
        if started is None:
            identities = {
                (
                    record.invocation_id,
                    record.thread_id,
                    record.graph_revision,
                    record.product_lock_digest,
                    record.root_input_digest,
                    record.fencing_token,
                )
                for record in live
            }
            if len(identities) != 1:
                raise CheckpointIntegrityError("invocation identity drifted")
            record = live[0]
            started = InvocationStarted(
                invocation_id=record.invocation_id,
                thread_id=record.thread_id,
                graph_revision=record.graph_revision,
                product_lock_digest=record.product_lock_digest,
                root_input_digest=record.root_input_digest,
                fencing_token=record.fencing_token,
            )
            await self.journal.start_invocation(started, fencing_token=started.fencing_token)
        for record in live:
            if (
                record.invocation_id != started.invocation_id
                or record.thread_id != started.thread_id
                or record.graph_revision != started.graph_revision
                or record.product_lock_digest != started.product_lock_digest
                or record.root_input_digest != started.root_input_digest
            ):
                raise CheckpointIntegrityError("invocation identity drifted")
        await self._anchor_unanchored_initial(live, started)
        return started

    async def _anchor_unanchored_initial(
        self,
        live: Sequence[CheckpointOutboxRecord],
        started: InvocationStarted,
    ) -> None:
        for record in live:
            if record.kind != "checkpoint":
                continue
            existing = await self.journal.read_checkpoint_anchor(
                record.thread_id,
                record.journal_checkpoint_id,
            )
            if existing is not None:
                continue
            if record.fencing_token != started.fencing_token:
                raise CheckpointIntegrityError("initial handshake fence advanced before recover")
            anchor = CheckpointAnchor.build(
                invocation_id=record.invocation_id,
                thread_id=record.thread_id,
                checkpoint_id=record.journal_checkpoint_id,
                parent_checkpoint_id=record.parent_checkpoint_id,
                checkpoint_bytes=record.checkpoint_bytes,
                pending_write_bytes=record.pending_write_bytes,
                task_identity=record.task_identity,
                graph_revision=record.graph_revision,
                product_lock_digest=record.product_lock_digest,
                root_input_digest=record.root_input_digest,
                fencing_token=record.fencing_token,
            )
            await self.journal.append_checkpoint_anchor(anchor, fencing_token=started.fencing_token)


@asynccontextmanager
async def open_sqlite_checkpointer(
    workspace: ChangeWorkspace,
) -> AsyncIterator[AssuranceSqliteBackend]:
    paths = workspace.paths
    _prepare_control_tree(paths)
    if paths.langgraph_checkpoints.exists() and (
        paths.langgraph_checkpoints.is_symlink() or not paths.langgraph_checkpoints.is_file()
    ):
        raise ValueError("langgraph checkpoint database must be a regular file")
    async with AsyncSqliteSaver.from_conn_string(str(paths.langgraph_checkpoints)) as upstream:
        await _configure_connection(upstream.conn)
        await upstream.setup()
        await _configure_connection(upstream.conn)
        await _setup_assurance_tables(upstream.conn)
        lock = asyncio.Lock()
        store = SqliteCheckpointStoreTransaction(upstream.conn, lock)
        journal = SqliteCheckpointAnchorJournal(upstream.conn, lock)
        local_lease = LocalInvocationRunnerLease(paths.langgraph_leases)
        linked_lease = _JournalLinkedLease(local_lease, journal)
        backend = AssuranceSqliteBackend(
            store=store,
            journal=journal,
            serializer=strict_checkpoint_serializer(),
            observers=(),
            lease=linked_lease,
            _conn=upstream.conn,
        )
        linked_lease.bind_recover(backend.recover_handshake)
        yield backend


def _prepare_control_tree(paths: ChangePaths) -> None:
    for path in (paths.langgraph_root, paths.langgraph_leases, paths.langgraph_identities):
        if path.exists():
            if path.is_symlink() or not path.is_dir():
                raise ValueError("langgraph control path must be a real directory")
            continue
        path.mkdir(mode=0o700)


async def _configure_connection(conn: Any) -> None:
    await conn.execute("PRAGMA synchronous=FULL")
    await conn.commit()


async def _setup_assurance_tables(conn: Any) -> None:
    await conn.executescript(_ASSURANCE_SCHEMA)
    await conn.commit()


def _raw_checkpoint(row: tuple[Any, ...]) -> RawCheckpointRow:
    return RawCheckpointRow(
        thread_id=str(row[0]),
        checkpoint_ns=str(row[1]),
        checkpoint_id=str(row[2]),
        parent_checkpoint_id=None if row[3] is None else str(row[3]),
        checkpoint_type=str(row[4] or ""),
        checkpoint_bytes=bytes(row[5] or b""),
        metadata_type="msgpack",
        metadata_bytes=bytes(row[6] or b""),
    )


def _outbox_record(row: tuple[Any, ...]) -> CheckpointOutboxRecord:
    return CheckpointOutboxRecord(
        outbox_id=str(row[0]),
        invocation_id=str(row[1]),
        thread_id=str(row[2]),
        checkpoint_id=str(row[3]),
        parent_checkpoint_id=None if row[4] is None else str(row[4]),
        kind=row[5],
        task_identity=str(row[6]),
        graph_revision=str(row[7]),
        product_lock_digest=str(row[8]),
        root_input_digest=str(row[9]),
        fencing_token=int(row[10]),
        checkpoint_ns=str(row[11]),
        journal_checkpoint_id=str(row[12]),
        checkpoint_bytes=bytes(row[13] or b""),
        pending_write_bytes=_decode_bytes_tuple(row[14]),
        metadata_bytes=bytes(row[15] or b""),
        checkpoint_type=str(row[16]),
        metadata_type=str(row[17]),
        write_items=_decode_write_items(row[18]),
        journal_anchored_at=row[19],
        observers_delivered_at=row[20],
        abandoned_at=row[21],
    )


__all__ = [
    "SQLITE_DEPLOYMENT_MODE",
    "AssuranceSqliteBackend",
    "SqliteCheckpointAnchorJournal",
    "SqliteCheckpointStoreTransaction",
    "open_sqlite_checkpointer",
]
