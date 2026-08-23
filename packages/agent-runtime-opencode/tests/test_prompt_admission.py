from __future__ import annotations

import pytest

from agent_runtime_opencode.discovery import OpenCodeDispatchIncomplete, expected_message_id
from agent_runtime_opencode.observation import classify_admission
from harness import (  # pyright: ignore[reportMissingImports]
    _bound_fixture,
    _open_code_fixture,
    agent_run_request,
    prepared_snapshot,
    prompt_admission_body,
    task_request,
)


def _provider_envelope(expected: dict[str, object], *, session_id: str, message_id: str) -> dict[str, object]:
    parts = expected["parts"]
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        raise AssertionError("expected admission body must include a text part")
    text = parts[0].get("text")
    if not isinstance(text, str):
        raise AssertionError("expected admission body must include a text part")
    return {
        "id": message_id,
        "messageID": message_id,
        "role": "user",
        "time": {"created": 1_755_923_000_000},
        "parts": [
            {
                "id": "prt_live_envelope",
                "sessionID": session_id,
                "text": text,
                "type": "text",
            }
        ],
    }


def test_expected_message_id_uses_opencode_msg_prefix() -> None:
    message_id = expected_message_id(prepared_snapshot(task_request()))
    assert message_id.startswith("msg_")
    assert len(message_id) == 68
    assert message_id[4:].isalnum()


@pytest.mark.parametrize(
    "cut",
    ["before_prompt_post", "after_admission_before_response", "after_lost_success_response"],
)
async def test_prompt_crashes_converge_to_one_admission(cut: str) -> None:
    fixture = _bound_fixture(prompt_cut=cut)
    try:
        await fixture.run_and_reconcile()
        assert fixture.fake.accepted_message_count(fixture.reference.expected_message_id) == 1
    finally:
        fixture.close()


async def test_live_get_message_info_parts_shape_is_already_admitted() -> None:
    fixture = _bound_fixture(terminal_mode="busy")
    try:
        expected = prompt_admission_body(fixture.request, fixture.reference.expected_message_id)
        fixture.fake.plant_message(
            fixture.reference.session_id,
            fixture.reference.expected_message_id,
            {
                "info": {
                    "id": fixture.reference.expected_message_id,
                    "role": "user",
                    "sessionID": fixture.reference.session_id,
                },
                "parts": [
                    {
                        "id": "prt_live",
                        "messageID": fixture.reference.expected_message_id,
                        "sessionID": fixture.reference.session_id,
                        "text": expected["parts"][0]["text"],
                        "type": "text",
                    }
                ],
            },
        )
        result = await fixture.reconcile()
        assert result.status == "running"
        assert result.status != "indeterminate"
        assert fixture.fake.prompt_posts == 0
    finally:
        fixture.close()


async def test_does_not_repost_when_provider_assigns_a_different_message_id() -> None:
    fixture = _bound_fixture(terminal_mode="busy")
    try:
        expected = prompt_admission_body(fixture.request, fixture.reference.expected_message_id)
        fixture.fake.plant_message(fixture.reference.session_id, "msg_provider_rewritten", expected)
        result = await fixture.reconcile()
        assert result.status == "running"
        assert fixture.fake.prompt_posts == 0
    finally:
        fixture.close()


async def test_same_message_id_with_changed_body_fails_closed() -> None:
    fixture = _bound_fixture(existing_message_body={"changed": True})
    try:
        result = await fixture.reconcile()
        assert result.status == "indeterminate"
        assert fixture.fake.prompt_posts == 0
    finally:
        fixture.close()


async def test_prompt_body_forwards_exact_selection_without_extras() -> None:
    fixture = _bound_fixture(terminal_mode="success")
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        assert fixture.fake.prompt_posts == 1
        body = fixture.fake.prompt_bodies[0]
        expected = prompt_admission_body(fixture.request, fixture.reference.expected_message_id)
        assert body == expected
        assert set(body) <= {"messageID", "parts", "model"}
        assert "system" not in body
        assert "tools" not in body
        assert "fallback" not in body
        assert "agent" not in body
        assert "model" not in body
    finally:
        fixture.close()


def test_classify_admission_accepts_provider_envelope_with_message_id() -> None:
    expected = {"messageID": "msg_abc", "parts": [{"type": "text", "text": "write result.json"}]}
    envelope = _provider_envelope(expected, session_id="ses_1", message_id="msg_abc")
    assert classify_admission(envelope, expected) == "exact"


def test_classify_admission_same_message_id_changed_text_is_conflict() -> None:
    expected = {"messageID": "msg_abc", "parts": [{"type": "text", "text": "write result.json"}]}
    envelope = _provider_envelope(expected, session_id="ses_1", message_id="msg_abc")
    envelope["parts"] = [{"id": "prt_changed", "sessionID": "ses_1", "text": "changed", "type": "text"}]
    assert classify_admission(envelope, expected) == "conflict"


