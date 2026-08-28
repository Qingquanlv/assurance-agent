from __future__ import annotations

import json

import pytest
from agent_runtime_cursor.parser import CursorProtocolError, StreamInput, StreamLimits, parse_stream


_CWD = "/tmp/attempt-workspace"
_SESSION = "sess-1"


def _limits() -> StreamLimits:
    return StreamLimits(
        max_output_bytes=4096,
        max_line_bytes=256,
        max_record_count=16,
        max_json_nesting=8,
        max_stderr_bytes=1024,
        max_elapsed_seconds=30,
    )


def _lines(*events: dict[str, object], extra: bytes = b"") -> bytes:
    body = b"".join(json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n" for event in events)
    return body + extra


def _init(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "cwd": _CWD,
        "session_id": _SESSION,
        "subtype": "init",
        "type": "system",
    }
    payload.update(overrides)
    return payload


def _result(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "is_error": False,
        "result": {"ok": True},
        "session_id": _SESSION,
        "subtype": "success",
        "type": "result",
    }
    payload.update(overrides)
    return payload


def _stream_fixture(name: str) -> StreamInput:
    if name == "happy":
        stdout = _lines(
            _init(),
            {
                "message": {"content": [{"text": "working", "type": "text"}], "role": "assistant"},
                "session_id": _SESSION,
                "type": "assistant",
            },
            {
                "extra_additive": True,
                "session_id": _SESSION,
                "subtype": "started",
                "type": "tool_call",
            },
            _result(),
        )
        return StreamInput(
            stdout=stdout,
            stderr=b"",
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    if name == "missing_init":
        return StreamInput(
            stdout=_lines(_result()),
            stderr=b"",
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    if name == "changed_session":
        return StreamInput(
            stdout=_lines(_init(), _result(session_id="sess-2")),
            stderr=b"",
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    if name == "oversized_line":
        pad = "x" * 240
        return StreamInput(
            stdout=_lines(_init()) + json.dumps({"pad": pad, "type": "assistant"}).encode("utf-8") + b"\n",
            stderr=b"",
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    if name == "missing_terminal":
        return StreamInput(
            stdout=_lines(_init()),
            stderr=b"",
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    if name == "exit_mismatch":
        return StreamInput(
            stdout=_lines(_init(), _result()),
            stderr=b"",
            exit_code=1,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    if name == "cwd_mismatch":
        return StreamInput(
            stdout=_lines(_init(), _result()),
            stderr=b"",
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd="/tmp/other-workspace",
        )
    if name == "unknown_type":
        return StreamInput(
            stdout=_lines(_init(), {"type": "billing", "session_id": _SESSION}, _result()),
            stderr=b"",
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    if name == "truncated_line":
        return StreamInput(
            stdout=_lines(_init()) + b'{"type":"result","subtype":"success"',
            stderr=b"",
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    if name == "nested":
        nested: object = {"ok": True}
        for _ in range(10):
            nested = {"child": nested}
        return StreamInput(
            stdout=_lines(_init(), _result(result=nested)),  # type: ignore[arg-type]
            stderr=b"",
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    if name == "stderr":
        return StreamInput(
            stdout=_lines(_init(), _result()),
            stderr=b"x" * 2000,
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    if name == "elapsed":
        return StreamInput(
            stdout=_lines(_init(), _result()),
            stderr=b"",
            exit_code=0,
            elapsed_seconds=31,
            expected_cwd=_CWD,
        )
    if name == "record_count":
        assistant: dict[str, object] = {"session_id": _SESSION, "type": "assistant"}
        return StreamInput(
            stdout=_lines(_init(), *([assistant] * 20), _result()),
            stderr=b"",
            exit_code=0,
            elapsed_seconds=0.1,
            expected_cwd=_CWD,
        )
    raise AssertionError(name)


def test_stream_parser_accepts_one_init_consistent_session_and_terminal() -> None:
    parsed = parse_stream(_stream_fixture("happy"), _limits())
    assert parsed.session_id == _SESSION
    assert parsed.exit_code == 0
    assert parsed.terminal["type"] == "result"
    assert parsed.init["cwd"] == _CWD


@pytest.mark.parametrize(
    "fixture, message",
    [
        ("missing_init", "one system init"),
        ("changed_session", "session identity changed"),
        ("oversized_line", "line byte limit"),
        ("missing_terminal", "terminal record"),
        ("exit_mismatch", "exit status"),
    ],
)
def test_stream_parser_fails_closed(fixture: str, message: str) -> None:
    with pytest.raises(CursorProtocolError, match=message):
        parse_stream(_stream_fixture(fixture), _limits())


@pytest.mark.parametrize(
    "fixture, message",
    [
        ("cwd_mismatch", "cwd"),
        ("unknown_type", "known structural event"),
        ("truncated_line", "truncated"),
        ("nested", "JSON nesting"),
        ("stderr", "stderr"),
        ("elapsed", "elapsed"),
        ("record_count", "record count"),
    ],
)
def test_stream_parser_enforces_remaining_bounds(fixture: str, message: str) -> None:
    with pytest.raises(CursorProtocolError, match=message):
        parse_stream(_stream_fixture(fixture), _limits())
