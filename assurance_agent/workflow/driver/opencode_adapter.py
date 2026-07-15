"""OpenCode server HTTP adapter (spec 5a).

Endpoints/payloads transcribed from the CURRENT TS opencode_adapter.ts:
- POST /session                    {title, parentID?}                 -> {id}
- POST /session/{id}/prompt_async  {parts:[{type:'text',text}], model?, agent?} -> 204
- GET  /session/status             -> { "<sid>": {"type": "busy|retry"} | "idle" }

The prompt is dispatched ASYNCHRONOUSLY and completion is detected by polling
/session/status for a SUSTAINED idle streak. A synchronous POST /message would
hold a connection open for the entire (multi-minute) agent run and trip the
server's ~300s header timeout ("fetch failed"), killing every long phase; and
treating the first idle as "done" aborts phases mid-flight because OpenCode
briefly drops a session from the status map between tool rounds.

Every request carries ?directory=<sut> and optional Basic-Auth headers derived
from the environment (never from CLI args). Only an *explicit* model is pinned;
otherwise the field is omitted so the server resolves its own default (TS
behavior — pinning a parent session's stale model breaks phases).
"""
import base64
import os
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal

import httpx

from assurance_agent.workflow.driver.adapter import DriverError, PhaseRequest, PhaseResult

SessionStatus = Literal["busy", "retry", "idle"]

DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_POLL_MAX_S = 3600.0
DEFAULT_IDLE_DONE_STREAK = 8


def auth_headers_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = env if env is not None else os.environ
    user = env.get("OPENCODE_SERVER_USERNAME") or env.get("AA_OPENCODE_USERNAME")
    password = env.get("OPENCODE_SERVER_PASSWORD") or env.get("AA_OPENCODE_PASSWORD")
    if not user or not password:
        return {}
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def parse_model(raw: str | dict[str, str] | None) -> dict[str, str] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    text = raw.strip()
    if not text:
        return None
    slash = text.find("/")
    if slash <= 0 or slash >= len(text) - 1:
        raise DriverError(f'--model must be "provider/model" (got "{raw}")')
    return {"providerID": text[:slash], "modelID": text[slash + 1 :]}


def _session_status(payload: Any, session_id: str) -> SessionStatus:
    mapping = payload if isinstance(payload, dict) else {}
    raw = mapping.get(session_id)
    if isinstance(raw, str):
        value = raw
    elif isinstance(raw, dict):
        value = raw.get("type")
    else:
        value = None
    if value in ("busy", "retry"):
        return value  # type: ignore[return-value]
    return "idle"


class OpenCodeAdapter:
    def __init__(
        self,
        server: str,
        directory: str,
        *,
        model: str | dict[str, str] | None = None,
        parent_session: str | None = None,
        auth_headers: dict[str, str] | None = None,
        client: httpx.Client | None = None,
        timeout: float = 3600.0,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_max_s: float = DEFAULT_POLL_MAX_S,
        idle_done_streak: int = DEFAULT_IDLE_DONE_STREAK,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = server.rstrip("/")
        self._directory = directory
        self._model = parse_model(model)
        self._parent = parent_session
        self._headers = auth_headers if auth_headers is not None else auth_headers_from_env()
        self._client = client or httpx.Client(timeout=timeout)
        self._poll_interval = poll_interval_s
        self._poll_max = poll_max_s
        self._idle_streak_target = idle_done_streak
        self._sleep = sleep
        self._monotonic = monotonic

    def _request(self, method: str, path: str, json: Any | None = None) -> httpx.Response:
        return self._client.request(
            method,
            f"{self._base}{path}",
            params={"directory": self._directory},
            json=json,
            headers=self._headers,
        )

    def _create_session(self, request: PhaseRequest) -> str:
        body: dict[str, Any] = {"title": f"Phase {request.phase_id}"}
        if self._parent:
            body["parentID"] = self._parent
        resp = self._request("POST", "/session", json=body)
        if not 200 <= resp.status_code < 300:
            raise DriverError(
                f"opencode create session failed ({resp.status_code}): {resp.text[:300]}"
            )
        session_id = (resp.json() or {}).get("id")
        if not session_id:
            raise DriverError("opencode create session: missing id")
        return session_id

    def _dispatch_prompt(self, session_id: str, request: PhaseRequest) -> None:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": request.prompt}]}
        if self._model:
            body["model"] = self._model
        if request.agent:
            body["agent"] = request.agent
        resp = self._request("POST", f"/session/{session_id}/prompt_async", json=body)
        if resp.status_code != 204 and not 200 <= resp.status_code < 300:
            raise DriverError(f"opencode prompt failed ({resp.status_code}): {resp.text[:300]}")

    def get_status(self, session_id: str) -> SessionStatus:
        resp = self._request("GET", "/session/status")
        if not 200 <= resp.status_code < 300:
            raise DriverError(f"opencode status failed ({resp.status_code}): {resp.text[:300]}")
        payload = resp.json() if resp.content else {}
        return _session_status(payload, session_id)

    def _await_idle(self, session_id: str) -> None:
        deadline = self._monotonic() + self._poll_max
        saw_busy = False
        idle_streak = 0
        # Grace period so the session can flip to busy before we trust "idle".
        self._sleep(min(self._poll_interval, 1.5))
        while self._monotonic() < deadline:
            status = self.get_status(session_id)
            if status in ("busy", "retry"):
                saw_busy = True
                idle_streak = 0
            else:
                idle_streak += 1
                # Never-busy sessions may be slow to start; require a longer streak.
                need = self._idle_streak_target if saw_busy else self._idle_streak_target + 4
                if idle_streak >= need:
                    return
            self._sleep(self._poll_interval)
        raise DriverError(
            f"opencode phase timed out after {self._poll_max}s (session {session_id})"
        )

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        try:
            session_id = self._create_session(request)
            self._dispatch_prompt(session_id, request)
            self._await_idle(session_id)
        except DriverError as err:
            return PhaseResult(ok=False, output="", error=str(err))
        # Output is written to artifacts by the agent; the loop next commits the
        # signed attempt outcome. Gate routing remains inside compute_status.
        return PhaseResult(ok=True, output="")
