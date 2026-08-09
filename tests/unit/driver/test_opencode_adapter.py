import httpx
import pytest
from typing import Any
from pathlib import Path

from assurance_agent.workflow.driver.adapter import Adapter, DriverError, PhaseRequest
from assurance_agent.workflow.driver.opencode_adapter import (
    OpenCodeAdapter,
    auth_headers_from_env,
    parse_model,
    validate_bounded_agent_catalog,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest


def _request() -> PhaseRequest:
    return PhaseRequest(
        change_id="CH-1", phase_id="explore", skill="aa-explore", agent="aa-doc-author", prompt="p"
    )


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class _StatusScript:
    """Serves POST /session, POST prompt_async (204), and a scripted GET status."""

    def __init__(
        self,
        statuses: list[str],
        session_id: str = "ses_1",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self._statuses = statuses
        self._sid = session_id
        self._messages = messages or []
        self.status_calls = 0
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path == "/agent":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": name,
                        "tools": {"task": False, "call_omo_agent": False, "look_at": False},
                    }
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
            return httpx.Response(200, json=({self._sid: {"type": st}} if st != "idle" else {}))
        if path == f"/session/{self._sid}/message":
            return httpx.Response(200, json=self._messages)
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

    import json

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
    catalog = [
        {
            "name": "aa-doc-author",
            "tools": None,
            "permission": [
                {"permission": "task", "pattern": "*", "action": "deny"},
                {"permission": "bash", "pattern": "aa risk *", "action": "allow"},
            ],
        }
    ]

    with pytest.raises(DriverError, match="call_omo_agent.*restart OpenCode"):
        validate_bounded_agent_catalog(catalog, ("aa-doc-author",))


def test_bounded_agent_catalog_accepts_explicitly_disabled_delegation_tools() -> None:
    catalog = [
        {
            "name": "aa-doc-author",
            "tools": {"task": False, "call_omo_agent": False, "look_at": False},
            "permission": [],
        }
    ]

    validate_bounded_agent_catalog(catalog, ("aa-doc-author",))


def test_bounded_agent_catalog_accepts_tools_migrated_to_permission_denies() -> None:
    catalog = [
        {
            "name": "aa-doc-author",
            "tools": None,
            "permission": [
                {"permission": "task", "pattern": "*", "action": "deny"},
                {"permission": "call_omo_agent", "pattern": "*", "action": "deny"},
                {"permission": "look_at", "pattern": "*", "action": "deny"},
            ],
        }
    ]

    validate_bounded_agent_catalog(catalog, ("aa-doc-author",))
