from __future__ import annotations

import base64
import json
import logging
from io import StringIO
from pathlib import Path
from urllib.parse import quote

import pytest

from agent_runtime_opencode.protocol import canonical_json_text
from agent_runtime_opencode.reducer import reduce_terminal
from agent_runtime_opencode.redaction import (
    encoded_canary_forms,
    redact_text,
    redact_json,
    reject_canaries_in_payload,
    scan_for_canaries,
)
from harness import (  # pyright: ignore[reportMissingImports]
    _CANARY,
    _SECRET_TEXT,
    _bound_fixture,
    _terminal_success_fixture,
    agent_run_request,
    task_request,
)


_REVIEW_PROSE = (
    'AuthControl.is_authed treats literal token == "dev" as authenticated. '
    "The API documents a Bearer scheme, not a raw cookie header."
)


@pytest.mark.parametrize("key", ["password", "admin_password", "adminPassword", "user-password"])
def test_password_key_transport_mapping_is_redacted_and_rejected(key: str) -> None:
    payload = {"headers": {key: "source pass 123"}}
    assert redact_json(payload) == {"headers": {key: "[redacted]"}}
    with pytest.raises(ValueError, match="credential"):
        reject_canaries_in_payload(payload)


@pytest.mark.parametrize(
    "diagnostic",
    [
        'login failed: {"admin_password": "source-pass-123"}',
        "login failed: {'admin_password': 'source pass 123'}",
        'admin_password="source pass 123"',
        "admin_password='source pass 123'",
        'admin_password="source \\"pass\\" 123"',
        'admin_password="source\npass 123"',
        'admin_password="source pass 123',
    ],
)
def test_password_diagnostic_is_redacted_in_real_terminal_failure(diagnostic: str) -> None:
    run = agent_run_request()
    outcome = reduce_terminal(
        kind="failed",
        session={"error": {"name": "ProviderError", "message": diagnostic}},
        messages=[],
        agent_run=run,
        request=task_request(run),
        diff=None,
    )
    assert outcome.failure is not None
    assert "[redacted]" in outcome.failure.message
    assert "source" not in outcome.failure.message
    assert "123" not in outcome.failure.message
    with pytest.raises(ValueError, match="credential"):
        reject_canaries_in_payload({"diagnostic": diagnostic})


def test_review_prose_is_not_treated_as_a_credential() -> None:
    assert redact_text(_REVIEW_PROSE) == _REVIEW_PROSE
    reject_canaries_in_payload({"claim": _REVIEW_PROSE})


def test_password_comparison_is_not_a_transport_credential() -> None:
    prose = 'The code checks admin_password == "source pass 123".'
    assert redact_text(prose) == prose
    reject_canaries_in_payload({"claim": prose})


@pytest.mark.parametrize("value", ["Bearer x", "Bearer abcdefgh!suffix", "Basic x:y"])
def test_explicit_authorization_redacts_and_rejects_the_complete_value(value: str) -> None:
    header = f"Authorization: {value}"
    assert redact_text(header) == "[redacted]"
    with pytest.raises(ValueError, match="credential"):
        reject_canaries_in_payload({"diagnostic": header})


def test_bare_bearer_redaction_does_not_leave_an_unsupported_suffix() -> None:
    assert redact_text("Bearer abcdefgh!suffix") == "[redacted]"


@pytest.mark.parametrize("style", ["json", "repr"])
@pytest.mark.parametrize("value", ["Bearer x", "Basic x:y"])
def test_serialized_authorization_is_rejected(style: str, value: str) -> None:
    headers = {"Authorization": value}
    diagnostic = json.dumps(headers) if style == "json" else repr(headers)
    with pytest.raises(ValueError, match="credential"):
        reject_canaries_in_payload({"diagnostic": diagnostic})


@pytest.mark.parametrize("style", ["json", "repr"])
@pytest.mark.parametrize("value", ["Bearer x", "Basic x:y", "Bearer abcdefgh!suffix"])
def test_serialized_authorization_is_fully_redacted(style: str, value: str) -> None:
    headers = {"Authorization": value}
    diagnostic = json.dumps(headers) if style == "json" else repr(headers)
    redacted = redact_text(diagnostic)
    assert "[redacted]" in redacted
    assert value not in redacted
    assert value.split(" ", 1)[1] not in redacted


def test_quoted_authorization_comparison_remains_prose() -> None:
    prose = "'Authorization' == \"Bearer x\" is a comparison, not a header assignment."
    assert redact_text(prose) == prose
    reject_canaries_in_payload({"claim": prose})


def test_authorization_mapping_is_redacted_and_rejected_even_with_a_short_value() -> None:
    value = {"headers": {"Authorization": "Bearer x"}}
    assert redact_json(value) == {"headers": {"Authorization": "[redacted]"}}
    with pytest.raises(ValueError, match="credential"):
        reject_canaries_in_payload(value)


def test_assignment_and_bearer_tokens_are_still_credentials() -> None:
    with pytest.raises(ValueError, match="credential"):
        reject_canaries_in_payload({"note": f"Authorization: Bearer {_SECRET_TEXT}"})
    with pytest.raises(ValueError, match="credential"):
        reject_canaries_in_payload({"note": f"OPENCODE_TOKEN={_SECRET_TEXT}"})
    with pytest.raises(ValueError, match="credential"):
        reject_canaries_in_payload({"note": f"api_key={_SECRET_TEXT}"})


