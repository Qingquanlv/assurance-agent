from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import TypedDict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from graph_engine.persistence.anchored_checkpointer import (
    AnchoredCheckpointer,
    AsyncOnlyCheckpointerError,
)
from graph_engine.persistence.checkpoint_store import CheckpointOutboxDraft, MemoryCheckpointStore
from graph_engine.persistence.journal import CheckpointAnchorState, CheckpointIntegrityError
from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease, StaleFencingToken

_HELPERS_PATH = Path(__file__).with_name("test_checkpoint_store_contract.py")
_HELPERS_SPEC = importlib.util.spec_from_file_location("checkpoint_store_contract_helpers", _HELPERS_PATH)
assert _HELPERS_SPEC is not None and _HELPERS_SPEC.loader is not None
_helpers = importlib.util.module_from_spec(_HELPERS_SPEC)
_HELPERS_SPEC.loader.exec_module(_helpers)

FENCE = _helpers.FENCE
INPUT = _helpers.INPUT
LOCK = _helpers.LOCK
REVISION = _helpers.REVISION
MemoryCheckpointAnchorJournal = _helpers.MemoryCheckpointAnchorJournal
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


@pytest.fixture
def journal() -> MemoryCheckpointAnchorJournal:
    return MemoryCheckpointAnchorJournal()


@pytest.fixture
def store() -> MemoryCheckpointStore:
    return MemoryCheckpointStore()


@pytest.fixture
def observer() -> RecordingObserver:
    return RecordingObserver()


@pytest.fixture
def saver(
    store: MemoryCheckpointStore,
    journal: MemoryCheckpointAnchorJournal,
    observer: RecordingObserver,
) -> AnchoredCheckpointer:
    asyncio.run(journal.start_invocation(started(), fencing_token=FENCE))
    return AnchoredCheckpointer(
        store=store,
        journal=journal,
        observers=(observer,),
        identity=identity(),
    )


async def test_put_is_visible_only_after_matching_journal_anchor(saver, journal) -> None:
    stored_config = await saver.aput(config, checkpoint, metadata, new_versions)
    checkpoint_id = stored_config["configurable"]["checkpoint_id"]
    assert await journal.read_checkpoint_anchor("inv-1", checkpoint_id) is not None
    loaded = await saver.aget_tuple(stored_config)
    assert loaded is not None
    assert loaded.checkpoint == checkpoint


async def test_put_writes_anchors_pending_task_identity(saver, journal) -> None:
    await saver.aput_writes(
        config,
        [("result", {"ok": True})],
        task_id="task-a",
        task_path="push-0",
    )
    anchor = journal.only_pending_write_anchor()
    assert anchor.task_identity == "task-a:push-0"


def test_sync_methods_fail_explicitly_and_do_not_bypass_anchoring(
    store: MemoryCheckpointStore,
    journal: MemoryCheckpointAnchorJournal,
) -> None:
    saver = AnchoredCheckpointer(
        store=store,
        journal=journal,
        observers=(),
        identity=identity(),
    )
    with pytest.raises(AsyncOnlyCheckpointerError, match="async-only"):
        saver.put(run_config(), sample_checkpoint(), sample_metadata(), {"result": 1})
    with pytest.raises(AsyncOnlyCheckpointerError, match="async-only"):
        saver.put_writes(run_config(), [("result", {"ok": True})], "task-a", "push-0")
    with pytest.raises(AsyncOnlyCheckpointerError, match="async-only"):
        saver.get_tuple(run_config())
    with pytest.raises(AsyncOnlyCheckpointerError, match="async-only"):
        list(saver.list(run_config()))


async def test_unanchored_store_row_is_invisible_until_handshake_closes(
    store: MemoryCheckpointStore,
    journal: MemoryCheckpointAnchorJournal,
) -> None:
    await journal.start_invocation(started(), fencing_token=FENCE)
    await store.put_checkpoint(
        run_config(),
        b"not-yet-visible",
        CheckpointOutboxDraft(
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
        ),
    )
    saver = AnchoredCheckpointer(
        store=store,
        journal=journal,
        observers=(),
        identity=identity(),
    )
    assert await saver.aget_tuple(run_config(checkpoint_id="cp-2")) is None
    listed = [item async for item in saver.alist(run_config(checkpoint_id="cp-2"))]
    assert listed == []


class BranchState(TypedDict):
    left: int
    right: int


