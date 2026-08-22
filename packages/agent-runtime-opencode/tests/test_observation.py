from __future__ import annotations

from agent_runtime_contracts import InstructionPart
from agent_runtime_opencode.observation import (
    classify_provider_state,
    structured_result_from_messages,
)
from harness import _bound_fixture, agent_run_request  # pyright: ignore[reportMissingImports]


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


async def test_silent_sse_timeout_gets_when_poll_fallback_is_locked_off() -> None:
    fixture = _bound_fixture(
        terminal_mode="success",
        sse_mode="silent",
        request_timeout_seconds=0.2,
        poll_fallback_supported=False,
    )
    fixture.fake.sse_silent_seconds = 2.0
    try:
        result = await fixture.reconcile()
        session_id = fixture.reference.session_id
        assert session_id is not None
        assert result.status == "terminal"
        assert result.status != "indeterminate"
        assert fixture.fake.count("GET", "/session/status") >= 1
        assert fixture.fake.count("GET", f"/session/{session_id}") >= 1
        assert _message_gets(fixture, session_id) >= 1
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


async def test_idle_json_prompt_parts_are_not_a_complete_result() -> None:
    agent_run = agent_run_request().model_copy(
        update={"instructions": (InstructionPart.from_json({"task": "write result.json"}),)}
    )
    fixture = _bound_fixture(terminal_mode="idle_only", agent_run=agent_run)
    try:
        result = await fixture.reconcile()
        session_id = fixture.reference.session_id
        assert session_id is not None
        assert result.status == "running"
        assert result.status != "terminal"
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


def test_embedded_json_in_assistant_prose_is_the_structured_result() -> None:
    messages = [
        {
            "info": {"id": "msg_user", "role": "user"},
            "parts": [{"type": "text", "text": 'Write result.json with {"ok": true}.'}],
        },
        {
            "info": {"id": "msg_write", "role": "assistant"},
            "parts": [
                {"type": "tool", "state": {"status": "completed"}},
                {
                    "type": "text",
                    "text": 'Done. `result.json` written with `{"ok": true}`.',
                },
            ],
        },
        {
            "info": {"id": "msg_done", "role": "assistant"},
            "parts": [{"type": "text", "text": "Done."}],
        },
    ]
    assert structured_result_from_messages(messages) == {"ok": True}


def test_busy_session_with_completed_result_is_succeeded() -> None:
    messages = [
        {
            "info": {"id": "msg_result", "role": "assistant"},
            "parts": [{"type": "text", "text": '{"ok": true}'}],
        }
    ]
    assert (
        classify_provider_state(
            session_id="ses_live",
            status_map={"ses_live": {"type": "busy"}},
            session={"id": "ses_live"},
            messages=messages,
        )
        == "succeeded"
    )


async def test_malformed_identity_bearing_sse_fails_closed() -> None:
    fixture = _bound_fixture(terminal_mode="busy", sse_mode="malformed_identity")
    try:
        result = await fixture.reconcile()
        assert result.status == "indeterminate"
    finally:
        fixture.close()
