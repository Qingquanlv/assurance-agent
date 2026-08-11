import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml

from assurance_agent import resources
from assurance_agent.workflow.driver.adapter import Adapter, DriverError, PhaseRequest
from assurance_agent.workflow.driver.opencode_adapter import (
    OpenCodeAdapter,
    SANDBOX_ESCAPE_TOOLS,
    auth_headers_from_env,
    parse_model,
    validate_bounded_agent_catalog,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest


def _packaged_agent_entry(name: str) -> dict[str, Any]:
    raw = resources.read_text("opencode", "agents", f"{name}.md")
    frontmatter, separator, prompt = raw[4:].partition("\n---\n")
    assert separator
    metadata = yaml.safe_load(frontmatter + "\n")
    assert isinstance(metadata, dict)
    permissions: list[dict[str, str]] = []
    tools = metadata.get("tools")
    assert isinstance(tools, dict)
    for tool, enabled in tools.items():
        if enabled is False:
            permissions.append({"permission": tool, "pattern": "*", "action": "deny"})
    declared_permissions = metadata.get("permission")
    assert isinstance(declared_permissions, dict)
    for permission, policy in declared_permissions.items():
        if isinstance(policy, str):
            permissions.append({"permission": permission, "pattern": "*", "action": policy})
            continue
        assert isinstance(policy, dict)
        permissions.extend(
            {"permission": permission, "pattern": pattern, "action": action}
            for pattern, action in policy.items()
        )
    return {
        "name": name,
        # OpenCode preserves YAML's clip chomping on folded block scalars.
        "description": str(metadata["description"]),
        "mode": metadata["mode"],
        "prompt": prompt.rstrip("\r\n"),
        "tools": None,
        "permission": permissions,
    }


def _request() -> PhaseRequest:
    return PhaseRequest(
        change_id="CH-1", phase_id="explore", skill="aa-explore", agent="aa-doc-author", prompt="p"
    )


def _client(handler, *, timeout: float = 5.0) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)


class _StatusScript:
    """Serves POST /session, POST prompt_async (204), and a scripted GET status."""

    def __init__(
        self,
        statuses: list[str | dict[str, Any]],
        session_id: str = "ses_1",
        messages: list[dict[str, Any]] | None = None,
        abort_status: int = 204,
    ) -> None:
        self._statuses = statuses
        self._sid = session_id
        self._messages = messages or []
        self._abort_status = abort_status
        self.status_calls = 0
        self.abort_calls = 0
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path == "/agent":
            return httpx.Response(
                200,
                json=[
                    _packaged_agent_entry(name)
                    for name in (
                        "aa-archiver",
                        "aa-doc-author",
                        "aa-explorer",
                        "aa-intake-host",
                        "aa-reporter",
                        "aa-reviewer",
                        "aa-test-author",
                    )
                ],
            )
        if path == "/session":
            return httpx.Response(200, json={"id": self._sid})
        if path == f"/session/{self._sid}/prompt_async":
            return httpx.Response(204)
        if path == "/session/status":
            i = min(self.status_calls, len(self._statuses) - 1)
            self.status_calls += 1
            st = self._statuses[i]
            if isinstance(st, dict):
                return httpx.Response(200, json={self._sid: st})
            return httpx.Response(200, json=({self._sid: {"type": st}} if st != "idle" else {}))
        if path == f"/session/{self._sid}/message":
            return httpx.Response(200, json=self._messages)
        if path == f"/session/{self._sid}/abort":
            self.abort_calls += 1
            return httpx.Response(self._abort_status, json=self._abort_status < 300)
        return httpx.Response(404)


def _fast(**kw: Any) -> dict[str, Any]:
    """Adapter with no-op sleep + short streak for deterministic polling tests."""
    return {"idle_done_streak": 2, "sleep": lambda _s: None, **kw}


def test_opencode_adapter_satisfies_protocol() -> None:
    adapter = OpenCodeAdapter("http://x", "/dir", client=_client(lambda r: httpx.Response(200)))
    assert isinstance(adapter, Adapter)


def test_run_phase_success_after_sustained_idle() -> None:
    script = _StatusScript(["busy", "busy", "idle", "idle"])
    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(script), **_fast())
    result = adapter.run_phase(_request())
    assert result.ok is True
    assert result.output == ""  # artifacts are on disk; text is intentionally empty
    assert script.abort_calls == 0