def test_classify_admission_omitted_provider_model_is_not_drift() -> None:
    expected = {
        "messageID": "msg_abc",
        "model": {"providerID": "openai", "modelID": "gpt-5.6-terra"},
        "parts": [{"type": "text", "text": "write result.json"}],
    }
    envelope = _provider_envelope(expected, session_id="ses_1", message_id="msg_abc")
    assert "model" not in envelope
    assert classify_admission(envelope, expected) == "exact"


def test_classify_admission_same_id_accepts_provider_prefixed_text() -> None:
    expected = {
        "messageID": "msg_abc",
        "model": {"providerID": "openai", "modelID": "gpt-5.6-terra"},
        "parts": [
            {"type": "text", "text": "# intake"},
            {"type": "text", "text": "middle"},
            {"type": "text", "text": '{"ok":true}'},
        ],
    }
    envelope = {
        "info": {
            "id": "msg_abc",
            "role": "user",
            "model": {"providerID": "openai", "modelID": "gpt-5.6-terra"},
            "time": {"created": 1},
        },
        "parts": [
            {
                "id": "prt_0",
                "sessionID": "ses_1",
                "text": "[provider prefix]\n# intake",
                "type": "text",
            },
            {"id": "prt_1", "sessionID": "ses_1", "text": "middle", "type": "text"},
            {"id": "prt_2", "sessionID": "ses_1", "text": '{"ok":true}', "type": "text"},
        ],
    }
    assert classify_admission(envelope, expected) == "exact"


def test_classify_admission_changed_model_fails_closed() -> None:
    expected = {
        "messageID": "msg_abc",
        "model": {"providerID": "openai", "modelID": "gpt-5.6-terra"},
        "parts": [{"type": "text", "text": "write result.json"}],
    }
    envelope = _provider_envelope(expected, session_id="ses_1", message_id="msg_abc")
    envelope["model"] = {"providerID": "openai", "modelID": "other"}
    assert classify_admission(envelope, expected) == "conflict"


async def test_live_provider_envelope_with_message_id_is_already_admitted() -> None:
    fixture = _bound_fixture(terminal_mode="busy")
    try:
        expected = prompt_admission_body(fixture.request, fixture.reference.expected_message_id)
        envelope = _provider_envelope(
            expected,
            session_id=fixture.reference.session_id or "",
            message_id=fixture.reference.expected_message_id,
        )
        fixture.fake.plant_message(
            fixture.reference.session_id or "",
            fixture.reference.expected_message_id,
            envelope,
        )
        result = await fixture.reconcile()
        assert result.status == "running"
        assert result.status != "indeterminate"
        assert fixture.fake.prompt_posts == 0
        assert classify_admission(envelope, expected) == "exact"
    finally:
        fixture.close()


async def test_selected_model_info_parts_envelope_is_already_admitted() -> None:
    selected = agent_run_request().model_copy(
        update={
            "execution": agent_run_request().execution.model_copy(
                update={"provider_model": "openai/gpt-5.6-terra"}
            )
        }
    )
    fixture = _bound_fixture(terminal_mode="busy", agent_run=selected)
    try:
        expected = prompt_admission_body(fixture.request, fixture.reference.expected_message_id)
        assert expected.get("model") == {"providerID": "openai", "modelID": "gpt-5.6-terra"}
        parts = expected["parts"]
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
            raise AssertionError("expected admission body must include a text part")
        text = parts[0].get("text")
        if not isinstance(text, str):
            raise AssertionError("expected admission body must include a text part")
        envelope = {
            "info": {
                "id": fixture.reference.expected_message_id,
                "role": "user",
                "sessionID": fixture.reference.session_id,
            },
            "parts": [
                {
                    "id": "prt_live",
                    "messageID": fixture.reference.expected_message_id,
                    "sessionID": fixture.reference.session_id,
                    "text": text,
                    "type": "text",
                }
            ],
        }
        fixture.fake.plant_message(
            fixture.reference.session_id or "",
            fixture.reference.expected_message_id,
            envelope,
        )
        result = await fixture.reconcile()
        assert result.status == "running"
        assert fixture.fake.prompt_posts == 0
        assert classify_admission(envelope, expected) == "exact"
    finally:
        fixture.close()


async def test_execute_after_bind_returns_failed_on_prompt_conflict() -> None:
    fixture = _bound_fixture(existing_message_body={"changed": True})
    try:
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        assert outcome.status == "failed"
        assert outcome.failure is not None
        assert outcome.failure.retryable is False
        assert outcome.failure.message == "prompt identity conflict"
    finally:
        fixture.close()


async def test_execute_raises_before_bind_on_multiple_matches() -> None:
    fixture = _open_code_fixture(existing_matches=2)
    try:
        with pytest.raises(OpenCodeDispatchIncomplete, match="multiple exact metadata matches"):
            await fixture.handler.execute(fixture.request, fixture.context)
        assert fixture.port.snapshot.reference is None
    finally:
        fixture.close()
