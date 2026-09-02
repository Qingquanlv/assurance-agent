from __future__ import annotations

import json

import pytest

from agent_runtime_contracts import InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_opencode.discovery import OpenCodeDispatchIncomplete, expected_message_id
from agent_runtime_opencode.observation import (
    classify_admission,
    prompt_admission_body as build_prompt_admission_body,
    user_prompt_already_admitted,
)
from harness import (  # pyright: ignore[reportMissingImports]
    _bound_fixture,
    _open_code_fixture,
    agent_run_request,
    prepared_snapshot,
    prompt_admission_body,
    task_request,
)


def _provider_parts(
    expected: dict[str, object], *, session_id: str, message_id: str
) -> list[dict[str, object]]:
    parts = expected["parts"]
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        raise AssertionError("expected admission body must include a text part")
    provider_parts: list[dict[str, object]] = []
    for index, part in enumerate(parts):
        text = part.get("text") if isinstance(part, dict) else None
        if not isinstance(text, str):
            raise AssertionError("expected admission body must include only text parts")
        provider_parts.append(
            {
                "id": f"prt_live_{index}",
                "messageID": message_id,
                "sessionID": session_id,
                "text": text,
                "type": "text",
            }
        )
    return provider_parts


def _provider_envelope(expected: dict[str, object], *, session_id: str, message_id: str) -> dict[str, object]:
    return {
        "id": message_id,
        "messageID": message_id,
        "role": "user",
        "time": {"created": 1_755_923_000_000},
        "parts": _provider_parts(expected, session_id=session_id, message_id=message_id),
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
                "parts": _provider_parts(
                    expected,
                    session_id=fixture.reference.session_id or "",
                    message_id=fixture.reference.expected_message_id,
                ),
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


async def test_prompt_body_forwards_bounded_agent_selection_and_message_policy() -> None:
    run = agent_run_request()
    fixture = _bound_fixture(terminal_mode="success", agent_run=run)
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        assert fixture.fake.prompt_posts == 1
        body = fixture.fake.prompt_bodies[0]
        expected = prompt_admission_body(fixture.request, fixture.reference.expected_message_id)
        assert body == expected
        assert set(body) <= {"messageID", "parts", "agent", "model", "tools"}
        assert "system" not in body
        assert "fallback" not in body
        assert body["agent"] == run.workspace.agent_profile
        assert "tools" not in body
        assert "model" not in body
        contract_part = body["parts"][-1]
        assert contract_part["type"] == "text"
        assert contract_part["text"].startswith("# Runtime result contract\n")
        assert "final assistant response MUST be exactly one JSON object" in contract_part["text"]
        assert "does not replace required tool calls or file writes" in contract_part["text"]
        assert "schema_id: fixture.result.v1" in contract_part["text"]
    finally:
        fixture.close()


def test_prompt_body_embeds_the_locked_result_schema_when_available() -> None:
    schema = {
        "type": "object",
        "required": ["output_files"],
        "properties": {"output_files": {"type": "array", "items": {"type": "string"}}},
    }
    run = agent_run_request().model_copy(
        update={
            "result_contract": ResultContract(
                schema_id="fixture.result.v1",
                schema_digest=canonical_digest(schema),
                delivery_mode="assistant_json_local_v1",
                schema_document=schema,
            )
        }
    )
    body = build_prompt_admission_body(run, "msg_contract")
    assert '"required":["output_files"]' in body["parts"][-1]["text"]


def test_prompt_renders_authenticated_schema_after_business_text_without_format() -> None:
    schema = {
        "additionalProperties": False,
        "properties": {"ok": {"const": True, "type": "boolean"}},
        "required": ["ok"],
        "type": "object",
    }
    run = agent_run_request().model_copy(
        update={
            "instructions": (
                InstructionPart.text("text/plain", "business instructions"),
                InstructionPart.from_json({"context": "authenticated-input"}),
            ),
            "result_contract": ResultContract(
                schema_id="fixture.result.v1",
                schema_digest=canonical_digest(schema),
                delivery_mode="assistant_json_local_v1",
                schema_document=schema,
            ),
        }
    )
    body = build_prompt_admission_body(run, "msg_contract")

    assert "format" not in body
    assert set(body) <= {"messageID", "parts", "agent", "model", "tools"}
    texts = [part["text"] for part in body["parts"]]
    assert texts[0] == "business instructions"
    assert '"context":"authenticated-input"' in texts[1]
    contract_text = texts[-1]
    assert contract_text.startswith("# Runtime result contract\n")
    assert "exactly one JSON object" in contract_text
    assert "delivery_mode: assistant_json_local_v1" in contract_text
    assert '"const":true' in contract_text
    assert '"required":["ok"]' in contract_text


async def test_admitted_prompt_never_sends_opencode_json_schema_format() -> None:
    fixture = _bound_fixture(terminal_mode="success")
    try:
        await fixture.handler.execute(fixture.request, fixture.context)
        assert fixture.fake.prompt_posts == 1
        body = fixture.fake.prompt_bodies[0]
        assert "format" not in body
        assert "json_schema" not in json.dumps(body)
    finally:
        fixture.close()


def test_prompt_body_does_not_add_agent_policy_without_a_bound_agent() -> None:
    body = build_prompt_admission_body(agent_run_request(), "msg_contract")

    assert "tools" not in body
    assert not body["parts"][0]["text"].startswith("<system-reminder>")


def test_classify_admission_accepts_provider_envelope_with_message_id() -> None:
    expected = {"messageID": "msg_abc", "parts": [{"type": "text", "text": "write result.json"}]}
    envelope = _provider_envelope(expected, session_id="ses_1", message_id="msg_abc")
    assert classify_admission(envelope, expected) == "exact"


def test_classify_admission_same_message_id_changed_text_is_conflict() -> None:
    expected = {"messageID": "msg_abc", "parts": [{"type": "text", "text": "write result.json"}]}
    envelope = _provider_envelope(expected, session_id="ses_1", message_id="msg_abc")
    envelope["parts"] = [{"id": "prt_changed", "sessionID": "ses_1", "text": "changed", "type": "text"}]
    assert classify_admission(envelope, expected) == "conflict"


def test_classify_admission_same_message_id_partial_parts_are_pending() -> None:
    expected = {
        "messageID": "msg_abc",
        "parts": [
            {"type": "text", "text": "first instruction"},
            {"type": "text", "text": "second instruction"},
        ],
    }
    envelope = _provider_envelope(expected, session_id="ses_1", message_id="msg_abc")
    envelope["parts"] = envelope["parts"][:1]

    assert classify_admission(envelope, expected) == "pending"


def test_list_fallback_treats_same_id_partial_message_as_already_admitted() -> None:
    expected = {
        "messageID": "msg_abc",
        "parts": [
            {"type": "text", "text": "first instruction"},
            {"type": "text", "text": "second instruction"},
        ],
    }
    envelope = _provider_envelope(expected, session_id="ses_1", message_id="msg_abc")
    envelope["parts"] = envelope["parts"][:1]

    assert user_prompt_already_admitted([envelope], expected) is True


def test_list_fallback_rejects_same_id_changed_message() -> None:
    expected = {"messageID": "msg_abc", "parts": [{"type": "text", "text": "expected"}]}
    envelope = _provider_envelope(expected, session_id="ses_1", message_id="msg_abc")
    envelope["parts"] = [{"type": "text", "text": "changed"}]

    assert user_prompt_already_admitted([envelope], expected) is False


async def test_partial_prompt_materialization_does_not_repost_or_fail() -> None:
    fixture = _bound_fixture(terminal_mode="busy")
    try:
        expected = prompt_admission_body(fixture.request, fixture.reference.expected_message_id)
        envelope = _provider_envelope(
            expected,
            session_id=fixture.reference.session_id or "",
            message_id=fixture.reference.expected_message_id,
        )
        envelope["parts"] = envelope["parts"][:1]
        fixture.fake.plant_message(
            fixture.reference.session_id or "",
            fixture.reference.expected_message_id,
            envelope,
        )

        result = await fixture.reconcile()

        assert result.status == "running"
        assert fixture.fake.prompt_posts == 0
    finally:
        fixture.close()


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
        envelope = {
            "info": {
                "id": fixture.reference.expected_message_id,
                "role": "user",
                "sessionID": fixture.reference.session_id,
            },
            "parts": _provider_parts(
                expected,
                session_id=fixture.reference.session_id or "",
                message_id=fixture.reference.expected_message_id,
            ),
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
