from __future__ import annotations

from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_opencode.discovery import restatement_message_id
from agent_runtime_opencode.observation import (
    classify_provider_state,
    parse_closed_terminal_result,
    restatement_admission_body,
    terminal_result_is_contract_violation,
    user_prompt_already_admitted,
)
from harness import (  # pyright: ignore[reportMissingImports]
    _bound_fixture,
    agent_run_request,
)


def _closed_session_messages(text: str) -> list[dict[str, object]]:
    return [
        {
            "info": {"id": "msg_user", "role": "user"},
            "parts": [{"type": "text", "text": "produce the artifact"}],
        },
        {
            "info": {
                "id": "msg_result",
                "role": "assistant",
                "time": {"created": 1, "completed": 2},
                "finish": "stop",
            },
            "parts": [
                {"type": "step-start"},
                {"type": "text", "text": text},
                {"type": "step-finish", "reason": "stop"},
            ],
        },
    ]


def test_prose_before_the_result_object_is_a_contract_violation() -> None:
    messages = _closed_session_messages(
        'The read-back confirms exploration.json is complete.\n\n{"output_files": ["a.json"]}'
    )

    assert terminal_result_is_contract_violation({"id": "ses_live"}, messages) is True


def test_a_lone_result_object_is_not_a_contract_violation() -> None:
    messages = _closed_session_messages('{"output_files": ["a.json"]}')

    assert terminal_result_is_contract_violation({"id": "ses_live"}, messages) is False


def test_provider_error_is_not_reported_as_a_contract_violation() -> None:
    session = {"id": "ses_live", "error": {"name": "ProviderError", "message": "upstream refused"}}

    assert terminal_result_is_contract_violation(session, _closed_session_messages("partial")) is False


def test_open_tool_work_is_not_reported_as_a_contract_violation() -> None:
    messages = _closed_session_messages("thinking")
    messages.append(
        {
            "info": {"id": "msg_tool", "role": "assistant"},
            "parts": [{"type": "tool", "state": {"status": "running"}}],
        }
    )

    assert terminal_result_is_contract_violation({"id": "ses_live"}, messages) is False


def test_restatement_message_id_is_derived_and_distinct() -> None:
    message_id = restatement_message_id("msg_" + canonical_digest({"seed": 1}))

    assert message_id.startswith("msg_")
    assert message_id != "msg_" + canonical_digest({"seed": 1})
    assert message_id == restatement_message_id("msg_" + canonical_digest({"seed": 1}))


def test_restatement_body_restates_the_contract_without_the_skill_instructions() -> None:
    agent_run = agent_run_request()
    body = restatement_admission_body(agent_run, "msg_restate")
    parts = body["parts"]
    assert isinstance(parts, list)
    assert len(parts) == 1
    text = parts[0]["text"]
    assert isinstance(text, str)

    assert body["messageID"] == "msg_restate"
    assert agent_run.result_contract.schema_digest in text
    assert "do not call tools again" not in text
    assert "already complete and verified" not in text
    assert "write" in text.casefold()
    for instruction in agent_run.instructions:
        if instruction.text_content is not None:
            assert instruction.text_content not in text


def test_restatement_does_not_forbid_writes_the_session_never_made() -> None:
    """A session that only read files can still idle with progress text.

    The corrective turn exists to recover a lone JSON object. It must not tell
    the model that required outputs are already on disk when that is the thing
    the finalize handler is about to look for.
    """
    text = restatement_admission_body(agent_run_request(), "msg_restate")["parts"][0]["text"]
    assert isinstance(text, str)
    assert "already complete and verified" not in text
    assert "do not call tools again" not in text
    assert "do not rewrite any file" not in text
    assert "required" in text.casefold()
    assert "write" in text.casefold()


def _restatement_posts(fixture: object, expected: dict[str, object]) -> int:
    bodies = fixture.fake.prompt_bodies  # pyright: ignore[reportAttributeAccessIssue]
    return sum(1 for body in bodies if body.get("messageID") == expected["messageID"])


