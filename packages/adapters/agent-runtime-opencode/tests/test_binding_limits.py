from __future__ import annotations

import asyncio
import time

import pytest

from harness import _bound_fixture  # pyright: ignore[reportMissingImports]


class _StopExecuteLoop(Exception):
    pass


async def test_execute_honors_binding_poll_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    poll_interval = 0.37
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise _StopExecuteLoop()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    fixture = _bound_fixture(terminal_mode="busy", config_overrides={"poll_interval_seconds": poll_interval})
    try:
        with pytest.raises(_StopExecuteLoop):
            await fixture.handler.execute(fixture.request, fixture.context)
        assert sleeps == [poll_interval]
    finally:
        fixture.close()


async def test_cancel_post_abort_bounded_by_cancel_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    poll_interval = 0.1
    cancel_timeout = 0.25
    now = [1000.0]
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return now[0]

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    fixture = _bound_fixture(
        terminal_mode="busy",
        config_overrides={
            "poll_interval_seconds": poll_interval,
            "cancel_timeout_seconds": cancel_timeout,
        },
    )
    try:
        result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
        assert result.status == "acknowledged"
        assert sleeps
        assert all(interval == poll_interval for interval in sleeps)
        assert now[0] >= 1000.0 + cancel_timeout
    finally:
        fixture.close()
