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

同时实现 graph 的 ``AgentInvoker``：v2 路径的每个请求都使用
``request.workspace_root`` 作为 ``?directory=``（task 私有 workspace），并把
失败归一化为 typed error kind（401/403 → ``auth``，429 → ``rate_limit``，网络
错误 → ``transport``，轮询超时 → ``timeout``）；成功时返回新建 session ID 供
reconnect 元数据使用。
"""

import base64
import os
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal

import httpx

from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.driver.adapter import DriverError, PhaseRequest, PhaseResult
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult

SessionStatus = Literal["busy", "retry", "idle"]

DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_POLL_MAX_S = 3600.0
DEFAULT_IDLE_DONE_STREAK = 8


class _OpenCodeCallError(DriverError):
    """带 typed ``ErrorKind`` 的 opencode 调用失败；v1 run_phase 仍按 DriverError 捕获。"""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind


def _classify_http_status(status_code: int) -> ErrorKind:
    if status_code in (401, 403):
        return "auth"
    if status_code == 429:
        return "rate_limit"
    return "internal"


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

    def _request(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        *,
        directory: str | None = None,
    ) -> httpx.Response:
        # When ``directory`` is provided (graph invoke / attempt workspace), never
        # fall back to the adapter host/SUT root. Omitting it keeps v1 ``run_phase``
        # on ``self._directory``.
        bound = self._directory if directory is None else directory
        if not bound:
            raise _OpenCodeCallError("internal", "opencode directory is required")
        return self._client.request(
            method,
            f"{self._base}{path}",
            params={"directory": bound},
            json=json,
            headers=self._headers,
        )

    def _create_session(
        self,
        title: str,
        parent: str | None,
        *,
        directory: str | None = None,
    ) -> str:
        body: dict[str, Any] = {"title": title}
        if parent:
            body["parentID"] = parent
        resp = self._request("POST", "/session", json=body, directory=directory)
        if not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_http_status(resp.status_code),
                f"opencode create session failed ({resp.status_code}): {resp.text[:300]}",
            )
        session_id = (resp.json() or {}).get("id")
        if not session_id:
            raise _OpenCodeCallError("internal", "opencode create session: missing id")
        return session_id

    def _dispatch_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        agent: str | None = None,
        directory: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
        if self._model:
            body["model"] = self._model
        if agent:
            body["agent"] = agent
        resp = self._request("POST", f"/session/{session_id}/prompt_async", json=body, directory=directory)
        if resp.status_code != 204 and not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_http_status(resp.status_code),
                f"opencode prompt failed ({resp.status_code}): {resp.text[:300]}",
            )

    def get_status(self, session_id: str, *, directory: str | None = None) -> SessionStatus:
        resp = self._request("GET", "/session/status", directory=directory)
        if not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_http_status(resp.status_code),
                f"opencode status failed ({resp.status_code}): {resp.text[:300]}",
            )
        payload = resp.json() if resp.content else {}
        return _session_status(payload, session_id)

    def _await_idle(
        self,
        session_id: str,
        *,
        directory: str | None = None,
        poll_max: float | None = None,
    ) -> None:
        limit = poll_max if poll_max is not None else self._poll_max
        deadline = self._monotonic() + limit
        saw_busy = False
        idle_streak = 0
        # Grace period so the session can flip to busy before we trust "idle".
        self._sleep(min(self._poll_interval, 1.5))
        while self._monotonic() < deadline:
            status = self.get_status(session_id, directory=directory)
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
        raise _OpenCodeCallError("timeout", f"opencode phase timed out after {limit}s (session {session_id})")

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        try:
            session_id = self._create_session(f"Phase {request.phase_id}", self._parent)
            self._dispatch_prompt(session_id, request.prompt, agent=request.agent)
            self._await_idle(session_id)
        except DriverError as err:
            return PhaseResult(ok=False, output="", error=str(err))
        # Output is written to artifacts by the agent; GraphRuntime commits the
        # attempt outcome and routes gates from the ledger projection.
        return PhaseResult(ok=True, output="")

    def invoke(self, request: AgentRequest) -> AgentResult:
        """graph AgentInvoker：每个请求都以 task 私有 workspace root 为 directory。

        Create, prompt, every status poll, and reconnect (parent session reuse)
        bind ``?directory=`` to ``request.workspace_root`` only — never the host
        SUT root, change root, or a prior attempt directory held on the adapter.
        """
        directory = str(request.workspace_root)
        session_id: str | None = None
        try:
            session_id = self._create_session(
                f"Phase {request.node_id}",
                request.reconnect_session_id or self._parent,
                directory=directory,
            )
            self._dispatch_prompt(session_id, request.prompt, agent=request.agent, directory=directory)
            self._await_idle(session_id, directory=directory, poll_max=request.timeout_seconds)
        except _OpenCodeCallError as exc:
            return AgentResult(ok=False, error_kind=exc.kind, error=str(exc), session_id=session_id)
        except httpx.TransportError as exc:
            return AgentResult(
                ok=False,
                error_kind="transport",
                error=f"opencode transport error: {exc}",
                session_id=session_id,
            )
        return AgentResult(ok=True, session_id=session_id)
