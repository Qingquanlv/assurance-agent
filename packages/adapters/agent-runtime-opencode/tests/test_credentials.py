from __future__ import annotations

import base64
import logging
from io import StringIO
from pathlib import Path
from urllib.parse import quote

import pytest

from agent_runtime_opencode.protocol import canonical_json_text
from agent_runtime_opencode.redaction import (
    encoded_canary_forms,
    redact_text,
    scan_for_canaries,
)
from harness import (  # pyright: ignore[reportMissingImports]
    _CANARY,
    _SECRET_TEXT,
    _bound_fixture,
    _terminal_success_fixture,
)


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


def test_a_described_comparison_is_not_read_as_a_credential_assignment() -> None:
    described = "TC_DEPT_007 sends an invalid header, so the guard sees token=='dev' and rejects it."
    assert redact_text(described, canaries=(_SECRET_TEXT,)) == described

    for leaked in (
        f"OPENCODE_API_KEY={_SECRET_TEXT}",
        f"password={_SECRET_TEXT}",
        f"AA_ADMIN_PASSWORD = {_SECRET_TEXT}",
    ):
        redacted = redact_text(leaked, canaries=("unrelated-canary",))
        assert _SECRET_TEXT not in redacted, leaked
        assert "[redacted]" in redacted, leaked


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