@pytest.mark.parametrize(
    "retry_status",
    [
        {
            "type": "retry",
            "attempt": 7,
            "message": "AccountQuotaExceeded: quota exhausted",
            "next": 99_999,
        },
        {
            "type": "retry",
            "attempt": 2,
            "message": "Too Many Requests",
            "next": 99_999,
        },
        {
            "type": "retry",
            "attempt": 1,
            "message": "Usage limit reached",
            "action": {"reason": "account_rate_limit"},
            "next": 99_999,
        },
    ],
)
def test_invoke_returns_rate_limit_immediately_for_account_retry_metadata(
    tmp_path: Path,
    retry_status: dict[str, Any],
) -> None:
    script = _StatusScript([retry_status, "busy", "idle", "idle"])
    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(script), **_fast())

    result = adapter.invoke(
        AgentRequest(
            target="skill:aa-explore",
            node_id="explore",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=("change:explore/**",),
            prompt="p",
            timeout_seconds=30.0,
            agent="aa-explorer",
        )
    )

    assert result.ok is False
    assert result.error_kind == "rate_limit"
    assert script.status_calls == 1
    assert script.abort_calls == 1


def test_invoke_keeps_polling_for_ordinary_overloaded_retry(tmp_path: Path) -> None:
    script = _StatusScript(
        [
            {
                "type": "retry",
                "attempt": 3,
                "message": "Provider is overloaded",
                "next": 99_999,
            },
            "busy",
            "idle",
            "idle",
        ]
    )
    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(script), **_fast())

    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            agent="custom-agent",
        )
    )

    assert result.ok is True
    assert script.status_calls == 4
    assert script.abort_calls == 0


def test_invoke_reports_assistant_401_as_auth_instead_of_success(tmp_path: Path) -> None:
    """A provider APIError must not degrade into a later missing-output retry."""
    script = _StatusScript(
        ["busy", "idle", "idle"],
        messages=[
            {
                "info": {
                    "id": "msg_assistant",
                    "role": "assistant",
                    "modelID": "deepseek-v4-flash",
                    "providerID": "anthropic",
                    "agent": "aa-explorer",
                    "error": {
                        "name": "APIError",
                        "data": {
                            "message": "Unauthorized: API key is missing or invalid",
                            "statusCode": 401,
                            "isRetryable": False,
                        },
                    },
                },
                "parts": [],
            }
        ],
    )
    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(script), **_fast())
    result = adapter.invoke(
        AgentRequest(
            target="skill:aa-explore",
            node_id="explore",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=("change:explore/**",),
            prompt="p",
            timeout_seconds=30.0,
            agent="aa-explorer",
        )
    )

    assert result.ok is False
    assert result.error_kind == "auth"
    assert result.session_id == "ses_1"
    assert result.error is not None and "Unauthorized" in result.error
    assert script.abort_calls == 1


def test_lone_idle_between_tool_rounds_does_not_finish_early() -> None:
    # busy → a single transient idle (streak resets on next busy) → busy → idle idle.
    script = _StatusScript(["busy", "idle", "busy", "idle", "idle"])
    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(script), **_fast())
    result = adapter.run_phase(_request())
    assert result.ok is True
    # Proves we did NOT return on the first (index-1) idle: needed >=5 status polls.
    assert script.status_calls >= 5


def test_never_busy_requires_longer_idle_streak() -> None:
    # Slow-to-start session that never reports busy needs streak target + 4.
    script = _StatusScript(["idle"] * 10)
    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(script), **_fast())
    result = adapter.run_phase(_request())
    assert result.ok is True
    assert script.status_calls >= 6  # idle_done_streak (2) + 4


def test_dispatch_uses_prompt_async_with_directory_model_agent_and_auth() -> None:
    script = _StatusScript(["idle"] * 10)
    adapter = OpenCodeAdapter(
        "http://host/",
        "/sut",
        model="anthropic/claude",
        parent_session="ses_parent",
        auth_headers={"Authorization": "Basic zzz"},
        client=_client(script),
        **_fast(),
    )
    adapter.run_phase(_request())

    create = script.seen[0]
    dispatch = script.seen[1]
    assert create.url.path == "/session"
    assert dispatch.url.path == "/session/ses_1/prompt_async"  # NOT the blocking /message
    assert create.url.params["directory"] == "/sut"
    assert dispatch.url.params["directory"] == "/sut"
    assert dispatch.headers["Authorization"] == "Basic zzz"
    create_body = json.loads(create.content)
    assert create_body["title"] == "Phase explore"
    assert create_body["parentID"] == "ses_parent"
    dispatch_body = json.loads(dispatch.content)
    assert dispatch_body["model"] == {"providerID": "anthropic", "modelID": "claude"}
    assert dispatch_body["agent"] == "aa-doc-author"
    dispatched_text = dispatch_body["parts"][0]["text"]
    assert dispatched_text.startswith("<system-reminder>\n")
    assert dispatched_text.endswith("\n</system-reminder>")
    assert "\np\n" in dispatched_text


