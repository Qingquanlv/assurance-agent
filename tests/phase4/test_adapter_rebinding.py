from __future__ import annotations

import pytest

from tests.phase4.six_wheel_harness import (
    invocation_files_contain_provider_transcript,
    replay_after_deleting_provider_state,
    run_fixture,
)


@pytest.mark.asyncio
async def test_rebinding_preserves_business_request_and_result() -> None:
    opencode = await run_fixture("phase4-opencode")
    cursor = await run_fixture("phase4-cursor")
    assert opencode.agent_request_bytes == cursor.agent_request_bytes
    assert opencode.business_output_bytes == cursor.business_output_bytes
    assert opencode.adapter_id != cursor.adapter_id
    assert opencode.lock_digest != cursor.lock_digest
    assert opencode.composition_digest != cursor.composition_digest


@pytest.mark.asyncio
async def test_replay_succeeds_after_deleting_fake_provider_state() -> None:
    run = await run_fixture("phase4-opencode")
    assert run.provider_state_dir.is_dir()
    replay_after_deleting_provider_state(run)
    assert not run.provider_state_dir.exists()


@pytest.mark.asyncio
async def test_invocation_files_omit_session_event_and_provider_transcript() -> None:
    run = await run_fixture("phase4-cursor")
    assert invocation_files_contain_provider_transcript(run.invocation_root) is False
