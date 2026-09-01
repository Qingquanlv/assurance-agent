from __future__ import annotations

import time

from agent_runtime_contracts import InstructionPart
from agent_runtime_contracts.runtime_binding import AgentRuntimeCapabilities
from agent_runtime_opencode.observation import (
    advertised_runtime_capabilities,
    classify_provider_state,
    provider_error_is_transient,
    provider_error_message,
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


def test_opencode_observation_advertises_local_schema_validation_only() -> None:
    assert advertised_runtime_capabilities() == AgentRuntimeCapabilities(provider_schema=False)
    assert advertised_runtime_capabilities().provider_schema is False


def test_nested_opencode_provider_error_message_is_transient() -> None:
    messages = [
        {
            "info": {
                "role": "assistant",
                "error": {
                    "name": "UnknownError",
                    "data": {"message": "unknown certificate verification error"},
                },
            },
            "parts": [],
        }
    ]

    assert provider_error_message({}, messages) == "unknown certificate verification error"
    assert provider_error_is_transient({}, messages) is True


def test_complete_result_wins_message_aborted_race() -> None:
    messages = [
        {
            "info": {
                "role": "assistant",
                "error": {"name": "MessageAbortedError", "data": {"message": "Aborted"}},
            },
            "parts": [{"type": "text", "text": '{"output_files":["result.json"]}'}],
        }
    ]

    assert structured_result_from_messages(messages) == {"output_files": ["result.json"]}
    assert (
        classify_provider_state(
            session_id="ses_1",
            status_map={},
            session={},
            messages=messages,
        )
        == "succeeded"
    )


def test_incomplete_result_with_message_aborted_stays_canceled() -> None:
    messages = [
        {
            "info": {
                "role": "assistant",
                "error": {"name": "MessageAbortedError", "data": {"message": "Aborted"}},
            },
            "parts": [{"type": "text", "text": '{"output_files":'}],
        }
    ]

    assert structured_result_from_messages(messages) is None
    assert (
        classify_provider_state(
            session_id="ses_1",
            status_map={},
            session={},
            messages=messages,
        )
        == "canceled"
    )


async def test_continuous_sse_does_not_block_observation() -> None:
    fixture = _bound_fixture(
        terminal_mode="success",
        sse_mode="drip",
        request_timeout_seconds=0.4,
    )
    try:
        started = time.monotonic()
        result = await fixture.reconcile()
        assert time.monotonic() - started < 3.0
        assert result.status == "terminal"
        assert result.outcome is not None
        assert result.outcome.status == "succeeded"
    finally:
        fixture.close()


async def test_sse_observation_is_bounded_by_poll_interval() -> None:
    fixture = _bound_fixture(
        terminal_mode="success",
        sse_mode="drip",
        request_timeout_seconds=1.0,
        config_overrides={"poll_interval_seconds": 0.05},
    )
    try:
        started = time.monotonic()
        result = await fixture.reconcile()
        assert time.monotonic() - started < 0.5
        assert result.status == "terminal"
    finally:
        fixture.close()


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


async def test_busy_session_is_aborted_after_complete_structured_result_is_captured() -> None:
    fixture = _bound_fixture(terminal_mode="success_busy")
    try:
        result = await fixture.reconcile()

        assert result.status == "terminal"
        assert result.outcome is not None
        assert result.outcome.status == "succeeded"
        assert fixture.fake.abort_calls == 1
    finally:
        fixture.close()


async def test_busy_artifact_array_with_pending_continuation_is_not_aborted() -> None:
    fixture = _bound_fixture(terminal_mode="artifact_array_busy")
    try:
        result = await fixture.reconcile()

        assert result.status == "running"
        assert fixture.fake.abort_calls == 0
    finally:
        fixture.close()


async def test_busy_schema_invalid_candidate_is_not_aborted() -> None:
    fixture = _bound_fixture(terminal_mode="success_busy")
    fixture.fake.structured_result = {"artifact": "minimum-coverage-matrix.json"}
    try:
        result = await fixture.reconcile()

        assert result.status == "running"
        assert fixture.fake.abort_calls == 0
    finally:
        fixture.close()


async def test_busy_valid_receipt_wins_over_later_invalid_artifact() -> None:
    fixture = _bound_fixture(terminal_mode="mixed_result_busy")
    try:
        result = await fixture.reconcile()

        assert result.status == "terminal"
        assert result.outcome is not None
        assert result.outcome.status == "succeeded"
        assert fixture.fake.abort_calls == 1
    finally:
        fixture.close()


async def test_idle_invalid_only_candidate_has_precise_invalid_output() -> None:
    fixture = _bound_fixture(terminal_mode="success")
    fixture.fake.structured_result = {"artifact": "minimum-coverage-matrix.json"}
    try:
        result = await fixture.reconcile()

        assert result.status == "terminal"
        assert result.outcome is not None
        assert result.outcome.status == "failed"
        assert result.outcome.failure is not None
        assert result.outcome.failure.kind == "invalid_output"
        assert result.outcome.failure.message == "$: missing required properties ['ok']"
    finally:
        fixture.close()


def test_completed_write_tool_content_is_the_structured_result() -> None:
    messages = [
        {
            "info": {"id": "msg_user", "role": "user"},
            "parts": [{"type": "text", "text": 'Write result.json with {"status": "ok"}.'}],
        },
        {
            "info": {"id": "msg_write", "role": "assistant"},
            "parts": [
                {
                    "type": "tool",
                    "tool": "write",
                    "state": {
                        "status": "completed",
                        "input": {
                            "filePath": "/tmp/result.json",
                            "content": '{"status": "ok", "artifact": "result.json"}',
                        },
                    },
                },
                {"type": "text", "text": ""},
            ],
        },
        {
            "info": {"id": "msg_empty", "role": "assistant"},
            "parts": [{"type": "text", "text": ""}],
        },
    ]
    assert structured_result_from_messages(messages) == {
        "artifact": "result.json",
        "status": "ok",
    }
    assert (
        classify_provider_state(
            session_id="ses_live",
            status_map={"ses_live": {"type": "busy"}},
            session={"id": "ses_live"},
            messages=messages,
        )
        == "succeeded"
    )


def test_top_level_json_array_does_not_expose_nested_object_as_result() -> None:
    messages = [
        {
            "info": {"id": "msg_artifact_write", "role": "assistant"},
            "parts": [
                {
                    "type": "tool",
                    "tool": "artifact_write",
                    "state": {
                        "status": "completed",
                        "input": {"content": '[{"mrc_id":"MRC-API-001","required":true}]'},
                    },
                }
            ],
        }
    ]

    assert structured_result_from_messages(messages) is None


def test_running_write_tool_is_not_a_structured_result() -> None:
    messages = [
        {
            "info": {"id": "msg_write", "role": "assistant"},
            "parts": [
                {
                    "type": "tool",
                    "state": {
                        "status": "running",
                        "input": {"content": '{"ok": true}'},
                    },
                }
            ],
        }
    ]
    assert structured_result_from_messages(messages) is None


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
