from __future__ import annotations

from pathlib import Path

import pytest

from graph_engine.persistence.runner_lease import (
    LocalInvocationRunnerLease,
    RunnerConflict,
    StaleFencingToken,
)


async def test_only_one_local_runner_acquires_invocation(tmp_path: Path) -> None:
    leases = LocalInvocationRunnerLease(tmp_path)
    first = await leases.acquire("inv-1", owner_id="runner-a")
    try:
        assert first.fencing_token == 1
        with pytest.raises(RunnerConflict):
            await leases.acquire("inv-1", owner_id="runner-b")
    finally:
        await leases.release(first)


async def test_crash_reclaim_fences_old_owner(tmp_path: Path) -> None:
    first_process = LocalInvocationRunnerLease(tmp_path)
    first = await first_process.acquire("inv-1", owner_id="runner-a")
    first_process.simulate_process_exit_for_test(first)

    replacement_process = LocalInvocationRunnerLease(tmp_path)
    second = await replacement_process.acquire("inv-1", owner_id="runner-b")
    try:
        assert second.fencing_token > first.fencing_token
        with pytest.raises(StaleFencingToken):
            await replacement_process.assert_current("inv-1", first.fencing_token)
    finally:
        await replacement_process.release(second)