async def test_contract_violation_admits_one_corrective_turn_then_succeeds() -> None:
    fixture = _bound_fixture(terminal_mode="contract_violation", sse_mode="fast_idle")
    try:
        expected = restatement_admission_body(
            agent_run_request(),
            restatement_message_id(fixture.reference.expected_message_id),
        )

        nudged = await fixture.reconcile()
        assert nudged.status == "running"
        assert _restatement_posts(fixture, expected) == 1

        posted = fixture.fake.prompt_bodies[-1]
        assert posted["parts"] == expected["parts"]

        fixture.fake.terminal_mode = "success"
        settled = await fixture.reconcile()
        assert settled.status == "terminal"
        assert settled.outcome is not None
        assert settled.outcome.status == "succeeded"
    finally:
        fixture.close()


async def test_the_corrective_turn_is_admitted_at_most_once() -> None:
    fixture = _bound_fixture(terminal_mode="contract_violation", sse_mode="fast_idle")
    try:
        expected = restatement_admission_body(
            agent_run_request(),
            restatement_message_id(fixture.reference.expected_message_id),
        )
        assert (await fixture.reconcile()).status == "running"
        assert _restatement_posts(fixture, expected) == 1

        settled = await fixture.reconcile()
        assert _restatement_posts(fixture, expected) == 1
        assert settled.status == "terminal"
        assert settled.outcome is not None
        assert settled.outcome.status == "failed"
        assert settled.outcome.failure is not None
        assert settled.outcome.failure.kind == "invalid_output"
    finally:
        fixture.close()


def test_an_admitted_corrective_turn_is_recognised_in_history() -> None:
    body = restatement_admission_body(agent_run_request(), "msg_restate")
    parts = body["parts"]
    assert isinstance(parts, list)
    history = [
        {
            "info": {"id": "msg_restate", "role": "user"},
            "parts": [dict(part) for part in parts],
        }
    ]

    assert user_prompt_already_admitted(history, body) is True


def test_the_corrective_turn_does_not_change_result_parsing() -> None:
    messages = _closed_session_messages('{"output_files": ["a.json"]}')

    assert parse_closed_terminal_result(messages) == {"output_files": ["a.json"]}


def _corrected_session(first: str, second: str) -> list[dict[str, object]]:
    messages = _closed_session_messages(first)
    messages.append(
        {
            "info": {"id": "msg_restate", "role": "user"},
            "parts": [{"type": "text", "text": "# Final response rejected"}],
        }
    )
    messages.append(
        {
            "info": {
                "id": "msg_second",
                "role": "assistant",
                "time": {"created": 3, "completed": 4},
                "finish": "stop",
            },
            "parts": [
                {"type": "step-start"},
                {"type": "text", "text": second},
                {"type": "step-finish", "reason": "stop"},
            ],
        }
    )
    return messages


def test_the_superseded_response_does_not_defeat_the_corrected_one() -> None:
    messages = _corrected_session(
        'All four outputs written and read back.\n\n{"output_files": ["a.json"]}',
        '{"output_files": ["a.json"]}',
    )

    assert parse_closed_terminal_result(messages) == {"output_files": ["a.json"]}
    assert terminal_result_is_contract_violation({"id": "ses_live"}, messages) is False
    assert (
        classify_provider_state(
            session_id="ses_live",
            status_map={"ses_live": {"type": "idle"}},
            session={"id": "ses_live"},
            messages=messages,
        )
        == "succeeded"
    )


def test_a_second_violation_after_the_corrective_turn_still_fails() -> None:
    messages = _corrected_session(
        'Read-back passed.\n\n{"output_files": ["a.json"]}',
        'Confirming once more.\n\n{"output_files": ["a.json"]}',
    )

    assert terminal_result_is_contract_violation({"id": "ses_live"}, messages) is True
    assert (
        classify_provider_state(
            session_id="ses_live",
            status_map={"ses_live": {"type": "idle"}},
            session={"id": "ses_live"},
            messages=messages,
        )
        == "failed"
    )


def test_a_pending_corrective_turn_keeps_the_session_running() -> None:
    messages = _closed_session_messages('Read-back passed.\n\n{"output_files": ["a.json"]}')
    messages.append(
        {
            "info": {"id": "msg_restate", "role": "user"},
            "parts": [{"type": "text", "text": "# Final response rejected"}],
        }
    )

    assert (
        classify_provider_state(
            session_id="ses_live",
            status_map={"ses_live": {"type": "idle"}},
            session={"id": "ses_live"},
            messages=messages,
        )
        == "running"
    )
