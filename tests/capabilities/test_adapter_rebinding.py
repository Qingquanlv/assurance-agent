from __future__ import annotations

import sys

import pytest

from tests.capabilities.six_wheel_harness import (
    invocation_files_contain_provider_transcript,
    replay_after_deleting_provider_state,
    run_fixture,
)


@pytest.mark.asyncio
async def test_replay_succeeds_after_deleting_fake_provider_state() -> None:
    run = await run_fixture("six-wheel-opencode")
    assert run.adapter_id == "runtime.opencode"
    assert run.agent_request_bytes
    assert run.business_output_bytes
    assert [entry for entry in sys.path if entry.startswith(str(run.workspace))] == []
    assert invocation_files_contain_provider_transcript(run.invocation_root) is False
    assert run.provider_state_dir.is_dir()
    replay_after_deleting_provider_state(run)
    assert not run.provider_state_dir.exists()
