"""OpenCode server HTTP adapter (spec 5a).

Endpoints/payloads transcribed from the CURRENT TS opencode_adapter.ts:
- POST /session                    {title, parentID?}                 -> {id}
- POST /session/{id}/prompt_async  {parts:[{type:'text',text}], model?, agent?} -> 204
- GET  /session/status             -> { "<sid>": {"type": "busy|retry"} | "idle" }
- GET  /session/{id}/message       -> [{info:{role,error?}, parts:[...]}]

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

BOUNDED_OPENCODE_AGENTS = (
    "aa-archiver",
    "aa-doc-author",
    "aa-explorer",
    "aa-intake-host",
    "aa-reporter",
    "aa-reviewer",
    "aa-test-author",
)
DELEGATION_ESCAPE_TOOLS = ("task", "call_omo_agent", "look_at")


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


def _classify_prompt_status(status_code: int, *, model_was_explicit: bool) -> ErrorKind:
    if model_was_explicit and status_code in (400, 404):
        return "invalid_input"
    return _classify_http_status(status_code)


def _shield_bounded_prompt(prompt: str, agent: str | None) -> str:
    """Keep third-party keyword hooks from rewriting bounded AA instructions.

    OpenCode plugins conventionally exclude ``system-reminder`` blocks from
    keyword-derived mode injection.  AA graph prompts contain unavoidable words
    such as the ``aa-explore`` skill name and source-search prohibitions; without
    this boundary a plugin can prepend contradictory delegation/search commands
    after the runtime has already validated the worker policy.
    """
    if agent not in BOUNDED_OPENCODE_AGENTS:
        return prompt
    return f"<system-reminder>\n{prompt}\n</system-reminder>"


def auth_headers_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = env if env is not None else os.environ
    user = env.get("OPENCODE_SERVER_USERNAME") or env.get("AA_OPENCODE_USERNAME")
    password = env.get("OPENCODE_SERVER_PASSWORD") or env.get("AA_OPENCODE_PASSWORD")
    if not user or not password:
        return {}
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def validate_bounded_agent_catalog(payload: Any, agent_names: tuple[str, ...]) -> None:
    """Fail closed unless live OpenCode workers disable delegation escape tools.

    Project agent files can be refreshed while a long-running OpenCode server
    still serves cached or globally shadowed definitions.  The live ``/agent``
    catalog is therefore the authority for this preflight, not the files on
    disk.  ``task`` is OpenCode-native; ``call_omo_agent`` and ``look_at`` are
    plugin tools that can spawn child sessions outside the worker's permission
    floor.
    """
    if not isinstance(payload, list):
        raise _OpenCodeCallError("internal", "opencode /agent returned a non-list payload")
    by_name = {
        item.get("name"): item
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for agent_name in agent_names:
        agent = by_name.get(agent_name)
        if not isinstance(agent, dict):
            raise _OpenCodeCallError(
                "internal",
                f"bounded OpenCode agent {agent_name!r} is missing; refresh agents and restart OpenCode",
            )
        tools = agent.get("tools")
        permissions = agent.get("permission")

        def explicitly_disabled(tool: str) -> bool:
            if isinstance(tools, dict) and tools.get(tool) is False:
                return True
            action: str | None = None
            if isinstance(permissions, list):
                for rule in permissions:
                    if not isinstance(rule, dict) or rule.get("permission") != tool:
                        continue
                    if rule.get("pattern") not in (None, "*"):
                        continue
                    candidate = rule.get("action")
                    action = candidate if isinstance(candidate, str) else None
            return action == "deny"

        unsafe = [tool for tool in DELEGATION_ESCAPE_TOOLS if not explicitly_disabled(tool)]
        if unsafe:
            joined = ", ".join(unsafe)
            raise _OpenCodeCallError(
                "internal",
                f"bounded OpenCode agent {agent_name!r} has stale/unsafe tools ({joined}); "
                "refresh agents and restart OpenCode before running the workflow",
            )


def validate_bounded_agent_server(server: str, directory: str) -> None:
    """Validate all packaged worker policies against a live OpenCode server."""
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{server.rstrip('/')}/agent",
                params={"directory": directory},
                headers=auth_headers_from_env(),
            )
    except httpx.TransportError as exc:
        raise _OpenCodeCallError("transport", f"opencode agent preflight failed: {exc}") from exc
    if not 200 <= response.status_code < 300:
        raise _OpenCodeCallError(
            _classify_http_status(response.status_code),
            f"opencode agent preflight failed ({response.status_code}): {response.text[:300]}",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise _OpenCodeCallError("internal", "opencode /agent returned invalid JSON") from exc
    validate_bounded_agent_catalog(payload, BOUNDED_OPENCODE_AGENTS)


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
        self._validated_agent_policies: set[tuple[str, str]] = set()

    def _request(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        *,
        directory: str | None = None,
    ) -> httpx.Response:
        return self._client.request(
            method,
            f"{self._base}{path}",
            params={"directory": directory if directory is not None else self._directory},
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

    def _validate_live_agent_policy(self, agent: str | None, *, directory: str) -> None:
        if agent not in BOUNDED_OPENCODE_AGENTS:
            return
        key = (directory, agent)
        if key in self._validated_agent_policies:
            return
        resp = self._request("GET", "/agent", directory=directory)
        if not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_http_status(resp.status_code),
                f"opencode agent preflight failed ({resp.status_code}): {resp.text[:300]}",
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise _OpenCodeCallError("internal", "opencode /agent returned invalid JSON") from exc
        validate_bounded_agent_catalog(payload, (agent,))
        self._validated_agent_policies.add(key)

    def _dispatch_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        agent: str | None = None,
        model: dict[str, str] | None = None,
        directory: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": _shield_bounded_prompt(prompt, agent)}]}
        if model is not None:
            body["model"] = model
        if agent:
            body["agent"] = agent
        resp = self._request("POST", f"/session/{session_id}/prompt_async", json=body, directory=directory)
        if resp.status_code != 204 and not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_prompt_status(
                    resp.status_code,
                    model_was_explicit=model is not None,
                ),
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

    def _raise_on_session_error(
        self,
        session_id: str,
        *,
        model_was_explicit: bool,
        directory: str | None = None,
    ) -> None:
        """Surface the terminal assistant API error hidden behind an idle session.

        ``prompt_async`` returns before the provider call.  Provider failures are
        therefore recorded on the assistant message while ``/session/status``
        simply becomes idle; treating idle as success turns auth/rate-limit errors
        into misleading artifact-missing failures downstream.
        """
        resp = self._request("GET", f"/session/{session_id}/message", directory=directory)
        if not 200 <= resp.status_code < 300:
            raise _OpenCodeCallError(
                _classify_http_status(resp.status_code),
                f"opencode messages failed ({resp.status_code}): {resp.text[:300]}",
            )
        payload = resp.json() if resp.content else []
        if not isinstance(payload, list):
            raise _OpenCodeCallError("internal", "opencode messages returned a non-list payload")
        for message in reversed(payload):
            if not isinstance(message, dict):
                continue
            info = message.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            error = info.get("error")
            if error is None:
                return
            error_data = error.get("data") if isinstance(error, dict) else None
            status_code = error_data.get("statusCode") if isinstance(error_data, dict) else None
            kind = (
                _classify_prompt_status(
                    status_code,
                    model_was_explicit=model_was_explicit,
                )
                if isinstance(status_code, int)
                else "internal"
            )
            detail = error_data.get("message") if isinstance(error_data, dict) else None
            if not isinstance(detail, str) or not detail.strip():
                detail = str(error)[:500]
            raise _OpenCodeCallError(kind, f"opencode assistant error: {detail[:500]}")

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        try:
            session_id = self._create_session(f"Phase {request.phase_id}", self._parent)
            self._dispatch_prompt(
                session_id,
                request.prompt,
                agent=request.agent,
                model=self._model,
            )
            self._await_idle(session_id)
            self._raise_on_session_error(
                session_id,
                model_was_explicit=self._model is not None,
            )
        except DriverError as err:
            return PhaseResult(ok=False, output="", error=str(err))
        # Output is written to artifacts by the agent; GraphRuntime commits the
        # attempt outcome and routes gates from the ledger projection.
        return PhaseResult(ok=True, output="")

    def invoke(self, request: AgentRequest) -> AgentResult:
        """graph AgentInvoker：每个请求都以 task 私有 workspace root 为 directory。"""
        directory = str(request.workspace_root)
        session_id: str | None = None
        try:
            self._validate_live_agent_policy(request.agent, directory=directory)
            session_id = self._create_session(
                f"Phase {request.node_id}",
                request.reconnect_session_id or self._parent,
                directory=directory,
            )
            self._dispatch_prompt(
                session_id,
                request.prompt,
                agent=request.agent,
                model=parse_model(request.model),
                directory=directory,
            )
            self._await_idle(session_id, directory=directory, poll_max=request.timeout_seconds)
            self._raise_on_session_error(
                session_id,
                model_was_explicit=request.model is not None,
                directory=directory,
            )
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