async def test_sibling_pending_write_is_anchored_and_not_rerun_after_resume(
    store: MemoryCheckpointStore,
    journal: MemoryCheckpointAnchorJournal,
) -> None:
    await journal.start_invocation(started(), fencing_token=FENCE)
    saver = AnchoredCheckpointer(
        store=store,
        journal=journal,
        observers=(),
        identity=identity(),
    )
    runs = {"left": 0, "right": 0}

    def left_node(state: BranchState) -> dict[str, int]:
        runs["left"] += 1
        interrupt("wait")
        return {"left": 1}

    def right_node(state: BranchState) -> dict[str, int]:
        runs["right"] += 1
        return {"right": 1}

    builder = StateGraph(BranchState)
    builder.add_node("left", left_node)
    builder.add_node("right", right_node)
    builder.add_edge(START, "left")
    builder.add_edge(START, "right")
    builder.add_edge("left", END)
    builder.add_edge("right", END)
    graph = builder.compile(checkpointer=saver)
    invoke_config: RunnableConfig = {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": REVISION,
            "assurance_product_lock_digest": LOCK,
            "assurance_root_input_digest": INPUT,
            "assurance_fencing_token": FENCE,
        }
    }

    interrupted = await graph.ainvoke({"left": 0, "right": 0}, invoke_config)
    assert interrupted["__interrupt__"]
    assert runs == {"left": 1, "right": 1}
    pending_anchors = journal.pending_write_anchors()
    assert any(anchor.pending_write_bytes for anchor in pending_anchors)
    snapshot = await saver.aget_tuple(invoke_config)
    assert snapshot is not None
    assert snapshot.checkpoint["channel_values"]["right"] == 0
    assert any(channel == "right" and value == 1 for _, channel, value in (snapshot.pending_writes or []))

    resumed = await graph.ainvoke(Command(resume=True), invoke_config)
    assert resumed["left"] == 1
    assert resumed["right"] == 1
    assert runs == {"left": 2, "right": 1}


def test_identity_requires_thread_id_equal_to_invocation_id() -> None:
    state = CheckpointAnchorState(
        invocation_id="inv-1",
        thread_id="inv-1",
        graph_revision=REVISION,
        product_lock_digest=LOCK,
        root_input_digest=INPUT,
        fencing_token=FENCE,
    )
    assert state.thread_id == state.invocation_id


class RecordingStore:
    def __init__(self, inner: MemoryCheckpointStore) -> None:
        self.inner = inner
        self.mutations = 0

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    async def put_checkpoint(self, *args, **kwargs):
        self.mutations += 1
        return await self.inner.put_checkpoint(*args, **kwargs)

    async def put_pending_writes(self, *args, **kwargs):
        self.mutations += 1
        return await self.inner.put_pending_writes(*args, **kwargs)


def _lease_config(*, fencing_token: int, checkpoint_id: str = "cp-1") -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
            "assurance_revision_id": REVISION,
            "assurance_product_lock_digest": LOCK,
            "assurance_root_input_digest": INPUT,
            "assurance_fencing_token": fencing_token,
        }
    }


async def test_saver_write_requires_thread_id_and_assurance_fencing_token(
    saver: AnchoredCheckpointer,
) -> None:
    missing_thread = {
        "configurable": {
            "assurance_revision_id": REVISION,
            "assurance_product_lock_digest": LOCK,
            "assurance_root_input_digest": INPUT,
            "assurance_fencing_token": FENCE,
        }
    }
    missing_token = {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": REVISION,
            "assurance_product_lock_digest": LOCK,
            "assurance_root_input_digest": INPUT,
        }
    }
    with pytest.raises(CheckpointIntegrityError, match="thread_id"):
        await saver.aput(missing_thread, checkpoint, metadata, new_versions)
    with pytest.raises(CheckpointIntegrityError, match="assurance_fencing_token"):
        await saver.aput(missing_token, checkpoint, metadata, new_versions)
    with pytest.raises(CheckpointIntegrityError, match="thread_id"):
        await saver.aput_writes(missing_thread, [("result", {"ok": True})], "task-a", "push-0")
    with pytest.raises(CheckpointIntegrityError, match="assurance_fencing_token"):
        await saver.aput_writes(missing_token, [("result", {"ok": True})], "task-a", "push-0")


async def test_reclaimed_predecessor_cannot_checkpoint(tmp_path: Path) -> None:
    first_process = LocalInvocationRunnerLease(tmp_path)
    first = await first_process.acquire("inv-1", owner_id="runner-a")
    first_process.simulate_process_exit_for_test(first)

    replacement_process = LocalInvocationRunnerLease(tmp_path)
    second = await replacement_process.acquire("inv-1", owner_id="runner-b")
    store = RecordingStore(MemoryCheckpointStore())
    journal = MemoryCheckpointAnchorJournal()
    await journal.start_invocation(
        started(fencing_token=first.fencing_token), fencing_token=first.fencing_token
    )
    saver = AnchoredCheckpointer(
        store=store,
        journal=journal,
        observers=(),
        identity=identity(fencing_token=second.fencing_token),
        lease=replacement_process,
    )
    try:
        with pytest.raises(StaleFencingToken):
            await saver.aput(
                _lease_config(fencing_token=first.fencing_token),
                sample_checkpoint(),
                sample_metadata(),
                {"result": 1},
            )
        assert store.mutations == 0
        with pytest.raises(StaleFencingToken):
            await saver.aput_writes(
                _lease_config(fencing_token=first.fencing_token),
                [("result", {"ok": True})],
                "task-a",
                "push-0",
            )
        assert store.mutations == 0
    finally:
        await replacement_process.release(second)
