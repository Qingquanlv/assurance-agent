from __future__ import annotations

from tests.agent_runtime.conformance import assert_common_runtime_adapter_contract
from tests.agent_runtime.fakes import CursorRuntimeHarness


async def test_cursor_shared_adapter_contract() -> None:
    harness = CursorRuntimeHarness()
    assert harness.recovery_profile == "confined_process"
    await assert_common_runtime_adapter_contract(harness)
