from __future__ import annotations

import base64
import json
import logging
from io import StringIO
from pathlib import Path

import pytest
from agent_runtime_contracts.schema import canonical_json_bytes
from agent_runtime_cursor.redaction import (
    encoded_canary_forms,
    redact_text,
    scan_for_canaries,
    stderr_projection,
)
from fake_process_host import FakeConfinedProcessHost  # pyright: ignore[reportMissingImports]
from graph_engine.plugin_api import SecretHandleUnauthorized
from cursor_harness import (  # pyright: ignore[reportMissingImports]
    CANARY,
    SECRET_TEXT,
    RevocableSecretPort,
    complete_stream,
    context,
    cursor_cut,
    error_stream,
    execute_fixture,
)


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


def test_redaction_happens_before_size_limiting() -> None:
    message = f"Authorization: Bearer {SECRET_TEXT}" + ("n" * 500)
    redacted = redact_text(message, canaries=(SECRET_TEXT,), limit=240)
    assert SECRET_TEXT not in redacted
    assert "sk-" not in redacted
    assert "[redacted]" in redacted
    assert len(redacted) <= 240


def test_encoded_canary_forms_are_detected() -> None:
    forms = encoded_canary_forms(CANARY)
    assert SECRET_TEXT.encode("utf-8") in forms
    assert base64.b64encode(CANARY) in forms
    text = f"CURSOR_API_KEY={SECRET_TEXT}; url=https://user:{SECRET_TEXT}@example.invalid"
    redacted = redact_text(text, canaries=(SECRET_TEXT,))
    assert SECRET_TEXT not in redacted
    assert "[redacted]" in redacted


async def test_canaries_are_absent_from_argv_receipt_result_and_durables(tmp_path: Path) -> None:
    handler, stream = _capture_logs()
    secrets = RevocableSecretPort()
    host = FakeConfinedProcessHost(stdout=complete_stream(str(tmp_path.resolve())), stderr=b"transient")
    fixture = execute_fixture(tmp_path, host)
    fixture.context, fixture.port = context(tmp_path, port=fixture.port, secrets=secrets)
    fixture.handler = fixture.handler.__class__(fixture.config, fixture.host)
    try:
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        secrets.revoke()
        with pytest.raises(SecretHandleUnauthorized, match="revoked"):
            secrets.resolve("cursor.api-key")
        launch = fixture.host.launches[0]
        assert set(launch.environment) == {"PATH", "CURSOR_API_KEY"}
        assert launch.environment["CURSOR_API_KEY"] == SECRET_TEXT
        assert SECRET_TEXT not in launch.environment["PATH"]
        assert SECRET_TEXT not in launch.argv
        receipt = fixture.port.snapshot.reference
        texts = (
            json.dumps(list(launch.argv)),
            json.dumps(thawed_reference(receipt)),
            json.dumps(fixture.port.snapshot.model_dump(mode="json")),
            json.dumps(outcome.model_dump(mode="json")),
            canonical_json_bytes(fixture.handler.dispatch_fingerprint).decode("utf-8"),
            stderr_projection(host.stderr, canaries=(SECRET_TEXT,)),
            _captured_text(handler, stream),
        )
        scan_for_canaries(texts=texts, roots=(tmp_path,), canaries=(CANARY,))
        for text in texts:
            assert SECRET_TEXT not in text
            assert base64.b64encode(CANARY).decode("ascii") not in text
    finally:
        logging.getLogger().removeHandler(handler)


def thawed_reference(reference: object) -> object:
    from agent_runtime_contracts.schema import thaw_json

    return thaw_json(reference)


async def test_provider_error_and_exception_strings_redact_canaries(tmp_path: Path) -> None:
    message = f"Authorization: Bearer {SECRET_TEXT}"
    host = FakeConfinedProcessHost(stdout=error_stream(str(tmp_path.resolve()), message=message), exit_code=1)
    fixture = execute_fixture(tmp_path, host)
    outcome = await fixture.handler.execute(fixture.request, fixture.context)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert SECRET_TEXT not in outcome.failure.message
    assert "Bearer" not in outcome.failure.message or "[redacted]" in outcome.failure.message
    dumped = outcome.model_dump_json()
    assert SECRET_TEXT not in dumped
    result = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
    assert result.reason is None or SECRET_TEXT not in result.reason


async def test_stderr_projection_and_host_receipt_omit_canaries(tmp_path: Path) -> None:
    leaked = f"CURSOR_API_KEY={SECRET_TEXT}\n"
    host = FakeConfinedProcessHost(
        stdout=complete_stream(str(tmp_path.resolve())),
        stderr=leaked.encode("utf-8"),
    )
    fixture = execute_fixture(tmp_path, host)
    outcome = await fixture.handler.execute(fixture.request, fixture.context)
    projection = stderr_projection(host.stderr, canaries=(SECRET_TEXT,))
    assert SECRET_TEXT not in projection
    durable = (
        json.dumps(outcome.model_dump(mode="json")),
        json.dumps(fixture.port.snapshot.model_dump(mode="json")),
        projection,
    )
    scan_for_canaries(texts=durable, roots=(tmp_path,), canaries=(CANARY,))


def test_scan_detects_byte_and_encoded_canaries(tmp_path: Path) -> None:
    leaked = tmp_path / "ledger.json"
    leaked.write_bytes(base64.b64encode(CANARY))
    with pytest.raises(ValueError, match="canary"):
        scan_for_canaries(texts=(), roots=(tmp_path,), canaries=(CANARY,))
    leaked.unlink()
    (tmp_path / "clean.txt").write_text("ok", encoding="utf-8")
    scan_for_canaries(texts=("idle",), roots=(tmp_path,), canaries=(CANARY,))


async def test_only_authorized_environment_name_receives_secret_bytes() -> None:
    fixture = await cursor_cut("after_host_terminal_receipt")
    launch = fixture.host.launches[0]
    secret_names = [name for name, value in launch.environment.items() if value == SECRET_TEXT]
    assert secret_names == ["CURSOR_API_KEY"]
    assert "PATH" not in secret_names
