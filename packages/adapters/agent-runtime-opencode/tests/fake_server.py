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
PromptCut = Literal[
    "before_prompt_post",
    "after_admission_before_response",
    "after_lost_success_response",
]
TerminalMode = Literal[
    "artifact_array_busy",
    "busy",
    "idle_only",
    "mixed_result_busy",
    "success",
    "success_busy",
    "open_tools",
    "error",
    "canceled",
]
SseMode = Literal[
    "heartbeat",
    "gap",
    "fast_idle",
    "silent",
    "cursor",
    "malformed_identity",
    "wrong_session",
    "drip",
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
_SUPPORTED_PROMPT_CUTS: tuple[PromptCut, ...] = (
    "before_prompt_post",
    "after_admission_before_response",
    "after_lost_success_response",
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
        prompt_cut: PromptCut | None = None,
    ) -> None:
        if cut is not None and cut not in _SUPPORTED_CUTS:
            raise ValueError(f"unsupported cut: {cut}")
        if create_cut is not None and create_cut not in _SUPPORTED_CREATE_CUTS:
            raise ValueError(f"unsupported create cut: {create_cut}")
        if prompt_cut is not None and prompt_cut not in _SUPPORTED_PROMPT_CUTS:
            raise ValueError(f"unsupported prompt cut: {prompt_cut}")
        self.profile = profile
        self.cut = cut
        self.create_cut = create_cut
        self.prompt_cut = prompt_cut
        self.redirect_location = redirect_location
        self.project_scope = project_scope
        self.hide_sessions = hide_sessions
        self.metadata = dict(metadata or {})
        self.create_bodies: list[dict[str, object]] = []
        self.prompt_bodies: list[dict[str, object]] = []
        self.title_update_bodies: list[dict[str, object]] = []
        self.title_update_response_session_id: str | None = None
        self.reject_title_updates = False
        self.directories: list[str] = []
        self.sse_cursors: list[str | None] = []
        self.terminal_mode: TerminalMode = "success"
        self.sse_mode: SseMode = "heartbeat"
        self.omit_status = False
        self.sse_silent_seconds = 2.0
        self.structured_result: object = {"ok": True}
        self.error_message = "provider failed"
        self.diff_payload: object | None = None
        self.pending_permissions: list[dict[str, object]] = []
        self.path_faults: dict[str, str] = {}
        self._reject_all = False
        self._created_ids: list[str] = []
        self._sessions: dict[str, dict[str, object]] = {}
        self._messages: dict[str, dict[str, dict[str, object]]] = {}
        self._seq = 0
        self._records: list[RecordedCall] = []
        self._lock = threading.Lock()
        self._mutated = False
        self._abort_calls = 0
        self._sse_release = threading.Event()
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
    def prompt_posts(self) -> int:
        return sum(
            1 for item in self.records if item.method == "POST" and item.path.endswith("/prompt_async")
        )

    @property
    def abort_calls(self) -> int:
        with self._lock:
            return self._abort_calls

    @property
    def generated_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._created_ids)

    def count(self, method: str, path: str) -> int:
        return sum(1 for item in self.records if item.method == method and item.path == path)

    def close(self) -> None:
        self._sse_release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def reject_all_requests(self) -> None:
        with self._lock:
            self._reject_all = True

    def fault_on(self, path: str, fault: str) -> None:
        with self._lock:
            self.path_faults[path] = fault

    def accepted_message_count(self, message_id: str) -> int:
        with self._lock:
            return sum(1 for stored in self._messages.values() if message_id in stored)

    def plant_message(self, session_id: str, message_id: str, body: dict[str, object]) -> None:
        with self._lock:
            self._messages.setdefault(session_id, {})[message_id] = dict(body)

    def add_session(
        self,
        *,
        session_id: str | None = None,
        metadata: dict[str, object] | None = None,
        parent_id: str | None = None,
        agent: str = "build",
    ) -> dict[str, object]:
        with self._lock:
            self._seq += 1
            sid = session_id or f"ses_seed_{self._seq}"
            payload: dict[str, object] = {
                "id": sid,
                "title": "seed",
                "directory": self.project_scope,
                "agent": agent,
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

    def session_title(self, session_id: str) -> str | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            title = session.get("title")
            return title if isinstance(title, str) else None

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
        directory = (query.get("directory") or [None])[0]
        if isinstance(directory, str):
            with self._lock:
                self.directories.append(directory)
        length = int(handler.headers.get("Content-Length", "0") or "0")
        body = handler.rfile.read(length) if length > 0 else b""
        if self.cut == "before_request":
            handler.close_connection = True
            handler.connection.close()
            return
        with self._lock:
            reject_all = self._reject_all
            path_fault = self.path_faults.get(path)
        if reject_all:
            self._record(handler.command, path, body)
            self._disconnect(handler)
            return
        self._record(handler.command, path, body)
        if path_fault == "malformed_response":
            self._write_raw(handler, 200, b"{")
            return
        if path_fault == "oversized_response":
            self._write_raw(handler, 200, b"x" * 70_000)
            return
        if path_fault == "http_500":
            self._write_json(handler, 500, {"error": "internal"})
            return
        if path_fault == "disconnect":
            self._disconnect(handler)
            return
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
            self._write_sse(handler, query)
            return
        if path == "/session/status":
            if self.cut == "polling_lag":
                time.sleep(0.05)
            self._write_json(handler, 200, self._status_map())
            return
        if path == "/permission" and handler.command == "GET":
            with self._lock:
                permissions = [dict(item) for item in self.pending_permissions]
            self._write_json(handler, 200, permissions)
            return
        if path == "/session" and handler.command == "GET":
            directory = (query.get("directory") or [None])[0]
            if directory != self.project_scope or self.hide_sessions:
                payload: object = []
            else:
                with self._lock:
                    payload = list(self._sessions.values())
            self._write_json(handler, 200, payload)
            return
        if path.startswith("/session/") and path != "/session/status":
            self._handle_session_resource(handler, path, body)
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
                "agent": parsed.get("agent"),
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
            return self._status_map()
        return {"ok": True}

    def _handle_session_resource(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        body: bytes,
    ) -> None:
        remainder = path.removeprefix("/session/")
        parts = remainder.split("/")
        session_id = parts[0]
        if len(parts) == 1:
            if handler.command == "PATCH":
                self._handle_title_update(handler, session_id, body)
                return
            if handler.command != "GET":
                self._write_json(handler, 405, {"error": "method not allowed"})
                return
            with self._lock:
                session = self._sessions.get(session_id)
            if session is None:
                self._write_json(handler, 404, {"error": "not found"})
                return
            self._write_json(handler, 200, self._session_view(session))
            return
        if parts[-1] == "prompt_async" and handler.command == "POST":
            self._handle_prompt(handler, session_id, body)
            return
        if parts[-1] == "abort" and handler.command == "POST":
            self._handle_abort(handler, session_id)
            return
        if parts[-1] == "diff" and handler.command == "GET":
            if self.diff_payload is None:
                self._write_json(handler, 404, {"error": "not found"})
                return
            self._write_json(handler, 200, self.diff_payload)
            return
        if "message" in parts and handler.command == "GET":
            self._handle_messages(handler, session_id, parts)
            return
        self._write_json(handler, 404, {"error": "not found"})

    def _handle_title_update(
        self,
        handler: BaseHTTPRequestHandler,
        session_id: str,
        body: bytes,
    ) -> None:
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        with self._lock:
            self.title_update_bodies.append(parsed)
            reject = self.reject_title_updates
            session = self._sessions.get(session_id)
        if reject:
            self._write_json(handler, 500, {"error": "title update rejected"})
            return
        if session is None:
            self._write_json(handler, 404, {"error": "not found"})
            return
        if set(parsed) != {"title"}:
            self._write_json(handler, 400, {"error": "title update accepts only title"})
            return
        title = parsed.get("title")
        if not isinstance(title, str) or not title:
            self._write_json(handler, 400, {"error": "title is required"})
            return
        with self._lock:
            session["title"] = title
            view = self._session_view(session)
            response_session_id = self.title_update_response_session_id
        if response_session_id is not None:
            view["id"] = response_session_id
        self._write_json(handler, 200, view)

    def _handle_prompt(self, handler: BaseHTTPRequestHandler, session_id: str, body: bytes) -> None:
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        with self._lock:
            self.prompt_bodies.append(parsed)
            cut = self.prompt_cut
            self.prompt_cut = None
        if cut == "before_prompt_post":
            self._disconnect(handler)
            return
        admitted = self._admit_message(session_id, parsed)
        if admitted == "conflict":
            self._write_json(handler, 409, {"error": "prompt identity conflict"})
            return
        if cut == "after_admission_before_response":
            self._disconnect(handler)
            return
        if cut == "after_lost_success_response":
            self._write_raw(handler, 200, b"{")
            return
        self._write_raw(handler, 204, b"")

    def _admit_message(self, session_id: str, parsed: dict[str, object]) -> str:
        message_id = parsed.get("messageID")
        if not isinstance(message_id, str) or not message_id:
            return "conflict"
        with self._lock:
            stored = self._messages.setdefault(session_id, {})
            existing = stored.get(message_id)
            if existing is None:
                stored[message_id] = dict(parsed)
                return "created"
            if json.dumps(existing, sort_keys=True) == json.dumps(parsed, sort_keys=True):
                return "reuse"
            return "conflict"

    def _handle_abort(self, handler: BaseHTTPRequestHandler, session_id: str) -> None:
        del session_id
        with self._lock:
            self._abort_calls += 1
        self._write_json(handler, 200, True)

    def _handle_messages(
        self,
        handler: BaseHTTPRequestHandler,
        session_id: str,
        parts: list[str],
    ) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            stored = dict(self._messages.get(session_id, {}))
        if session is None:
            self._write_json(handler, 404, {"error": "not found"})
            return
        records = self._message_records(session_id, stored)
        if len(parts) >= 3 and parts[1] == "message":
            message_id = parts[2]
            for record in records:
                info = record.get("info")
                if isinstance(info, dict) and info.get("id") == message_id:
                    self._write_json(handler, 200, record)
                    return
            planted = stored.get(message_id)
            if planted is not None:
                self._write_json(handler, 200, planted)
                return
            self._write_json(handler, 404, {"error": "not found"})
            return
        self._write_json(handler, 200, records)

    def _message_records(
        self,
        session_id: str,
        stored: dict[str, dict[str, object]],
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for message_id, body in stored.items():
            records.append(
                {
                    "info": {"id": message_id, "role": "user"},
                    "parts": body.get("parts", []),
                    "admission": body,
                }
            )
        records.extend(self._synthetic_terminal_messages(session_id))
        return records

    def _synthetic_terminal_messages(self, session_id: str) -> list[dict[str, object]]:
        del session_id
        mode = self.terminal_mode
        if mode == "busy" or mode == "idle_only":
            return []
        if mode == "artifact_array_busy":
            return [
                {
                    "info": {"id": "msg_artifact_write", "role": "assistant"},
                    "parts": [
                        {
                            "type": "tool",
                            "tool": "artifact_write",
                            "state": {
                                "status": "completed",
                                "input": {
                                    "content": json.dumps(
                                        [
                                            {
                                                "mrc_id": "MRC-API-001",
                                                "required": True,
                                            }
                                        ]
                                    )
                                },
                            },
                        }
                    ],
                },
                {
                    "info": {"id": "msg_pending_continuation", "role": "assistant"},
                    "parts": [],
                },
            ]
        if mode == "mixed_result_busy":
            return [
                {
                    "info": {"id": "msg_valid_receipt", "role": "assistant"},
                    "parts": [{"type": "text", "text": json.dumps({"ok": True})}],
                },
                {
                    "info": {"id": "msg_later_artifact", "role": "assistant"},
                    "parts": [
                        {
                            "type": "tool",
                            "tool": "artifact_write",
                            "state": {
                                "status": "completed",
                                "input": {
                                    "content": json.dumps({"artifact": "minimum-coverage-matrix.json"})
                                },
                            },
                        }
                    ],
                },
            ]
        if mode == "error":
            return [
                {
                    "info": {
                        "id": "msg_terminal_error",
                        "role": "assistant",
                        "error": {"name": "ProviderError", "message": self.error_message},
                    },
                    "parts": [],
                }
            ]
        if mode == "canceled":
            return [
                {
                    "info": {
                        "id": "msg_terminal_canceled",
                        "role": "assistant",
                        "error": {"name": "Aborted", "message": "session aborted"},
                    },
                    "parts": [],
                }
            ]
        parts: list[dict[str, object]] = [
            {"type": "reasoning", "text": "internal chain-of-thought"},
            {"type": "text", "text": json.dumps(self.structured_result)},
        ]
        if mode == "open_tools":
            parts.append({"type": "tool", "state": {"status": "running"}})
        return [
            {
                "info": {
                    "id": "msg_terminal_result",
                    "role": "assistant",
                    "cost": 1.25,
                    "token_count": 9,
                    "model_history": ["hidden"],
                },
                "parts": parts,
            }
        ]

    def _status_map(self) -> dict[str, object]:
        with self._lock:
            session_ids = list(self._sessions)
            omit = self.omit_status
            mode = self.terminal_mode
        if omit:
            return {}
        status_type = (
            "busy" if mode in {"artifact_array_busy", "busy", "mixed_result_busy", "success_busy"} else "idle"
        )
        return {session_id: {"type": status_type} for session_id in session_ids}

    def _session_view(self, session: dict[str, object]) -> dict[str, object]:
        view = dict(session)
        if self.terminal_mode == "error":
            view["error"] = {"name": "ProviderError", "message": self.error_message}
        if self.terminal_mode == "canceled":
            view["aborted"] = True
        return view

    def _write_json(self, handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
        self._write_raw(handler, status, json.dumps(payload).encode("utf-8"))

    def _write_raw(self, handler: BaseHTTPRequestHandler, status: int, payload: bytes) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _write_sse(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        cursor = (query.get("cursor") or [None])[0]
        with self._lock:
            self.sse_cursors.append(cursor)
            mode = self.sse_mode
            session_id = next(iter(self._sessions), "ses_unknown")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        try:
            if self.cut == "sse_gap" or mode == "gap":
                handler.wfile.write(b": keepalive\n\n")
            elif mode == "silent":
                self._sse_release.wait(timeout=self.sse_silent_seconds)
            elif mode == "malformed_identity":
                handler.wfile.write(b'data: {"type":"session.idle","properties":{"sessionID":1}}\n\n')
            elif mode == "wrong_session":
                handler.wfile.write(
                    b'data: {"type":"session.idle","properties":{"sessionID":"ses_other"}}\n\n'
                )
            elif mode == "cursor":
                if not cursor:
                    payload = (
                        f'id: cursor-1\ndata: {{"type":"session.status",'
                        f'"properties":{{"sessionID":{json.dumps(session_id)}}}}}\n\n'
                    )
                else:
                    payload = (
                        f'id: cursor-2\ndata: {{"type":"session.idle",'
                        f'"properties":{{"sessionID":{json.dumps(session_id)}}}}}\n\n'
                    )
                handler.wfile.write(payload.encode("utf-8"))
            elif mode == "drip":
                while not self._sse_release.wait(timeout=0.05):
                    handler.wfile.write(b"event: server.heartbeat\ndata: {}\n\n")
                    handler.wfile.flush()
            elif mode == "fast_idle":
                payload = (
                    f'id: cursor-1\ndata: {{"type":"session.idle",'
                    f'"properties":{{"sessionID":{json.dumps(session_id)}}}}}\n\n'
                )
                handler.wfile.write(payload.encode("utf-8"))
            else:
                handler.wfile.write(b"event: server.heartbeat\ndata: {}\n\n")
            handler.wfile.flush()
        except OSError:
            return
        handler.close_connection = True


def _make_handler(fake: OpenCodeFakeServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            fake.handle(self)

        def do_POST(self) -> None:
            fake.handle(self)

        def do_PATCH(self) -> None:
            fake.handle(self)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler
