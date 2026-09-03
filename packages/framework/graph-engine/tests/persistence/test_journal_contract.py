from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import inspect

import pytest

from graph_engine.canonical import canonical_digest
from graph_engine.persistence.journal import (
    CheckpointAnchor,
    CheckpointAnchorJournalPort,
    CheckpointAnchorState,
    CheckpointIntegrityError,
    InvocationStarted,
)


def _anchor(**overrides: object) -> CheckpointAnchor:
    values: dict[str, object] = {
        "invocation_id": "inv-1",
        "thread_id": "inv-1",
        "checkpoint_id": "cp-2",
        "parent_checkpoint_id": "cp-1",
        "checkpoint_bytes": b"checkpoint",
        "pending_write_bytes": (b"write-a",),
        "task_identity": "task-1",
        "graph_revision": "a" * 64,
        "product_lock_digest": "b" * 64,
        "root_input_digest": "c" * 64,
        "fencing_token": 4,
    }
    values.update(overrides)
    return CheckpointAnchor.build(**values)  # type: ignore[arg-type]


def _started(**overrides: object) -> InvocationStarted:
    values: dict[str, object] = {
        "invocation_id": "inv-1",
        "thread_id": "inv-1",
        "graph_revision": "a" * 64,
        "product_lock_digest": "b" * 64,
        "root_input_digest": "c" * 64,
        "fencing_token": 4,
    }
    values.update(overrides)
    return InvocationStarted(**values)  # type: ignore[arg-type]


def test_checkpoint_anchor_digest_covers_bytes_lineage_revision_and_fence() -> None:
    anchor = CheckpointAnchor.build(
        invocation_id="inv-1",
        thread_id="inv-1",
        checkpoint_id="cp-2",
        parent_checkpoint_id="cp-1",
        checkpoint_bytes=b"checkpoint",
        pending_write_bytes=(b"write-a",),
        task_identity="task-1",
        graph_revision="a" * 64,
        product_lock_digest="b" * 64,
        root_input_digest="c" * 64,
        fencing_token=4,
    )
    assert len(anchor.anchor_digest) == 64
    assert replace(anchor, fencing_token=5).canonical_digest() != anchor.anchor_digest


def test_anchor_digest_is_the_canonical_projection_of_bytes_lineage_revision_and_fence() -> None:
    anchor = _anchor()
    assert anchor.anchor_digest == anchor.canonical_digest()
    assert anchor.anchor_digest == canonical_digest(
        {
            "checkpoint_bytes_digest": hashlib.sha256(b"checkpoint").hexdigest(),
            "checkpoint_id": "cp-2",
            "fencing_token": 4,
            "graph_revision": "a" * 64,
            "invocation_id": "inv-1",
            "parent_checkpoint_id": "cp-1",
            "pending_write_bytes_digests": [hashlib.sha256(b"write-a").hexdigest()],
            "product_lock_digest": "b" * 64,
            "root_input_digest": "c" * 64,
            "task_identity": "task-1",
            "thread_id": "inv-1",
        }
    )
    assert _anchor(checkpoint_bytes=b"other").anchor_digest != anchor.anchor_digest
    assert _anchor(parent_checkpoint_id="cp-0").anchor_digest != anchor.anchor_digest
    assert _anchor(graph_revision="d" * 64).anchor_digest != anchor.anchor_digest
    assert _anchor(root_input_digest="e" * 64).anchor_digest != anchor.anchor_digest


def test_journal_port_exposes_idempotent_append_read_and_cas_without_workflow_api() -> None:
    methods = {
        name
        for name, value in inspect.getmembers(CheckpointAnchorJournalPort)
        if callable(value) and not name.startswith("_")
    }
    assert methods == {
        "start_invocation",
        "append_checkpoint_anchor",
        "read_checkpoint_anchor",
        "assert_current_fence",
    }


def test_identical_journal_records_are_idempotent_and_divergent_identity_fails_closed() -> None:
    asyncio.run(_exercise_journal_idempotency())


async def _exercise_journal_idempotency() -> None:
    journal = _MemoryCheckpointAnchorJournal()
    started = _started()
    anchor = _anchor()

    await journal.start_invocation(started, fencing_token=4)
    await journal.start_invocation(started, fencing_token=4)
    await journal.append_checkpoint_anchor(anchor, fencing_token=4)
    await journal.append_checkpoint_anchor(anchor, fencing_token=4)
    loaded = await journal.read_checkpoint_anchor("inv-1", "cp-2")
    assert loaded == anchor
    await journal.assert_current_fence("inv-1", 4)

    with pytest.raises(CheckpointIntegrityError):
        await journal.append_checkpoint_anchor(
            _anchor(checkpoint_bytes=b"drift"),
            fencing_token=4,
        )
    with pytest.raises(CheckpointIntegrityError):
        await journal.append_checkpoint_anchor(
            _anchor(parent_checkpoint_id="cp-other"),
            fencing_token=4,
        )
    with pytest.raises(CheckpointIntegrityError):
        await journal.append_checkpoint_anchor(
            _anchor(graph_revision="f" * 64),
            fencing_token=4,
        )
    with pytest.raises(CheckpointIntegrityError):
        await journal.append_checkpoint_anchor(
            _anchor(root_input_digest="f" * 64),
            fencing_token=4,
        )
    with pytest.raises(CheckpointIntegrityError):
        await journal.append_checkpoint_anchor(anchor, fencing_token=5)
    with pytest.raises(CheckpointIntegrityError):
        await journal.start_invocation(_started(root_input_digest="f" * 64), fencing_token=4)
    with pytest.raises(CheckpointIntegrityError):
        await journal.assert_current_fence("inv-1", 5)


def test_invocation_started_projects_checkpoint_anchor_state() -> None:
    started = _started()
    state = started.anchor_state()
    assert isinstance(state, CheckpointAnchorState)
    assert state == CheckpointAnchorState(
        invocation_id="inv-1",
        thread_id="inv-1",
        graph_revision="a" * 64,
        product_lock_digest="b" * 64,
        root_input_digest="c" * 64,
        fencing_token=4,
    )
    assert _anchor().anchor_state() == state


class _MemoryCheckpointAnchorJournal:
    def __init__(self) -> None:
        self._started: dict[str, InvocationStarted] = {}
        self._anchors: dict[tuple[str, str], CheckpointAnchor] = {}

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
        started = self._started.get(anchor.invocation_id)
        if started is None:
            raise CheckpointIntegrityError("invocation has not started")
        if anchor.anchor_state() != started.anchor_state():
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
        started = self._started.get(invocation_id)
        if started is None:
            if fencing_token < 1:
                raise CheckpointIntegrityError("fencing token is stale")
            return
        if started.fencing_token != fencing_token:
            raise CheckpointIntegrityError("fencing token is stale")
