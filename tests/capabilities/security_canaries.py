"""Credential-canary injection and artifact scan for the six-wheel seam."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
import logging
from pathlib import Path
import uuid

from agent_runtime_contracts.schema import bound_redacted_diagnostics
from graph_engine.plugin_api import SecretHandleUnauthorized

from tests.capabilities.agent_harness import FakeAgentAdapter
from tests.capabilities.six_wheel_harness import (
    CASE_REVIEW_STRUCTURED,
    SixWheelTaskHost,
    _FixtureNodeOutput,
    _application_call,
    _fixture_agent_request,
    _import_activation,
    _start_until_blocked,
    resolve_fixture,
)


@dataclass
class CanaryScan:
    canary_text: str
    canary_bytes: bytes
    diagnostics: tuple[str, ...]
    roots: tuple[Path, ...]
    texts: tuple[str, ...]


class UniqueCanarySecretPort:
    def __init__(self, canary: bytes, *, handle: str = "phase4.canary") -> None:
        self._canary = canary
        self._handle = handle
        self._revoked = False

    def resolve(self, handle: str) -> bytes:
        if self._revoked:
            raise SecretHandleUnauthorized("secret port is revoked")
        if handle != self._handle:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}")
        return self._canary

    def revoke(self) -> None:
        self._revoked = True


class CanaryTaskHost(SixWheelTaskHost):
    def __init__(self, *, adapter_id: str, provider_state_dir: Path, canary: str) -> None:
        super().__init__(adapter_id=adapter_id, provider_state_dir=provider_state_dir)
        self.canary = canary
        self.secrets = UniqueCanarySecretPort(canary.encode("utf-8"))
        self.diagnostics: tuple[str, ...] = ()

    async def execute_canary(self, validated_input: object, context: object) -> _FixtureNodeOutput:
        del validated_input, context
        resolved = self.secrets.resolve("phase4.canary").decode("utf-8")
        leaking = (
            f"Authorization: Bearer {resolved}",
            f"provider failed with sk-{resolved}",
            f"api_key={resolved}",
        )
        self.diagnostics = bound_redacted_diagnostics(leaking)
        request = _fixture_agent_request()
        self.recorded_request_bytes = request.canonical_bytes()
        FakeAgentAdapter(
            CASE_REVIEW_STRUCTURED,
            adapter_id=self.adapter_id,
            adapter_version="1.0.0",
        ).execute_request(request)
        return _FixtureNodeOutput()


def inject_unique_canaries() -> CanaryScan:
    canary = f"phase4-canary-{uuid.uuid4().hex}"
    handler, stream = _capture_logs()
    resolved = resolve_fixture("six-wheel-opencode")
    host = CanaryTaskHost(
        adapter_id="runtime.opencode",
        provider_state_dir=resolved.workspace / "canary-provider",
        canary=canary,
    )
    engine_root = resolved.workspace / "canary-engine"
    with _import_activation(resolved.product_root, resolved.workspace):
        result, invocation_root, *_rest = _application_call(
            _start_until_blocked,
            engine_root,
            host,
            resolved.composition,
            "phase4-canary-inv",
            execute=host.execute_canary,
        )
    logs = _captured_text(handler, stream)
    host.secrets.revoke()
    texts = (
        result.status,
        str(result),
        logs,
        *host.diagnostics,
        bytes(resolved.composition.lock.canonical_bytes).decode("utf-8"),
        resolved.composition.lock_digest,
    )
    return CanaryScan(
        canary_text=canary,
        canary_bytes=canary.encode("utf-8"),
        diagnostics=host.diagnostics,
        roots=(invocation_root, engine_root, resolved.workspace, host.provider_state_dir),
        texts=texts,
    )


def assert_canaries_absent(scan: CanaryScan) -> None:
    for text in scan.texts:
        assert scan.canary_text not in text
    for root in scan.roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            payload = path.read_bytes()
            assert scan.canary_bytes not in payload, f"canary leaked into {path}"


def redacted_diagnostics_are_bounded(scan: CanaryScan) -> bool:
    assert scan.diagnostics
    for item in scan.diagnostics:
        assert scan.canary_text not in item
        assert "Bearer" not in item or "[redacted]" in item
    return True


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
