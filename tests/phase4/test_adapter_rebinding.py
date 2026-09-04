from __future__ import annotations

import sys

import pytest

from tests.phase4.six_wheel_harness import (
    invocation_files_contain_provider_transcript,
    replay_after_deleting_provider_state,
    run_fixture,
)


@pytest.mark.asyncio
async def test_opencode_fixture_preserves_business_request_and_result() -> None:
    opencode = await run_fixture("phase4-opencode")
    assert opencode.adapter_id == "runtime.opencode"
    assert opencode.agent_request_bytes
    assert opencode.business_output_bytes
    assert [entry for entry in sys.path if entry.startswith(str(opencode.workspace))] == []


@pytest.mark.asyncio
async def test_replay_succeeds_after_deleting_fake_provider_state() -> None:
    run = await run_fixture("phase4-opencode")
    assert run.provider_state_dir.is_dir()
    replay_after_deleting_provider_state(run)
    assert not run.provider_state_dir.exists()


@pytest.mark.asyncio
async def test_invocation_files_omit_session_event_and_provider_transcript() -> None:
    run = await run_fixture("phase4-opencode")
    assert invocation_files_contain_provider_transcript(run.invocation_root) is False
