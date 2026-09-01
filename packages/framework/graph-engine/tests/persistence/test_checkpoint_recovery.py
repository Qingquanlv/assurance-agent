from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer
from graph_engine.persistence.checkpoint_observer import CheckpointAnchorNotice
from graph_engine.persistence.journal import CheckpointIntegrityError
from graph_engine.stategraph.checkpoint_bridge import (
    CHECKPOINT_MARKERS_STATE_KEY,
    CheckpointBridgeMarker,
)

_HELPERS_PATH = Path(__file__).with_name("test_checkpoint_store_contract.py")
_HELPERS_SPEC = importlib.util.spec_from_file_location(
    "checkpoint_store_contract_helpers_recovery", _HELPERS_PATH
)
assert _HELPERS_SPEC is not None and _HELPERS_SPEC.loader is not None
_helpers = importlib.util.module_from_spec(_HELPERS_SPEC)
_HELPERS_SPEC.loader.exec_module(_helpers)

FENCE = _helpers.FENCE
MemoryCheckpointAnchorJournal = _helpers.MemoryCheckpointAnchorJournal
MemoryCheckpointStore = _helpers.MemoryCheckpointStore
RecordingObserver = _helpers.RecordingObserver
identity = _helpers.identity
run_config = _helpers.run_config
sample_checkpoint = _helpers.sample_checkpoint
sample_metadata = _helpers.sample_metadata
started = _helpers.started

config = run_config()
checkpoint = sample_checkpoint()
metadata = sample_metadata()
new_versions = {"result": 1}


class InjectedCrash(Exception):
    pass