def test_dispatch_keeps_non_bounded_agent_prompt_unchanged() -> None:
    script = _StatusScript(["idle"] * 10)
    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(script), **_fast())

    adapter._dispatch_prompt("ses_1", "search for a file", agent="custom-agent")

    import json

    dispatch_body = json.loads(script.seen[0].content)
    assert dispatch_body["parts"] == [{"type": "text", "text": "search for a file"}]


def test_invoke_uses_request_local_model_instead_of_constructor_fallback(
    tmp_path: Path,
) -> None:
    script = _StatusScript(["busy", "idle", "idle"])
    adapter = OpenCodeAdapter(
        "http://host",
        "/sut",
        model="anthropic/deepseek-v4-flash",
        client=_client(script),
        **_fast(),
    )

    result = adapter.invoke(
        AgentRequest(
            target="skill:aa-case-design",
            node_id="case-design",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=("change:cases/**",),
            prompt="p",
            timeout_seconds=30.0,
            agent="aa-doc-author",
            model="anthropic/glm-5.2",
            model_route_source="skill_route",
        )
    )

    import json

    dispatch = next(request for request in script.seen if request.url.path == "/session/ses_1/prompt_async")
    assert result.ok is True
    assert json.loads(dispatch.content)["model"] == {
        "providerID": "anthropic",
        "modelID": "glm-5.2",
    }


def test_invoke_without_request_model_omits_constructor_fallback(tmp_path: Path) -> None:
    script = _StatusScript(["busy", "idle", "idle"])
    adapter = OpenCodeAdapter(
        "http://host",
        "/sut",
        model="anthropic/deepseek-v4-flash",
        client=_client(script),
        **_fast(),
    )

    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            agent="custom-agent",
        )
    )

    import json

    dispatch = next(request for request in script.seen if request.url.path == "/session/ses_1/prompt_async")
    assert result.ok is True
    assert "model" not in json.loads(dispatch.content)


@pytest.mark.parametrize(("status", "expected"), [(400, "invalid_input"), (404, "invalid_input")])
def test_explicit_request_model_rejection_is_invalid_input(
    tmp_path: Path, status: int, expected: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        if request.url.path == "/session/ses_1/prompt_async":
            return httpx.Response(status, text="unknown model")
        if request.url.path == "/session/ses_1/abort":
            return httpx.Response(204)
        return httpx.Response(404)

    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(handler), **_fast())
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            model="anthropic/missing",
        )
    )

    assert result.ok is False
    assert result.error_kind == expected


def test_prompt_400_without_explicit_model_stays_internal(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        if request.url.path == "/session/ses_1/abort":
            return httpx.Response(204)
        return httpx.Response(400, text="malformed prompt")

    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(handler), **_fast())
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
        )
    )

    assert result.ok is False
    assert result.error_kind == "internal"


def test_async_explicit_model_rejection_is_invalid_input(tmp_path: Path) -> None:
    script = _StatusScript(
        ["busy", "idle", "idle"],
        messages=[
            {
                "info": {
                    "role": "assistant",
                    "error": {
                        "data": {
                            "message": "model not found",
                            "statusCode": 404,
                        }
                    },
                },
                "parts": [],
            }
        ],
    )
    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(script), **_fast())

    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            model="anthropic/missing",
        )
    )

    assert result.ok is False
    assert result.error_kind == "invalid_input"


def test_create_session_failure_returns_not_ok() -> None:
    adapter = OpenCodeAdapter(
        "http://host", "/sut", client=_client(lambda r: httpx.Response(500, text="boom")), **_fast()
    )
    result = adapter.run_phase(_request())
    assert result.ok is False
    assert result.error is not None and "create session failed" in result.error


def test_prompt_dispatch_failure_returns_not_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        if request.url.path == "/session/ses_1/abort":
            return httpx.Response(204)
        return httpx.Response(502, text="upstream")

    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(handler), **_fast())
    result = adapter.run_phase(_request())
    assert result.ok is False
    assert result.error is not None and "prompt failed" in result.error