def test_redaction_happens_before_size_limiting() -> None:
    message = f"Authorization: Bearer {_SECRET_TEXT}" + ("n" * 500)
    redacted = redact_text(message, canaries=(_SECRET_TEXT,), limit=240)
    assert _SECRET_TEXT not in redacted
    assert "sk-" not in redacted
    assert "[redacted]" in redacted
    assert len(redacted) <= 240


def test_encoded_canary_forms_are_detected() -> None:
    forms = encoded_canary_forms(_CANARY)
    assert _SECRET_TEXT.encode("utf-8") in forms
    assert base64.b64encode(_CANARY) in forms
    assert quote(_SECRET_TEXT).encode("utf-8") in forms
    text = f"cookie={quote(_SECRET_TEXT)}; url=https://user:{_SECRET_TEXT}@example.invalid"
    redacted = redact_text(text, canaries=(_SECRET_TEXT,))
    assert _SECRET_TEXT not in redacted
    assert quote(_SECRET_TEXT) not in redacted
    assert "[redacted]" in redacted


def _durable_blobs(fixture: object, outcome: object) -> tuple[tuple[str, ...], Path]:
    port = fixture.port  # type: ignore[attr-defined]
    context = fixture.context  # type: ignore[attr-defined]
    dumped_outcome = canonical_json_text(outcome.model_dump(mode="json"))  # type: ignore[union-attr]
    dumped_snapshot = canonical_json_text(port.snapshot.model_dump(mode="json"))
    workspace = context.write_root
    assert isinstance(workspace, Path)
    return (dumped_outcome, dumped_snapshot), workspace


def _capture_logs() -> tuple[logging.Handler, StringIO]:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    return handler, stream


def _captured_text(handler: logging.Handler, stream: StringIO) -> str:
    logging.getLogger().removeHandler(handler)
    handler.close()
    return stream.getvalue()


async def test_success_durable_outputs_and_workspace_have_no_canary() -> None:
    handler, stream = _capture_logs()
    fixture = _terminal_success_fixture()
    try:
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        texts, workspace = _durable_blobs(fixture, outcome)
        logs = _captured_text(handler, stream)
        scan_for_canaries(texts=(*texts, logs), roots=(workspace,), canaries=(_CANARY,))
        for text in (*texts, logs):
            assert _SECRET_TEXT not in text
            assert base64.b64encode(_CANARY).decode("ascii") not in text
    finally:
        logging.getLogger().removeHandler(handler)
        fixture.close()


_NOTE_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "ok": {"const": True, "type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["ok", "note"],
    "type": "object",
}


async def test_structured_result_canary_is_invalid_output() -> None:
    fixture = _bound_fixture(
        terminal_mode="success",
        sse_mode="fast_idle",
        result_schema=_NOTE_SCHEMA,
    )
    fixture.fake.structured_result = {"ok": True, "note": _SECRET_TEXT}
    try:
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        assert outcome.status == "failed"
        assert outcome.failure is not None
        assert outcome.failure.kind == "invalid_output"
        assert outcome.failure.retryable is False
        texts, workspace = _durable_blobs(fixture, outcome)
        scan_for_canaries(texts=texts, roots=(workspace,), canaries=(_CANARY,))
        dumped = canonical_json_text(outcome.model_dump(mode="json"))
        assert _SECRET_TEXT not in dumped
        assert outcome.output is None
    finally:
        fixture.close()


async def test_busy_structured_result_with_canary_is_not_aborted() -> None:
    fixture = _bound_fixture(
        terminal_mode="success_busy",
        result_schema=_NOTE_SCHEMA,
    )
    fixture.fake.structured_result = {"ok": True, "note": _SECRET_TEXT}
    try:
        result = await fixture.reconcile()

        assert result.status == "running"
        assert fixture.fake.abort_calls == 0
    finally:
        fixture.close()


async def test_provider_error_redacts_canary_from_typed_failure() -> None:
    handler, stream = _capture_logs()
    fixture = _bound_fixture(terminal_mode="error", sse_mode="fast_idle")
    fixture.fake.error_message = f"Authorization: Bearer {_SECRET_TEXT}; OPENCODE_API_KEY={_SECRET_TEXT}"
    try:
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        assert outcome.status == "failed"
        assert outcome.failure is not None
        assert _SECRET_TEXT not in outcome.failure.message
        texts, workspace = _durable_blobs(fixture, outcome)
        logs = _captured_text(handler, stream)
        scan_for_canaries(texts=(*texts, logs), roots=(workspace,), canaries=(_CANARY,))
    finally:
        logging.getLogger().removeHandler(handler)
        fixture.close()


def test_scan_detects_byte_and_encoded_canaries(tmp_path: Path) -> None:
    leaked = tmp_path / "ledger.json"
    leaked.write_bytes(base64.b64encode(_CANARY))
    with pytest.raises(ValueError, match="canary"):
        scan_for_canaries(texts=(), roots=(tmp_path,), canaries=(_CANARY,))
    leaked.unlink()
    (tmp_path / "clean.txt").write_text("ok", encoding="utf-8")
    scan_for_canaries(texts=("idle",), roots=(tmp_path,), canaries=(_CANARY,))
