from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal
from urllib.parse import parse_qs, urlparse

from agent_runtime_opencode.protocol import OpenCodeProtocolProfile


CutName = Literal[
    "before_request",
    "after_provider_mutation",
    "before_response",
    "malformed_response",
    "sse_gap",
    "polling_lag",
]
CreateCut = Literal[
    "before_create",
    "after_create_before_response",
    "invalid_success_body",
    "proxy_reset",
]
_SUPPORTED_CUTS: tuple[CutName, ...] = (
    "before_request",
    "after_provider_mutation",
    "before_response",
    "malformed_response",
    "sse_gap",
    "polling_lag",
)
_SUPPORTED_CREATE_CUTS: tuple[CreateCut, ...] = (
    "before_create",
    "after_create_before_response",
    "invalid_success_body",
    "proxy_reset",
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
        create_cut: CreateCut | None = None,
        redirect_location: str | None = None,
        project_scope: str = "/tmp/attempt-workspace",
        existing_matches: int = 0,
        metadata: dict[str, object] | None = None,
        hide_sessions: bool = False,
    ) -> None:
        if cut is not None and cut not in _SUPPORTED_CUTS:
            raise ValueError(f"unsupported cut: {cut}")
        if create_cut is not None and create_cut not in _SUPPORTED_CREATE_CUTS:
            raise ValueError(f"unsupported create cut: {create_cut}")
        self.profile = profile
        self.cut = cut
        self.create_cut = create_cut
        self.redirect_location = redirect_location
        self.project_scope = project_scope
        self.hide_sessions = hide_sessions
        self.metadata = dict(metadata or {})
        self.create_bodies: list[dict[str, object]] = []
        self._created_ids: list[str] = []
        self._sessions: dict[str, dict[str, object]] = {}
        self._seq = 0
        self._records: list[RecordedCall] = []
        self._lock = threading.Lock()
        self._mutated = False
        for index in range(existing_matches):
            self.add_session(session_id=f"ses_existing_{index + 1}", metadata=self.metadata)
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

    @property
    def create_calls(self) -> int:
        return self.count("POST", "/session")

    @property
    def generated_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._created_ids)

    def count(self, method: str, path: str) -> int:
        return sum(1 for item in self.records if item.method == method and item.path == path)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def add_session(
        self,
        *,
        session_id: str | None = None,
        metadata: dict[str, object] | None = None,
        parent_id: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            self._seq += 1
            sid = session_id or f"ses_seed_{self._seq}"
            payload: dict[str, object] = {
                "id": sid,
                "title": "seed",
                "directory": self.project_scope,
                "metadata": dict(metadata if metadata is not None else self.metadata),
            }
            if parent_id:
                payload["parentID"] = parent_id
            self._sessions[sid] = payload
            return dict(payload)

    def drop_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def set_session_metadata(self, session_id: str, metadata: dict[str, object]) -> None:
        with self._lock:
            session = self._sessions[session_id]
            session["metadata"] = dict(metadata)

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
        query = parse_qs(parsed.query)
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
            self._write_raw(handler, 200, b"{")
            return
        if handler.command == "POST" and path == "/session":
            self._handle_create(handler, body)
            return
        if path == "/event":
            self._write_sse(handler)
            return
        if path == "/session/status" and self.cut == "polling_lag":
            time.sleep(0.05)
        if path == "/session" and handler.command == "GET":
            directory = (query.get("directory") or [None])[0]
            if directory != self.project_scope or self.hide_sessions:
                payload: object = []
            else:
                with self._lock:
                    payload = list(self._sessions.values())
            self._write_json(handler, 200, payload)
            return
        if handler.command == "GET" and path.startswith("/session/") and path != "/session/status":
            session_id = path.removeprefix("/session/")
            if "/" in session_id:
                self._write_json(handler, 200, {"ok": True})
                return
            with self._lock:
                session = self._sessions.get(session_id)
            if session is None:
                self._write_json(handler, 404, {"error": "not found"})
                return
            self._write_json(handler, 200, session)
            return
        payload = self._payload(path)
        self._write_json(handler, 200, payload)

    def _handle_create(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        with self._lock:
            self.create_bodies.append(parsed)
        if self.create_cut == "before_create":
            self._disconnect(handler)
            return
        session = self._new_created_session(parsed)
        if self.create_cut in {"after_create_before_response", "proxy_reset"}:
            self._disconnect(handler)
            return
        if self.create_cut == "invalid_success_body":
            self._write_raw(handler, 200, b"{")
            return
        self._write_json(handler, 200, session)

    def _new_created_session(self, parsed: dict[str, object]) -> dict[str, object]:
        with self._lock:
            self._seq += 1
            session_id = f"ses_generated_{self._seq}"
            self._created_ids.append(session_id)
            session: dict[str, object] = {
                "id": session_id,
                "title": parsed.get("title"),
                "directory": self.project_scope,
                "metadata": parsed.get("metadata"),
            }
            self._sessions[session_id] = session
            return dict(session)

    def _disconnect(self, handler: BaseHTTPRequestHandler) -> None:
        handler.close_connection = True
        handler.connection.close()

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

    def _write_json(self, handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
        self._write_raw(handler, status, json.dumps(payload).encode("utf-8"))

    def _write_raw(self, handler: BaseHTTPRequestHandler, status: int, payload: bytes) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

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