def test_phase_times_out_when_never_idle() -> None:
    class _Clock:
        def __init__(self, step: float) -> None:
            self.t = 0.0
            self.step = step

        def __call__(self) -> float:
            v = self.t
            self.t += self.step
            return v

    script = _StatusScript(["busy"] * 100)
    adapter = OpenCodeAdapter(
        "http://host",
        "/sut",
        client=_client(script),
        idle_done_streak=2,
        poll_max_s=3.0,
        sleep=lambda _s: None,
        monotonic=_Clock(1.0),
    )
    result = adapter.run_phase(_request())
    assert result.ok is False
    assert result.error is not None and "timed out" in result.error
    assert script.abort_calls == 1


def test_invoke_aborts_once_after_transport_failure(tmp_path: Path) -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        if request.url.path == "/session/ses_1/prompt_async":
            return httpx.Response(204)
        if request.url.path == "/session/status":
            raise httpx.ConnectError("provider connection dropped", request=request)
        if request.url.path == "/session/ses_1/abort":
            return httpx.Response(204)
        return httpx.Response(404)

    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(handler), **_fast())
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            agent="custom-agent",
        )
    )

    assert result.ok is False
    assert result.error_kind == "transport"
    assert seen_paths.count("/session/ses_1/abort") == 1


def test_invoke_aborts_once_when_messages_json_is_malformed(tmp_path: Path) -> None:
    script = _StatusScript(["busy", "idle", "idle"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/ses_1/message":
            return httpx.Response(200, text="{not-json")
        return script(request)

    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(handler), **_fast())
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            agent="custom-agent",
        )
    )

    assert result.ok is False
    assert result.error_kind == "internal"
    assert result.error is not None and "messages returned invalid JSON" in result.error
    assert script.abort_calls == 1


def test_invoke_aborts_once_after_unexpected_post_create_failure(tmp_path: Path) -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        if request.url.path == "/session/ses_1/prompt_async":
            raise RuntimeError("injected client failed unexpectedly")
        if request.url.path == "/session/ses_1/abort":
            return httpx.Response(204)
        return httpx.Response(404)

    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(handler), **_fast())
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            agent="custom-agent",
        )
    )

    assert result.ok is False
    assert result.error_kind == "internal"
    assert result.error is not None and "injected client failed unexpectedly" in result.error
    assert seen_paths.count("/session/ses_1/abort") == 1


@pytest.mark.parametrize("cancel_type", [KeyboardInterrupt, SystemExit])
def test_invoke_aborts_once_then_reraises_process_cancellation(
    tmp_path: Path,
    cancel_type: type[BaseException],
) -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        if request.url.path == "/session/ses_1/prompt_async":
            raise cancel_type()
        if request.url.path == "/session/ses_1/abort":
            return httpx.Response(204)
        return httpx.Response(404)

    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(handler), **_fast())

    with pytest.raises(cancel_type):
        adapter.invoke(
            AgentRequest(
                target="skill:custom",
                node_id="custom",
                change_id="CH-1",
                workspace_root=tmp_path,
                allowed_writes=(),
                prompt="p",
                timeout_seconds=30.0,
                agent="custom-agent",
            )
        )

    assert seen_paths.count("/session/ses_1/abort") == 1


def test_invoke_fails_closed_and_aborts_for_non_mapping_status_payload(tmp_path: Path) -> None:
    script = _StatusScript(["busy"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            return httpx.Response(200, json=[{"type": "busy"}])
        return script(request)

    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(handler), **_fast())
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            agent="custom-agent",
        )
    )

    assert result.ok is False
    assert result.error_kind == "internal"
    assert result.error is not None and "non-mapping payload" in result.error
    assert script.abort_calls == 1


def test_abort_failure_makes_original_error_non_retryable_and_preserves_context(
    tmp_path: Path,
) -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        if request.url.path == "/session/ses_1/prompt_async":
            return httpx.Response(429, text="provider quota exhausted")
        if request.url.path == "/session/ses_1/abort":
            return httpx.Response(503, text="abort control plane unavailable")
        return httpx.Response(404)

    adapter = OpenCodeAdapter("http://host", "/sut", client=_client(handler), **_fast())
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            agent="custom-agent",
        )
    )

    assert result.ok is False
    assert result.error_kind == "internal"
    assert result.error is not None
    assert "prompt failed (429): provider quota exhausted" in result.error
    assert "abort failed (503): abort control plane unavailable" in result.error
    assert seen_paths.count("/session/ses_1/abort") == 1


