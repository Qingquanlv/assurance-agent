from __future__ import annotations

import pytest

from graph_engine.plugin_api import RecoverableTaskHandler
from cursor_harness import cursor_cut  # pyright: ignore[reportMissingImports]


async def _cursor_cut(cut: str):  # type: ignore[no-untyped-def]
    return await cursor_cut(cut)


@pytest.mark.parametrize(
    "cut, expected",
    [
        ("before_spawn", "not_dispatched"),
        ("after_spawn_before_bind", "indeterminate"),
        ("after_bind", "running"),
        ("mid_stream", "indeterminate"),
        ("after_host_terminal_receipt", "terminal"),
        ("after_process_exit_without_terminal", "indeterminate"),
    ],
)
async def test_cursor_recovery_never_blindly_retries(cut: str, expected: str) -> None:
    fixture = await _cursor_cut(cut)
    result = await fixture.reconcile_after_restart()
    assert result.status == expected
    assert result.status != "absent"
    if expected == "indeterminate":
        assert fixture.spawn_count == 1
    if expected == "not_dispatched":
        assert fixture.spawn_count == 0
    if expected == "terminal":
        assert result.outcome is not None
        assert result.outcome.status == "succeeded"
        assert fixture.spawn_count == 1


async def test_cursor_handler_is_recoverable() -> None:
    fixture = await _cursor_cut("before_spawn")
    assert isinstance(fixture.handler, RecoverableTaskHandler)
