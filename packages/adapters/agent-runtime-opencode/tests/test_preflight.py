from __future__ import annotations

import hashlib
import http.client
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from graph_engine.plugin_api import (
    InvocationMetadata,
    SecretHandleUnauthorized,
    SecretPort,
    TaskContext,
    TaskRequest,
    TaskWorkspaceIdentity,
)
from pydantic import ValidationError

from agent_runtime_contracts.wire.schema import canonical_digest
from agent_runtime_opencode.config import OpenCodeAdapterConfig
from agent_runtime_opencode.handler import OpenCodeHandler
from agent_runtime_opencode.observe.poll import _observe_fingerprint
from agent_runtime_opencode.transport.http import OpenCodeHttpClient, canonical_json_text
from agent_runtime_opencode.transport.profile import OpenCodeProtocolProfile
from fake_server import OpenCodeFakeServer  # pyright: ignore[reportMissingImports]


_SHA = "a" * 64
_CANARY = b"canary-secret-value"
_SECRET_TEXT = "canary-secret-value"


def _profile(**overrides: object) -> OpenCodeProtocolProfile:
    payload: dict[str, object] = {
        "prompt_idempotency": "conflict-on-body-drift",
        "metadata_supported": True,
        "sse_supported": True,
        "poll_fallback_supported": True,
    }
    payload.update(overrides)
    return OpenCodeProtocolProfile.model_validate(payload)


def _config(fake: OpenCodeFakeServer, **overrides: object) -> OpenCodeAdapterConfig:
    payload: dict[str, object] = {
        "schema_version": "1",
        "endpoint": fake.base_url,
        "tls_identity_digest": _SHA,
        "secret_handle": "opencode.token",
        "protocol_profile": "opencode-http-v1",
        "project_scope": fake.project_scope,
        "request_timeout_seconds": 5,
        "observation_horizon_seconds": 30,
        "poll_interval_seconds": 0.5,
        "cancel_timeout_seconds": 5,
        "max_response_bytes": 65536,
        "adapter_configuration_digest": _SHA,
    }
    payload.update(overrides)
    return OpenCodeAdapterConfig.model_validate(payload)


def _task_request(fake: OpenCodeFakeServer, **config_overrides: object) -> TaskRequest:
    config = _config(fake, **config_overrides)
    return TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="run",
        capability_id="runtime.opencode.execute",
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest=_SHA,
            composition_digest="b" * 64,
            entrypoint="runtime.opencode.execute",
        ),
        attempt=1,
        input={"schema_version": "1"},
        binding_data=config.model_dump(mode="json"),
    )


class _ExactSecretPort:
    def __init__(self, authorized: dict[str, bytes]) -> None:
        self._authorized = dict(authorized)

    def resolve(self, handle: str) -> bytes:
        try:
            return bytes(self._authorized[handle])
        except KeyError as error:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error


def _workspace_identity() -> TaskWorkspaceIdentity:
    payload = {
        "task_id": "task-1",
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": ["qa/proposal.md"],
        "baseline_files": [],
        "project_digest": _SHA,
        "write_root_digest": "b" * 64,
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(**payload, identity_digest=canonical_digest(payload))


def _context(*, secrets: SecretPort | None = None, tmp_path: Path | None = None) -> TaskContext:
    project_root = (tmp_path or Path(".")).resolve()
    write_root = project_root / "qa/.staging/task-1/attempt-1"
    write_root.mkdir(parents=True, exist_ok=True)
    return TaskContext(
        project_root=project_root,
        write_root=write_root,
        workspace_identity=_workspace_identity(),
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest=_SHA,
            composition_digest="b" * 64,
            entrypoint="runtime.opencode.execute",
        ),
        secrets=secrets if secrets is not None else _ExactSecretPort({"opencode.token": _CANARY}),
    )


async def test_fingerprint_ignores_credential_shaped_provider_config() -> None:
    class _Client:
        async def get_server_identity(self) -> dict[str, object]:
            return {"healthy": True, "version": "1.18.4"}

        async def get_profile(self) -> dict[str, object]:
            return {"provider": {"note": "api_key=unused-provider-field"}}

    fake = OpenCodeFakeServer(profile=_profile())
    try:
        config = _config(fake)
        fingerprint = await _observe_fingerprint(
            _Client(),  # type: ignore[arg-type]
            config,
            b"runtime-token",
        )
    finally:
        fake.close()
    assert fingerprint["protocol_profile"] == "opencode-http-v1"
    assert "api_key=unused-provider-field" not in canonical_json_text(fingerprint)