def test_abort_request_uses_bounded_cleanup_timeout(tmp_path: Path) -> None:
    abort_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        if request.url.path == "/session/ses_1/prompt_async":
            return httpx.Response(429, text="provider quota exhausted")
        if request.url.path == "/session/ses_1/abort":
            timeout = request.extensions.get("timeout")
            assert isinstance(timeout, dict)
            read_timeout = timeout.get("read")
            assert isinstance(read_timeout, float)
            abort_timeouts.append(read_timeout)
            return httpx.Response(204)
        return httpx.Response(404)

    adapter = OpenCodeAdapter(
        "http://host",
        "/sut",
        client=_client(handler, timeout=3600.0),
        **_fast(),
    )
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            agent="custom-agent",
        )
    )

    assert result.ok is False
    assert result.error_kind == "rate_limit"
    assert abort_timeouts == [30.0]


def test_poll_request_timeout_never_exceeds_remaining_agent_deadline(tmp_path: Path) -> None:
    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            value = self.now
            self.now += 0.1
            return value

    script = _StatusScript(["busy", "idle", "idle"])
    poll_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            timeout = request.extensions.get("timeout")
            assert isinstance(timeout, dict)
            read_timeout = timeout.get("read")
            assert isinstance(read_timeout, float)
            poll_timeouts.append(read_timeout)
        return script(request)

    adapter = OpenCodeAdapter(
        "http://host",
        "/sut",
        client=_client(handler),
        idle_done_streak=2,
        sleep=lambda _seconds: None,
        monotonic=_Clock(),
    )
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=1.0,
            agent="custom-agent",
        )
    )

    assert result.ok is True
    assert len(poll_timeouts) == 3
    assert all(0.0 < timeout < 1.0 for timeout in poll_timeouts)
    assert poll_timeouts == sorted(poll_timeouts, reverse=True)
    assert script.abort_calls == 0


def test_poll_request_timeout_does_not_expand_injected_client_cap(tmp_path: Path) -> None:
    script = _StatusScript(["busy", "idle", "idle"])
    poll_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            timeout = request.extensions.get("timeout")
            assert isinstance(timeout, dict)
            read_timeout = timeout.get("read")
            assert isinstance(read_timeout, float)
            poll_timeouts.append(read_timeout)
        return script(request)

    adapter = OpenCodeAdapter(
        "http://host",
        "/sut",
        client=_client(handler, timeout=0.25),
        idle_done_streak=2,
        sleep=lambda _seconds: None,
    )
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            agent="custom-agent",
        )
    )

    assert result.ok is True
    assert poll_timeouts == [0.25, 0.25, 0.25]
    assert script.abort_calls == 0


def test_polling_remains_compatible_with_narrow_injected_fake_client(tmp_path: Path) -> None:
    class _NarrowFakeClient:
        def __init__(self) -> None:
            self.statuses = ["busy", "idle", "idle"]
            self.status_calls = 0
            self.abort_calls = 0

        def request(
            self,
            method: str,
            url: str,
            *,
            params: Any,
            json: Any,
            headers: Any,
        ) -> httpx.Response:
            del method, params, json, headers
            path = httpx.URL(url).path
            if path == "/session":
                return httpx.Response(200, json={"id": "ses_1"})
            if path == "/session/ses_1/prompt_async":
                return httpx.Response(204)
            if path == "/session/status":
                status = self.statuses[self.status_calls]
                self.status_calls += 1
                payload = {"ses_1": {"type": status}} if status != "idle" else {}
                return httpx.Response(200, json=payload)
            if path == "/session/ses_1/message":
                return httpx.Response(200, json=[])
            if path == "/session/ses_1/abort":
                self.abort_calls += 1
                return httpx.Response(204)
            return httpx.Response(404)

    fake = _NarrowFakeClient()
    adapter = OpenCodeAdapter(
        "http://host",
        "/sut",
        client=cast(httpx.Client, fake),
        idle_done_streak=2,
        sleep=lambda _seconds: None,
    )
    result = adapter.invoke(
        AgentRequest(
            target="skill:custom",
            node_id="custom",
            change_id="CH-1",
            workspace_root=tmp_path,
            allowed_writes=(),
            prompt="p",
            timeout_seconds=30.0,
            agent="custom-agent",
        )
    )

    assert result.ok is True
    assert fake.status_calls == 3
    assert fake.abort_calls == 0


def test_parse_model_valid_and_invalid() -> None:
    assert parse_model("prov/mod") == {"providerID": "prov", "modelID": "mod"}
    assert parse_model(None) is None
    for bad in ("no-slash", "/leading", "trailing/"):
        with pytest.raises(DriverError):
            parse_model(bad)


