from __future__ import annotations

from tests.agent_runtime.conformance import assert_common_runtime_adapter_contract
from tests.agent_runtime.fakes import OpenCodeRuntimeHarness


async def test_opencode_shared_adapter_contract() -> None:
    harness = OpenCodeRuntimeHarness()
    assert harness.recovery_profile == "durable_reference"
    await assert_common_runtime_adapter_contract(harness)
