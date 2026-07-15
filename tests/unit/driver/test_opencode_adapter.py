import httpx
import pytest
from typing import Any

from assurance_agent.workflow.driver.adapter import Adapter, DriverError, PhaseRequest
from assurance_agent.workflow.driver.opencode_adapter import (
    OpenCodeAdapter,
    auth_headers_from_env,
    parse_model,
)


def _request() -> PhaseRequest:
    return PhaseRequest(
        change_id="CH-1", phase_id="explore", skill="aa-explore", agent="aa-doc-author", prompt="p"
    )


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class _StatusScript:
    """Serves POST /session, POST prompt_async (204), and a scripted GET status."""

    def __init__(self, statuses: list[str], session_id: str = "ses_1") -> None:
        self._statuses = statuses
        self._sid = session_id
        self.status_calls = 0
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path == "/session":
            return httpx.Response(200, json={"id": self._sid})
        if path == f"/session/{self._sid}/prompt_async":
            return httpx.Response(204)
        if path == "/session/status":
            i = min(self.status_calls, len(self._statuses) - 1)
            self.status_calls += 1
            st = self._statuses[i]
            return httpx.Response(200, json=({self._sid: {"type": st}} if st != "idle" else {}))
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
    assert dispatch_body["parts"] == [{"type": "text", "text": "p"}]


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