class FaultingStore:
    def __init__(self, inner: Any, fault: str | None) -> None:
        self._inner = inner
        self.fault = fault

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def put_checkpoint(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.put_checkpoint(*args, **kwargs)
        if self.fault == "after_store_outbox":
            raise InjectedCrash(self.fault)
        return result

    async def put_pending_writes(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.put_pending_writes(*args, **kwargs)
        if self.fault == "after_store_outbox":
            raise InjectedCrash(self.fault)
        return result

    async def mark_journal_anchored(self, outbox_id: str) -> None:
        await self._inner.mark_journal_anchored(outbox_id)
        if self.fault == "after_mark_journal_anchored":
            raise InjectedCrash(self.fault)

    async def mark_observers_delivered(self, outbox_id: str) -> None:
        if self.fault == "before_mark_observers_delivered":
            raise InjectedCrash(self.fault)
        await self._inner.mark_observers_delivered(outbox_id)


class FaultingJournal:
    def __init__(self, inner: Any, fault: str | None) -> None:
        self._inner = inner
        self.fault = fault

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def append_checkpoint_anchor(self, *args: Any, **kwargs: Any) -> None:
        await self._inner.append_checkpoint_anchor(*args, **kwargs)
        if self.fault == "after_journal_append":
            raise InjectedCrash(self.fault)


class FaultingObserver:
    def __init__(self, inner: Any, fault: str | None) -> None:
        self._inner = inner
        self.fault = fault

    async def on_anchored(self, notice: CheckpointAnchorNotice) -> None:
        await self._inner.on_anchored(notice)
        if self.fault == "after_observer_delivery":
            raise InjectedCrash(self.fault)


def _marker(
    *,
    kind: str,
    generation: int = 1,
    ordinal: int = 0,
) -> CheckpointBridgeMarker:
    return CheckpointBridgeMarker(
        kind=kind,  # type: ignore[arg-type]
        attempt_key="d" * 64,
        generation=generation,
        ordinal=ordinal,
        envelope_digest="e" * 64,
    )


async def _started_journal() -> Any:
    journal = MemoryCheckpointAnchorJournal()
    await journal.start_invocation(started(), fencing_token=FENCE)
    return journal


async def _crashing_saver(
    *,
    fault: str,
    store: Any | None = None,
    journal: Any | None = None,
    observer: Any | None = None,
    fencing_token: int = FENCE,
) -> tuple[AnchoredCheckpointer, Any, Any, Any]:
    store = store or MemoryCheckpointStore()
    journal = journal or await _started_journal()
    observer = observer or RecordingObserver()
    saver = AnchoredCheckpointer(
        store=FaultingStore(store, fault),
        journal=FaultingJournal(journal, fault),
        observers=(FaultingObserver(observer, fault),),
        identity=identity(fencing_token=fencing_token),
    )
    return saver, store, journal, observer


def _successor(
    store: Any,
    journal: Any,
    observer: Any,
    *,
    fencing_token: int = FENCE,
) -> AnchoredCheckpointer:
    return AnchoredCheckpointer(
        store=store,
        journal=journal,
        observers=(observer,),
        identity=identity(fencing_token=fencing_token),
    )


@pytest.mark.parametrize(
    "fault",
    [
        "after_store_outbox",
        "after_journal_append",
        "after_mark_journal_anchored",
        "after_observer_delivery",
        "before_mark_observers_delivered",
    ],
)
async def test_arecover_completes_the_missing_idempotent_handshake_step(fault: str) -> None:
    crashing, store, journal, observer = await _crashing_saver(fault=fault)
    with pytest.raises(InjectedCrash):
        await crashing.aput(config, checkpoint, metadata, new_versions)
    assert await crashing.aget_tuple(run_config(checkpoint_id="cp-2")) is None

    saver = _successor(store, journal, observer)
    await saver.arecover(thread_id="inv-1")
    loaded = await saver.aget_tuple(run_config(checkpoint_id="cp-2"))
    assert loaded is not None
    assert loaded.checkpoint == checkpoint
    assert await journal.read_checkpoint_anchor("inv-1", "cp-2") is not None
    assert observer.notices
    incomplete = await store.scan_incomplete_outbox("inv-1")
    assert incomplete == ()


@pytest.mark.parametrize(
    "corrupt",
    ["missing_bytes", "digest_drift", "wrong_parent", "wrong_revision", "wrong_root_input", "stale_fence"],
)
async def test_integrity_mismatch_on_read_raises_checkpoint_integrity_error(corrupt: str) -> None:
    store = MemoryCheckpointStore()
    journal = await _started_journal()
    saver = _successor(store, journal, RecordingObserver())
    stored = await saver.aput(config, checkpoint, metadata, new_versions)
    checkpoint_id = stored["configurable"]["checkpoint_id"]
    if corrupt == "missing_bytes":
        store.drop_checkpoint_bytes("inv-1", checkpoint_id)
    elif corrupt == "digest_drift":
        store.replace_checkpoint_bytes("inv-1", checkpoint_id, b"drifted-bytes")
    elif corrupt == "wrong_parent":
        store.replace_parent_checkpoint_id("inv-1", checkpoint_id, "cp-other")
    elif corrupt == "wrong_revision":
        store.replace_outbox_identity("inv-1", checkpoint_id, graph_revision="f" * 64)
    elif corrupt == "wrong_root_input":
        store.replace_outbox_identity("inv-1", checkpoint_id, root_input_digest="f" * 64)
    else:
        store.replace_outbox_identity("inv-1", checkpoint_id, fencing_token=5)

    with pytest.raises(CheckpointIntegrityError):
        await saver.aget_tuple(stored)
    with pytest.raises(CheckpointIntegrityError):
        [item async for item in saver.alist(stored)]


async def test_stale_fence_is_rejected_before_journal_and_successor_abandons() -> None:
    store = MemoryCheckpointStore()
    journal = await _started_journal()
    observer = RecordingObserver()
    first = _successor(store, journal, observer)
    first_checkpoint = sample_checkpoint(checkpoint_id="cp-1")
    first_config: RunnableConfig = {
        "configurable": {
            "thread_id": "inv-1",
            "checkpoint_ns": "",
            "assurance_revision_id": _helpers.REVISION,
            "assurance_product_lock_digest": _helpers.LOCK,
            "assurance_root_input_digest": _helpers.INPUT,
            "assurance_fencing_token": FENCE,
        }
    }
    first_stored = await first.aput(first_config, first_checkpoint, metadata, {"result": 1})
    first_id = first_stored["configurable"]["checkpoint_id"]

    crashing, store, journal, observer = await _crashing_saver(
        fault="after_store_outbox",
        store=store,
        journal=journal,
        observer=observer,
    )
    with pytest.raises(InjectedCrash):
        await crashing.aput(config, checkpoint, metadata, new_versions)

    journal.advance_fence("inv-1", 5)
    with pytest.raises(CheckpointIntegrityError, match="stale"):
        await journal.assert_current_fence("inv-1", FENCE)

    successor = _successor(store, journal, observer, fencing_token=5)
    await successor.arecover(thread_id="inv-1")

    visible = await successor.aget_tuple(run_config(checkpoint_id=first_id))
    assert visible is not None
    assert visible.checkpoint["id"] == first_id
    assert await successor.aget_tuple(run_config(checkpoint_id="cp-2")) is None
    stale_rows = [record for record in await store.list_outbox("inv-1") if record.checkpoint_id == "cp-2"]
    assert len(stale_rows) == 1
    assert stale_rows[0].abandoned_at is not None
    assert stale_rows[0].fencing_token == FENCE
    assert await journal.read_checkpoint_anchor("inv-1", "cp-2") is None


async def test_successor_completes_observers_when_stale_row_already_has_journal_anchor() -> None:
    store = MemoryCheckpointStore()
    journal = await _started_journal()
    observer = RecordingObserver()
    crashing, store, journal, observer = await _crashing_saver(
        fault="after_journal_append",
        store=store,
        journal=journal,
        observer=observer,
    )
    with pytest.raises(InjectedCrash):
        await crashing.aput(config, checkpoint, metadata, new_versions)
    assert await journal.read_checkpoint_anchor("inv-1", "cp-2") is not None

    journal.advance_fence("inv-1", 5)
    successor = _successor(store, journal, observer, fencing_token=5)
    await successor.arecover(thread_id="inv-1")
    loaded = await successor.aget_tuple(run_config(checkpoint_id="cp-2"))
    assert loaded is not None
    assert loaded.checkpoint == checkpoint
    assert observer.notices
    stale_rows = [record for record in await store.list_outbox("inv-1") if record.checkpoint_id == "cp-2"]
    assert len(stale_rows) == 1
    assert stale_rows[0].abandoned_at is None
    assert stale_rows[0].observers_delivered_at is not None
    assert stale_rows[0].fencing_token == FENCE


async def test_completion_marker_in_pending_write_is_not_an_observer_notice() -> None:
    store = MemoryCheckpointStore()
    journal = await _started_journal()
    observer = RecordingObserver()
    issued = _marker(kind="system_interrupt_issued", generation=1, ordinal=0)
    completed = _marker(kind="system_interrupt_completed", generation=1, ordinal=0)
    saver = _successor(store, journal, observer)
    await saver.aput_writes(
        config,
        [("__interrupt__", {CHECKPOINT_MARKERS_STATE_KEY: [issued.model_dump(mode="json")]})],
        task_id="task-issue",
        task_path="push-0",
    )
    crashing, store, journal, observer = await _crashing_saver(
        fault="after_store_outbox",
        store=store,
        journal=journal,
        observer=observer,
    )
    with pytest.raises(InjectedCrash):
        await crashing.aput_writes(
            config,
            [("result", {CHECKPOINT_MARKERS_STATE_KEY: [completed.model_dump(mode="json")], "ok": True})],
            task_id="task-complete",
            task_path="push-1",
        )

    saver = _successor(store, journal, observer)
    await saver.arecover(thread_id="inv-1")
    completion_notices = [
        notice
        for notice in observer.notices
        if any(marker.kind == "system_interrupt_completed" for marker in notice.markers)
    ]
    assert completion_notices == []
    issuance_notices = [
        notice
        for notice in observer.notices
        if any(marker.kind == "system_interrupt_issued" for marker in notice.markers)
    ]
    assert len(issuance_notices) == 1
    assert issuance_notices[0].markers[0].ordinal == 0

    merged = sample_checkpoint(checkpoint_id="cp-3")
    merged["channel_values"] = {
        "result": {"ok": True},
        CHECKPOINT_MARKERS_STATE_KEY: [completed.model_dump(mode="json")],
    }
    merged_config = run_config(checkpoint_id="cp-2")
    await saver.aput(merged_config, merged, metadata, {"result": 2})
    completion_notices = [
        notice
        for notice in observer.notices
        if any(marker.kind == "system_interrupt_completed" for marker in notice.markers)
    ]
    assert len(completion_notices) == 1
    assert [marker.kind for marker in completion_notices[0].markers] == ["system_interrupt_completed"]
    assert completion_notices[0].source == "checkpoint"
