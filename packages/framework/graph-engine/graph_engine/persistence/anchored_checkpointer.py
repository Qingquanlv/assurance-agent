from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol

from graph_engine.errors import GraphEngineError
from graph_engine.persistence.checkpoint_observer import (
    CheckpointAnchorNotice,
    CheckpointAnchorObserverPort,
    extract_checkpoint_markers,
    issuance_markers,
)
from graph_engine.persistence.checkpoint_store import (
    CheckpointOutboxDraft,
    CheckpointOutboxRecord,
    CheckpointStoreTransactionPort,
    RawCheckpointRow,
)
from graph_engine.persistence.journal import (
    CheckpointAnchor,
    CheckpointAnchorJournalPort,
    CheckpointAnchorState,
    CheckpointIntegrityError,
    strict_checkpoint_serializer,
)
from graph_engine.persistence.runner_lease import InvocationRunnerLeasePort
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeMarker


class AsyncOnlyCheckpointerError(GraphEngineError):
    """Raised when a caller uses a sync checkpointer method and would skip anchoring."""


_ASYNC_ONLY = "AnchoredCheckpointer is async-only; use the a-prefixed method"


class AnchoredCheckpointer(BaseCheckpointSaver[int]):
    def __init__(
        self,
        *,
        store: CheckpointStoreTransactionPort,
        journal: CheckpointAnchorJournalPort,
        identity: CheckpointAnchorState,
        observers: Sequence[CheckpointAnchorObserverPort] = (),
        lease: InvocationRunnerLeasePort | None = None,
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde or strict_checkpoint_serializer())
        self._store = store
        self._journal = journal
        self._identity = identity
        self._observers = tuple(observers)
        self._lease = lease

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        raise AsyncOnlyCheckpointerError(_ASYNC_ONLY)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise AsyncOnlyCheckpointerError(_ASYNC_ONLY)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise AsyncOnlyCheckpointerError(_ASYNC_ONLY)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        raise AsyncOnlyCheckpointerError(_ASYNC_ONLY)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del new_versions
        state = self._state_for(config)
        await self._assert_current_fence(state)
        checkpoint_type, checkpoint_bytes = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_bytes = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        draft = CheckpointOutboxDraft(
            invocation_id=state.invocation_id,
            thread_id=state.thread_id,
            checkpoint_id=str(checkpoint["id"]),
            parent_checkpoint_id=_parent_checkpoint_id(config),
            kind="checkpoint",
            task_identity="",
            graph_revision=state.graph_revision,
            product_lock_digest=state.product_lock_digest,
            root_input_digest=state.root_input_digest,
            fencing_token=state.fencing_token,
            checkpoint_ns=_checkpoint_ns(config),
            metadata_bytes=metadata_bytes,
            checkpoint_type=checkpoint_type,
            metadata_type=metadata_type,
        )
        stored_config, outbox_id = await self._store.put_checkpoint(config, checkpoint_bytes, draft)
        markers = extract_checkpoint_markers(checkpoint.get("channel_values", {}))
        await self._assert_current_fence(state)
        await self._finish_write(
            outbox_id=outbox_id,
            draft=draft,
            checkpoint_bytes=checkpoint_bytes,
            pending_write_bytes=(),
            markers=markers,
            source="checkpoint",
            fencing_token=state.fencing_token,
        )
        return stored_config

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        state = self._state_for(config)
        await self._assert_current_fence(state)
        write_items: list[tuple[str, str, str, bytes, str]] = []
        write_bytes: list[bytes] = []
        markers: list[CheckpointBridgeMarker] = []
        for channel, value in writes:
            value_type, value_payload = self.serde.dumps_typed(value)
            write_items.append((task_id, channel, value_type, value_payload, task_path))
            write_bytes.append(value_payload)
            markers.extend(extract_checkpoint_markers(value))
        draft = CheckpointOutboxDraft(
            invocation_id=state.invocation_id,
            thread_id=state.thread_id,
            checkpoint_id=str(_configurable(config)["checkpoint_id"]),
            parent_checkpoint_id=None,
            kind="pending_write",
            task_identity=f"{task_id}:{task_path}",
            graph_revision=state.graph_revision,
            product_lock_digest=state.product_lock_digest,
            root_input_digest=state.root_input_digest,
            fencing_token=state.fencing_token,
            checkpoint_ns=_checkpoint_ns(config),
            write_items=tuple(write_items),
        )
        outbox_id = await self._store.put_pending_writes(config, tuple(write_bytes), draft)
        await self._assert_current_fence(state)
        await self._finish_write(
            outbox_id=outbox_id,
            draft=draft,
            checkpoint_bytes=b"",
            pending_write_bytes=tuple(write_bytes),
            markers=issuance_markers(markers),
            source="pending_write",
            fencing_token=state.fencing_token,
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = str(_configurable(config)["thread_id"])
        namespace = _checkpoint_ns(config)
        checkpoint_id = get_checkpoint_id(config)
        if checkpoint_id:
            return await self._load_visible(thread_id, namespace, checkpoint_id)
        rows = await self._store.list_raw_checkpoints(thread_id, checkpoint_ns=namespace)
        for row in rows:
            loaded = await self._load_visible(thread_id, namespace, row.checkpoint_id)
            if loaded is not None:
                return loaded
        return None

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        thread_id = str(_configurable(config)["thread_id"])
        namespace = _checkpoint_ns(config)
        requested_id = get_checkpoint_id(config)
        before_id = get_checkpoint_id(before) if before else None
        yielded = 0
        for row in await self._store.list_raw_checkpoints(thread_id, checkpoint_ns=namespace):
            if requested_id and row.checkpoint_id != requested_id:
                continue
            if before_id and row.checkpoint_id >= before_id:
                continue
            loaded = await self._load_visible(thread_id, namespace, row.checkpoint_id)
            if loaded is None:
                continue
            if filter and not all(query == loaded.metadata.get(key) for key, query in filter.items()):
                continue
            yield loaded
            yielded += 1
            if limit is not None and yielded >= limit:
                return

    async def arecover(self, *, thread_id: str) -> None:
        for record in await self._store.scan_incomplete_outbox(thread_id):
            await self._recover_record(record)

    def _state_for(self, config: RunnableConfig) -> CheckpointAnchorState:
        configurable = _configurable(config)
        if "thread_id" not in configurable:
            raise CheckpointIntegrityError("checkpoint config is missing thread_id")
        if "assurance_fencing_token" not in configurable:
            raise CheckpointIntegrityError("checkpoint config is missing assurance_fencing_token")
        thread_id = str(configurable["thread_id"])
        if thread_id != self._identity.invocation_id:
            raise CheckpointIntegrityError("thread id must equal invocation id")
        return CheckpointAnchorState(
            invocation_id=self._identity.invocation_id,
            thread_id=thread_id,
            graph_revision=str(configurable.get("assurance_revision_id", self._identity.graph_revision)),
            product_lock_digest=str(
                configurable.get("assurance_product_lock_digest", self._identity.product_lock_digest)
            ),
            root_input_digest=str(
                configurable.get("assurance_root_input_digest", self._identity.root_input_digest)
            ),
            fencing_token=int(configurable["assurance_fencing_token"]),
        )

    async def _assert_current_fence(self, state: CheckpointAnchorState) -> None:
        await self._journal.assert_current_fence(state.invocation_id, state.fencing_token)
        if self._lease is not None:
            await self._lease.assert_current(state.invocation_id, state.fencing_token)

    async def _finish_write(
        self,
        *,
        outbox_id: str,
        draft: CheckpointOutboxDraft,
        checkpoint_bytes: bytes,
        pending_write_bytes: tuple[bytes, ...],
        markers: Sequence[CheckpointBridgeMarker],
        source: str,
        fencing_token: int,
    ) -> None:
        anchor = CheckpointAnchor.build(
            invocation_id=draft.invocation_id,
            thread_id=draft.thread_id,
            checkpoint_id=draft.journal_checkpoint_id,
            parent_checkpoint_id=draft.parent_checkpoint_id,
            checkpoint_bytes=checkpoint_bytes,
            pending_write_bytes=pending_write_bytes,
            task_identity=draft.task_identity,
            graph_revision=draft.graph_revision,
            product_lock_digest=draft.product_lock_digest,
            root_input_digest=draft.root_input_digest,
            fencing_token=draft.fencing_token,
        )
        await self._journal.append_checkpoint_anchor(anchor, fencing_token=fencing_token)
        await self._store.mark_journal_anchored(outbox_id)
        notice = CheckpointAnchorNotice(anchor=anchor, markers=tuple(markers), source=source)  # type: ignore[arg-type]
        for observer in self._observers:
            await observer.on_anchored(notice)
        await self._store.mark_observers_delivered(outbox_id)

    async def _recover_record(self, record: CheckpointOutboxRecord) -> None:
        current = await self._store.get_outbox(record.outbox_id)
        if current is None or current.abandoned_at is not None:
            return
        try:
            await self._journal.assert_current_fence(current.invocation_id, current.fencing_token)
            fence_current = True
        except CheckpointIntegrityError:
            fence_current = False
        existing = await self._journal.read_checkpoint_anchor(
            current.thread_id, current.journal_checkpoint_id
        )
        if not fence_current and existing is None:
            await self._store.mark_abandoned(current.outbox_id)
            return
        if existing is None:
            existing = CheckpointAnchor.build(
                invocation_id=current.invocation_id,
                thread_id=current.thread_id,
                checkpoint_id=current.journal_checkpoint_id,
                parent_checkpoint_id=current.parent_checkpoint_id,
                checkpoint_bytes=current.checkpoint_bytes,
                pending_write_bytes=current.pending_write_bytes,
                task_identity=current.task_identity,
                graph_revision=current.graph_revision,
                product_lock_digest=current.product_lock_digest,
                root_input_digest=current.root_input_digest,
                fencing_token=current.fencing_token,
            )
            await self._journal.append_checkpoint_anchor(existing, fencing_token=self._identity.fencing_token)
        if current.journal_anchored_at is None:
            await self._store.mark_journal_anchored(current.outbox_id)
            current = await self._require_outbox(current.outbox_id)
        if current.observers_delivered_at is None:
            notice = CheckpointAnchorNotice(
                anchor=existing,
                markers=self._markers_for_record(current),
                source="pending_write" if current.kind == "pending_write" else "checkpoint",
            )
            for observer in self._observers:
                await observer.on_anchored(notice)
            await self._store.mark_observers_delivered(current.outbox_id)

    async def _load_visible(
        self,
        thread_id: str,
        namespace: str,
        checkpoint_id: str,
    ) -> CheckpointTuple | None:
        records = [
            record
            for record in await self._store.list_outbox(thread_id)
            if record.checkpoint_id == checkpoint_id and record.kind == "checkpoint"
        ]
        if not records:
            return None
        live = [record for record in records if record.abandoned_at is None]
        if not live:
            return None
        record = next(
            (
                item
                for item in live
                if item.journal_anchored_at is not None and item.observers_delivered_at is not None
            ),
            live[0],
        )
        if record.abandoned_at is not None:
            return None
        handshake_complete = (
            record.journal_anchored_at is not None and record.observers_delivered_at is not None
        )
        if not handshake_complete:
            return None
        row = await self._store.read_raw_checkpoint(thread_id, checkpoint_id, checkpoint_ns=namespace)
        anchor = await self._journal.read_checkpoint_anchor(thread_id, record.journal_checkpoint_id)
        checked = self._assert_integrity(record, row, anchor)
        return await self._to_tuple(thread_id, namespace, checked)

    def _assert_integrity(
        self,
        record: CheckpointOutboxRecord,
        row: RawCheckpointRow | None,
        anchor: CheckpointAnchor | None,
    ) -> RawCheckpointRow:
        if anchor is None:
            raise CheckpointIntegrityError("journal anchor is missing")
        if row is None or not row.checkpoint_bytes:
            raise CheckpointIntegrityError("checkpoint bytes are missing")
        if row.checkpoint_bytes != anchor.checkpoint_bytes:
            raise CheckpointIntegrityError("checkpoint digest drifted")
        if row.parent_checkpoint_id != anchor.parent_checkpoint_id:
            raise CheckpointIntegrityError("checkpoint parent drifted")
        if record.graph_revision != anchor.graph_revision:
            raise CheckpointIntegrityError("checkpoint revision drifted")
        if record.root_input_digest != anchor.root_input_digest:
            raise CheckpointIntegrityError("checkpoint root input drifted")
        if record.fencing_token != anchor.fencing_token:
            raise CheckpointIntegrityError("fencing token is stale")
        return row

    async def _to_tuple(
        self,
        thread_id: str,
        namespace: str,
        row: RawCheckpointRow,
    ) -> CheckpointTuple:
        checkpoint: Checkpoint = self.serde.loads_typed((row.checkpoint_type, row.checkpoint_bytes))
        metadata: CheckpointMetadata = (
            self.serde.loads_typed((row.metadata_type, row.metadata_bytes)) if row.metadata_bytes else {}
        )
        pending_writes = []
        for write in await self._store.read_raw_pending_writes(
            thread_id, row.checkpoint_id, checkpoint_ns=namespace
        ):
            write_record = next(
                (
                    item
                    for item in await self._store.list_outbox(thread_id)
                    if item.kind == "pending_write"
                    and item.checkpoint_id == row.checkpoint_id
                    and item.task_identity == f"{write.task_id}:{write.task_path}"
                    and item.abandoned_at is None
                    and item.journal_anchored_at is not None
                    and item.observers_delivered_at is not None
                ),
                None,
            )
            if write_record is None:
                continue
            pending_writes.append(
                (write.task_id, write.channel, self.serde.loads_typed((write.value_type, write.value_bytes)))
            )
        parent_config: RunnableConfig | None = (
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": namespace,
                    "checkpoint_id": row.parent_checkpoint_id,
                }
            }
            if row.parent_checkpoint_id
            else None
        )
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": namespace,
                    "checkpoint_id": row.checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    def _markers_for_record(self, record: CheckpointOutboxRecord) -> tuple[CheckpointBridgeMarker, ...]:
        if record.kind == "checkpoint":
            try:
                checkpoint = self.serde.loads_typed((record.checkpoint_type, record.checkpoint_bytes))
            except (TypeError, ValueError):
                return ()
            if isinstance(checkpoint, Mapping):
                return extract_checkpoint_markers(checkpoint.get("channel_values", {}))
            return ()
        markers: list[CheckpointBridgeMarker] = []
        for _task_id, _channel, value_type, value_bytes, _task_path in record.write_items:
            try:
                value = self.serde.loads_typed((value_type, value_bytes))
            except (TypeError, ValueError):
                continue
            markers.extend(issuance_markers(extract_checkpoint_markers(value)))
        return tuple(markers)

    async def _require_outbox(self, outbox_id: str) -> CheckpointOutboxRecord:
        record = await self._store.get_outbox(outbox_id)
        if record is None:
            raise CheckpointIntegrityError("outbox record is missing")
        return record


def _configurable(config: RunnableConfig) -> Mapping[str, Any]:
    configurable = config.get("configurable")
    if configurable is None:
        raise CheckpointIntegrityError("checkpoint config is missing configurable")
    return configurable


def _checkpoint_ns(config: RunnableConfig) -> str:
    configurable = config.get("configurable") or {}
    return str(configurable.get("checkpoint_ns", ""))


def _parent_checkpoint_id(config: RunnableConfig) -> str | None:
    parent = (config.get("configurable") or {}).get("checkpoint_id")
    return str(parent) if parent else None


__all__ = [
    "AnchoredCheckpointer",
    "AsyncOnlyCheckpointerError",
]
