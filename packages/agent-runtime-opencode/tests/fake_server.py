from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal
from urllib.parse import urlparse

from agent_runtime_opencode.protocol import OpenCodeProtocolProfile


CutName = Literal[
    "before_request",
    "after_provider_mutation",
    "before_response",
    "malformed_response",
    "sse_gap",
    "polling_lag",
]
_SUPPORTED_CUTS: tuple[CutName, ...] = (
    "before_request",
    "after_provider_mutation",
    "before_response",
    "malformed_response",
    "sse_gap",
    "polling_lag",
)


@dataclass(frozen=True, slots=True)
class RecordedCall:
    method: str
    path: str
    body_digest: str


class OpenCodeFakeServer:
    def __init__(
        self,
        *,
        profile: OpenCodeProtocolProfile,
        cut: CutName | None = None,
        redirect_location: str | None = None,
        project_scope: str = "/tmp/attempt-workspace",
    ) -> None:
        if cut is not None and cut not in _SUPPORTED_CUTS:
            raise ValueError(f"unsupported cut: {cut}")
        self.profile = profile
        self.cut = cut
        self.redirect_location = redirect_location
        self.project_scope = project_scope
        self._records: list[RecordedCall] = []
        self._lock = threading.Lock()
        self._mutated = False
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"

    @staticmethod
    def supported_cuts() -> tuple[CutName, ...]:
        return _SUPPORTED_CUTS

    @property
    def records(self) -> tuple[RecordedCall, ...]:
        with self._lock:
            return tuple(self._records)

    def count(self, method: str, path: str) -> int:
        return sum(1 for item in self.records if item.method == method and item.path == path)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def _record(self, method: str, path: str, body: bytes) -> None:
        recorded = RecordedCall(
            method=method,
            path=path,
            body_digest=hashlib.sha256(body).hexdigest(),
        )
        with self._lock:
            self._records.append(recorded)

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        path = parsed.path
        length = int(handler.headers.get("Content-Length", "0") or "0")
        body = handler.rfile.read(length) if length > 0 else b""
        if self.cut == "before_request":
            handler.close_connection = True
            handler.connection.close()
            return
        self._record(handler.command, path, body)
        if self.cut == "after_provider_mutation":
            self._mutated = True
            handler.close_connection = True
            handler.connection.close()
            return
        if self.redirect_location:
            handler.send_response(302)
            handler.send_header("Location", self.redirect_location)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        if self.cut == "before_response":
            handler.close_connection = True
            handler.connection.close()
            return
        if self.cut == "malformed_response":
            payload = b"{"
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return
        if path == "/event":
            self._write_sse(handler)
            return
        if path == "/session/status" and self.cut == "polling_lag":
            time.sleep(0.05)
        payload = self._payload(path)
        encoded = json.dumps(payload).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)

    def _payload(self, path: str) -> object:
        if path == "/global/health":
            return {"healthy": True, "version": "opencode-http-v1"}
        if path == "/config":
            return {
                "protocol_profile": "opencode-http-v1",
                "prompt_admission": self.profile.prompt_admission,
                "prompt_idempotency": self.profile.prompt_idempotency,
                "metadata_supported": self.profile.metadata_supported,
                "sse_supported": self.profile.sse_supported,
                "poll_fallback_supported": self.profile.poll_fallback_supported,
            }
        if path == "/session":
            return []
        if path == "/session/status":
            return {}
        return {"ok": True}

    def _write_sse(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        if self.cut == "sse_gap":
            handler.wfile.write(b": keepalive\n\n")
            return
        handler.wfile.write(b"event: server.heartbeat\ndata: {}\n\n")


def _make_handler(fake: OpenCodeFakeServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            fake.handle(self)

        def do_POST(self) -> None:
            fake.handle(self)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler
