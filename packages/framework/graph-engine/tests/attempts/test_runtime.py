from __future__ import annotations

from dataclasses import replace

import pytest
from graph_engine.attempts.checkpoint import AttemptCheckpoint, AttemptPhase
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.resolutions import PendingTaskResult, SystemReference
from graph_engine.attempts.runtime import AttemptRuntime, DurableProgress, ReturnResolution
from graph_engine.persistence.attempt_checkpoint import MemoryAttemptCheckpointStore


class Handlers:
    def __init__(self, *, advance: bool, wait: bool) -> None:
        self.checkpoints = MemoryAttemptCheckpointStore()
        self.advance = advance
        self.wait = wait
        self.calls: list[AttemptPhase] = []
        self.phases = {AttemptPhase.AUTHORIZE: self.authorize, AttemptPhase.EXECUTE: self.execute}
        self.pending = PendingTaskResult(wakeup=SystemReference(reference_id="wait"))

    async def open_or_restore(self) -> AttemptCheckpoint:
        key = AttemptKey(digest="a" * 64)
        saved = await self.checkpoints.load(key)
        if saved is not None:
            return saved
        return await self.checkpoints.commit(
            AttemptCheckpoint(
                key, 0, 1, AttemptPhase.AUTHORIZE, "b" * 64, "c" * 64, "d" * 64, "inv-1", "execute", "node"
            ),
            expected_revision=0,
            fencing_token=1,
        )

    async def authorize(self, checkpoint: AttemptCheckpoint):
        self.calls.append(checkpoint.phase)
        if self.wait:
            return ReturnResolution(self.pending)
        if self.advance:
            await self.checkpoints.commit(
                replace(checkpoint, phase=AttemptPhase.EXECUTE, authorization_id="e" * 64),
                expected_revision=checkpoint.revision,
                fencing_token=1,
            )
        return DurableProgress()

    async def execute(self, checkpoint: AttemptCheckpoint):
        self.calls.append(checkpoint.phase)
        return ReturnResolution(self.pending)


@pytest.mark.asyncio
async def test_runtime_dispatches_saved_phase_after_reconstruction() -> None:
    first = Handlers(advance=True, wait=False)
    assert await AttemptRuntime(first).run() is first.pending
    assert first.calls == [AttemptPhase.AUTHORIZE, AttemptPhase.EXECUTE]
    restarted = Handlers(advance=False, wait=False)
    restarted.checkpoints = first.checkpoints
    assert await AttemptRuntime(restarted).run() is restarted.pending
    assert restarted.calls == [AttemptPhase.EXECUTE]


@pytest.mark.asyncio
async def test_pending_yields_without_polling_or_durable_progress() -> None:
    handlers = Handlers(advance=False, wait=True)
    assert await AttemptRuntime(handlers).run() is handlers.pending
    assert handlers.calls == [AttemptPhase.AUTHORIZE]


@pytest.mark.asyncio
async def test_progress_requires_a_new_durable_revision() -> None:
    handlers = Handlers(advance=False, wait=False)
    with pytest.raises(RuntimeError, match="no durable progress"):
        await AttemptRuntime(handlers).run()
    assert handlers.calls == [AttemptPhase.AUTHORIZE]


@pytest.mark.asyncio
async def test_same_phase_save_counts_as_progress() -> None:
    handlers = Handlers(advance=False, wait=False)

    async def save_then_wait(checkpoint: AttemptCheckpoint):
        handlers.calls.append(checkpoint.phase)
        if len(handlers.calls) > 1:
            return ReturnResolution(handlers.pending)
        await handlers.checkpoints.commit(checkpoint, expected_revision=checkpoint.revision, fencing_token=1)
        return DurableProgress()

    handlers.phases[AttemptPhase.AUTHORIZE] = save_then_wait
    assert await AttemptRuntime(handlers).run() is handlers.pending
    assert handlers.calls == [AttemptPhase.AUTHORIZE, AttemptPhase.AUTHORIZE]
