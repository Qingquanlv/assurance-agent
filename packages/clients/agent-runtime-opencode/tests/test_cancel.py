from __future__ import annotations

from harness import _bound_fixture  # pyright: ignore[reportMissingImports]


async def test_abort_http_200_is_acknowledged_not_terminal() -> None:
    fixture = _bound_fixture(terminal_mode="busy")
    try:
        result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
        assert result.status == "acknowledged"
        assert result.outcome is None
        assert fixture.fake.abort_calls == 1
        observed = await fixture.reconcile()
        assert observed.status == "running"
    finally:
        fixture.close()


async def test_completion_racing_cancel_provider_terminal_wins() -> None:
    fixture = _bound_fixture(terminal_mode="success", sse_mode="fast_idle")
    try:
        result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
        assert result.status == "terminal"
        assert result.outcome is not None
        assert result.outcome.status == "succeeded"
    finally:
        fixture.close()


async def test_missing_session_during_cancel_is_indeterminate() -> None:
    fixture = _bound_fixture(terminal_mode="busy")
    try:
        session_id = fixture.reference.session_id
        assert session_id is not None
        fixture.fake.drop_session(session_id)
        result = await fixture.handler.cancel(fixture.request, fixture.context, fixture.activity)
        assert result.status == "indeterminate"
        assert result.status != "acknowledged"
        assert result.reason is not None
        assert "missing" in result.reason
    finally:
        fixture.close()