def test_auth_headers_from_env() -> None:
    assert auth_headers_from_env({}) == {}
    headers = auth_headers_from_env({"OPENCODE_SERVER_USERNAME": "u", "OPENCODE_SERVER_PASSWORD": "p"})
    assert headers["Authorization"].startswith("Basic ")
    alias = auth_headers_from_env({"AA_OPENCODE_USERNAME": "u", "AA_OPENCODE_PASSWORD": "p"})
    assert alias == headers


def test_bounded_agent_catalog_rejects_stale_or_delegating_runtime_policy() -> None:
    agent = _packaged_agent_entry("aa-doc-author")
    agent["permission"] = [rule for rule in agent["permission"] if rule.get("permission") != "call_omo_agent"]

    with pytest.raises(DriverError, match="call_omo_agent.*restart OpenCode"):
        validate_bounded_agent_catalog([agent], ("aa-doc-author",))


def test_bounded_agent_catalog_rejects_runtime_missing_process_escape_denies() -> None:
    agent = _packaged_agent_entry("aa-doc-author")
    agent["permission"] = [
        rule
        for rule in agent["permission"]
        if rule.get("permission") not in {"skill_mcp", "interactive_bash", "monitor_start"}
    ]

    with pytest.raises(DriverError, match="skill_mcp.*interactive_bash.*monitor_start"):
        validate_bounded_agent_catalog([agent], ("aa-doc-author",))


def test_bounded_agent_catalog_accepts_explicitly_disabled_escape_tools() -> None:
    agent = _packaged_agent_entry("aa-doc-author")
    denied_tools = {*SANDBOX_ESCAPE_TOOLS, "workflow_start"}
    agent["permission"] = [rule for rule in agent["permission"] if rule["permission"] not in denied_tools]
    agent["tools"] = {tool: False for tool in denied_tools}

    validate_bounded_agent_catalog([agent], ("aa-doc-author",))


def test_bounded_agent_catalog_accepts_tools_migrated_to_permission_denies() -> None:
    catalog = [_packaged_agent_entry("aa-doc-author")]

    validate_bounded_agent_catalog(catalog, ("aa-doc-author",))


def test_bounded_agent_catalog_accepts_live_block_scalar_description() -> None:
    agent = _packaged_agent_entry("aa-intake-host")
    assert agent["description"].endswith("\n")

    validate_bounded_agent_catalog([agent], ("aa-intake-host",))


def test_bounded_agent_catalog_accepts_server_defaults_and_tool_output_grant() -> None:
    agent = _packaged_agent_entry("aa-doc-author")
    agent["permission"] = [
        {"permission": "question", "pattern": "*", "action": "allow"},
        *agent["permission"],
        {
            "permission": "external_directory",
            "pattern": "/tmp/opencode/tool-output/**",
            "action": "allow",
        },
    ]

    validate_bounded_agent_catalog([agent], ("aa-doc-author",))


def test_bounded_agent_catalog_rejects_stale_prompt_or_metadata() -> None:
    agent = _packaged_agent_entry("aa-doc-author")
    agent["prompt"] += "\nstale permissive instruction"

    with pytest.raises(DriverError, match="prompt.*restart OpenCode"):
        validate_bounded_agent_catalog([agent], ("aa-doc-author",))


@pytest.mark.parametrize(
    ("field", "value"),
    [("description", "stale description"), ("mode", "primary")],
)
def test_bounded_agent_catalog_rejects_stale_selection_metadata(
    field: str,
    value: str,
) -> None:
    agent = _packaged_agent_entry("aa-doc-author")
    agent[field] = value

    with pytest.raises(DriverError, match=rf"{field}.*restart OpenCode"):
        validate_bounded_agent_catalog([agent], ("aa-doc-author",))


def test_bounded_agent_catalog_rejects_extra_unsafe_permission_rule() -> None:
    agent = _packaged_agent_entry("aa-doc-author")
    agent["permission"].append({"permission": "bash", "pattern": "*", "action": "allow"})

    with pytest.raises(DriverError, match="permission.*restart OpenCode"):
        validate_bounded_agent_catalog([agent], ("aa-doc-author",))


def test_bounded_agent_catalog_rejects_duplicate_agent_name() -> None:
    agent = _packaged_agent_entry("aa-doc-author")

    with pytest.raises(DriverError, match="duplicate.*aa-doc-author.*restart OpenCode"):
        validate_bounded_agent_catalog([agent, dict(agent)], ("aa-doc-author",))
