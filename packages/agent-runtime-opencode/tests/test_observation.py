from __future__ import annotations

from harness import _bound_fixture  # pyright: ignore[reportMissingImports]


def _message_gets(fixture: object, session_id: str) -> int:
    fake = fixture.fake  # type: ignore[attr-defined]
    return sum(
        1
        for item in fake.records
        if item.method == "GET" and item.path.startswith(f"/session/{session_id}/message")
    )


async def test_fast_terminal_transition_is_observed() -> None:
    fixture = _bound_fixture(terminal_mode="success", sse_mode="fast_idle")
    try:
        result = await fixture.reconcile()
        assert result.status == "terminal"
        assert result.outcome is not None
        assert result.outcome.status == "succeeded"
        assert fixture.heartbeats
    finally:
        fixture.close()


async def test_sse_gap_authenticates_with_exact_get() -> None:
    fixture = _bound_fixture(terminal_mode="success", sse_mode="gap")
    try:
        result = await fixture.reconcile()
        session_id = fixture.reference.session_id
        assert session_id is not None
        assert result.status == "terminal"
        assert fixture.fake.count("GET", f"/session/{session_id}") >= 1
        assert _message_gets(fixture, session_id) >= 1
    finally:
        fixture.close()


async def test_cursor_reconnect_uses_locked_cursor() -> None:
    fixture = _bound_fixture(terminal_mode="success", sse_mode="cursor")
    try:
        result = await fixture.reconcile()
        assert result.status == "terminal"
        assert "cursor-1" in fixture.fake.sse_cursors
    finally:
        fixture.close()


async def test_silent_sse_falls_back_to_bounded_polling() -> None:
    fixture = _bound_fixture(
        terminal_mode="success",
        sse_mode="silent",
        request_timeout_seconds=0.2,
    )
    fixture.fake.sse_silent_seconds = 2.0
    try:
        result = await fixture.reconcile()
        assert result.status == "terminal"
        assert fixture.fake.count("GET", "/session/status") >= 1
    finally:
        fixture.close()


async def test_status_map_omission_triggers_exact_session_get() -> None:
    fixture = _bound_fixture(terminal_mode="busy", omit_status=True)
    try:
        result = await fixture.reconcile()
        session_id = fixture.reference.session_id
        assert session_id is not None
        assert result.status == "running"
        assert result.status not in {"absent", "terminal"}
        assert fixture.fake.count("GET", "/session/status") >= 1
        assert fixture.fake.count("GET", f"/session/{session_id}") >= 1
        assert _message_gets(fixture, session_id) >= 1
    finally:
        fixture.close()


async def test_transient_idle_is_not_terminal() -> None:
    fixture = _bound_fixture(terminal_mode="idle_only")
    try:
        result = await fixture.reconcile()
        session_id = fixture.reference.session_id
        assert session_id is not None
        assert result.status == "running"
        assert _message_gets(fixture, session_id) >= 1
    finally:
        fixture.close()


async def test_open_tool_work_is_not_terminal() -> None:
    fixture = _bound_fixture(terminal_mode="open_tools")
    try:
        result = await fixture.reconcile()
        session_id = fixture.reference.session_id
        assert session_id is not None
        assert result.status == "running"
        assert _message_gets(fixture, session_id) >= 1
    finally:
        fixture.close()


async def test_malformed_identity_bearing_sse_fails_closed() -> None:
    fixture = _bound_fixture(terminal_mode="busy", sse_mode="malformed_identity")
    try:
        result = await fixture.reconcile()
        assert result.status == "indeterminate"
    finally:
        fixture.close()
