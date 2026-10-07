from __future__ import annotations

import pytest

from tests.capabilities.agent_cuts import (
    AGENT_CUTS,
    WHEEL_FINALIZERS,
    assert_indeterminate_is_inert,
    run_finalize_cut,
    run_six_wheel_cut,
)

_CUTS = (
    "prepare-complete",
    "dispatch-unknown",
    "bound-running",
    "result-truncated",
    "terminal-observed",
)


@pytest.mark.parametrize("cut", _CUTS)
@pytest.mark.parametrize("wheel", tuple(WHEEL_FINALIZERS))
async def test_finalize_runs_only_after_authenticated_agent_result(wheel: str, cut: str) -> None:
    assert cut in AGENT_CUTS
    executed = await run_finalize_cut(wheel, cut)
    assert executed.status == "failed"
    assert executed.stop_reason is None
    if cut == "terminal-observed":
        assert executed.failure_kind == "invalid_output"
    else:
        assert executed.failure_kind == "invalid_input"
    assert_indeterminate_is_inert(executed)


@pytest.mark.parametrize("cut", ("dispatch-unknown",))
async def test_six_wheel_indeterminate_cuts_append_no_business_state(cut: str) -> None:
    observed = await run_six_wheel_cut(cut)
    assert_indeterminate_is_inert(observed)
    assert observed.status != "stopped"
    assert observed.finalize_invoked is False or observed.finalize_failed_closed is True