async def test_preflight_authenticates_idempotent_prompt_profile() -> None:
    fake = OpenCodeFakeServer(profile=_profile(prompt_idempotency="conflict-on-body-drift"))
    try:
        fingerprint = await OpenCodeHandler().preflight(_task_request(fake), _context())
        assert fingerprint["prompt_admission"] == "caller-message-id-v1"
        assert "secret" not in canonical_json_text(fingerprint)
        assert _SECRET_TEXT not in canonical_json_text(fingerprint)
        assert fake.count("GET", "/global/health") == 1
        assert fake.count("GET", "/config") == 1
        recorded = fake.records[0]
        assert recorded.method == "GET"
        assert recorded.path == "/global/health"
        assert recorded.body_digest == hashlib.sha256(b"").hexdigest()
    finally:
        fake.close()


async def test_preflight_rejects_undeclared_secret_handle() -> None:
    fake = OpenCodeFakeServer(profile=_profile())
    try:
        with pytest.raises(SecretHandleUnauthorized):
            await OpenCodeHandler().preflight(
                _task_request(fake),
                _context(secrets=_ExactSecretPort({})),
            )
        assert fake.records == ()
    finally:
        fake.close()


class _RecordingProxy:
    def __init__(self) -> None:
        self.hits = 0
        self._lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                with proxy._lock:
                    proxy.hits += 1
                body = b'{"healthy":false,"via":"proxy"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_CONNECT(self) -> None:
                self.do_GET()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def test_http_test_servers_use_short_shutdown_polling_with_real_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector_type = getattr(socketserver, "_ServerSelector")
    select = selector_type.select
    observed_timeouts: list[float | None] = []

    def record_select(self: object, timeout: float | None = None) -> object:
        observed_timeouts.append(timeout)
        return select(self, timeout)

    monkeypatch.setattr(selector_type, "select", record_select)

    fake = OpenCodeFakeServer(profile=_profile())
    try:
        connection = http.client.HTTPConnection("127.0.0.1", fake._server.server_address[1])
        try:
            connection.request("GET", "/global/health")
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == {"healthy": True, "version": "opencode-http-v1"}
        finally:
            connection.close()
    finally:
        fake.close()
    assert observed_timeouts and all(timeout is not None and timeout <= 0.02 for timeout in observed_timeouts)

    observed_timeouts.clear()
    proxy = _RecordingProxy()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", proxy._server.server_address[1])
        try:
            connection.request("GET", "/global/health")
            response = connection.getresponse()
            assert response.status == 200
            assert response.read() == b'{"healthy":false,"via":"proxy"}'
        finally:
            connection.close()
    finally:
        proxy.close()
    assert observed_timeouts and all(timeout is not None and timeout <= 0.02 for timeout in observed_timeouts)


async def test_http_client_ignores_ambient_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = OpenCodeFakeServer(profile=_profile())
    proxy = _RecordingProxy()
    try:
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            monkeypatch.setenv(key, proxy.url)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        config = _config(fake)
        client = OpenCodeHttpClient(config, secret=_CANARY)
        try:
            payload = await client.get_server_identity()
        finally:
            await client.aclose()
        assert payload == {"healthy": True, "version": "opencode-http-v1"}
        assert fake.count("GET", "/global/health") == 1
        assert config.origin.rstrip("/") == fake.base_url.rstrip("/")
        assert config.origin.rstrip("/") != proxy.url.rstrip("/")
        assert proxy.hits == 0
    finally:
        fake.close()
        proxy.close()


async def test_preflight_rejects_cross_origin_redirect() -> None:
    fake = OpenCodeFakeServer(
        profile=_profile(),
        redirect_location="http://example.com/steal",
    )
    try:
        with pytest.raises(ValidationError, match="origin"):
            await OpenCodeHandler().preflight(_task_request(fake), _context())
    finally:
        fake.close()


async def test_preflight_rejects_unsupported_prompt_admission() -> None:
    fake = OpenCodeFakeServer(profile=_profile(prompt_idempotency="accepts-message-id-only"))
    try:
        with pytest.raises(ValidationError, match="prompt"):
            await OpenCodeHandler().preflight(_task_request(fake), _context())
    finally:
        fake.close()


@pytest.mark.parametrize(
    "cut",
    [
        "before_request",
        "after_provider_mutation",
        "before_response",
        "malformed_response",
        "sse_gap",
        "polling_lag",
    ],
)
def test_fake_server_accepts_documented_cuts(cut: str) -> None:
    fake = OpenCodeFakeServer(profile=_profile(), cut=cut)
    try:
        assert fake.cut == cut
        assert fake.supported_cuts() == (
            "before_request",
            "after_provider_mutation",
            "before_response",
            "malformed_response",
            "sse_gap",
            "polling_lag",
        )
    finally:
        fake.close()
