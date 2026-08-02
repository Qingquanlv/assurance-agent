import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from assurance_agent import resources
from assurance_agent.workflow.driver.adapter import Adapter, DriverError, PhaseRequest
from assurance_agent.workflow.driver.opencode_adapter import (
    OpenCodeAdapter,
    auth_headers_from_env,
    parse_model,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest
from assurance_agent.workflow.graph.assurance_personas import (
    ASSURANCE_PERSONA_BY_TARGET,
    expected_assurance_persona,
)
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2

_EXACT_PERSONA_TABLE: dict[str, str] = {
    "aa-api-plan": "aa-doc-author",
    "aa-e2e-plan": "aa-doc-author",
    "aa-fuzz-plan": "aa-doc-author",
    "aa-performance-plan": "aa-doc-author",
    "aa-api-plan-fixer": "aa-doc-author",
    "aa-e2e-plan-fixer": "aa-doc-author",
    "aa-api-plan-reviewer": "aa-reviewer",
    "aa-e2e-plan-reviewer": "aa-reviewer",
    "aa-fuzz-plan-reviewer": "aa-reviewer",
    "aa-performance-plan-reviewer": "aa-reviewer",
    "aa-api-codegen": "aa-test-author",
    "aa-e2e-codegen": "aa-test-author",
    "aa-fuzz-codegen": "aa-test-author",
    "aa-performance-codegen": "aa-test-author",
    "aa-api-codegen-fixer": "aa-test-author",
    "aa-e2e-codegen-fixer": "aa-test-author",
}

# Assurance-like skills that must stay outside the closed sixteen-target table.
_EXTRA_ASSURANCE_LIKE_TARGETS = (
    "aa-explore",
    "aa-case-design",
    "aa-case-reviewer",
    "aa-case-fixer",
    "aa-fuzz-plan-fixer",
    "aa-performance-plan-fixer",
    "aa-fuzz-codegen-fixer",
    "aa-performance-codegen-fixer",
    "aa-inspect",
    "aa-fix-proposal",
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


# ---------------------------------------------------------------------------
# Task 14: dormant exact persona registry + attempt-directory continuity
# ---------------------------------------------------------------------------


def test_assurance_persona_registry_is_exact_sixteen_targets() -> None:
    assert dict(ASSURANCE_PERSONA_BY_TARGET) == _EXACT_PERSONA_TABLE
    assert len(ASSURANCE_PERSONA_BY_TARGET) == 16
    assert set(ASSURANCE_PERSONA_BY_TARGET.values()) == {
        "aa-doc-author",
        "aa-reviewer",
        "aa-test-author",
    }
    for target in _EXTRA_ASSURANCE_LIKE_TARGETS:
        assert target not in ASSURANCE_PERSONA_BY_TARGET


@pytest.mark.parametrize(("target", "persona"), sorted(_EXACT_PERSONA_TABLE.items()))
def test_expected_assurance_persona_for_each_target(target: str, persona: str) -> None:
    assert expected_assurance_persona(target) == persona
    assert expected_assurance_persona(f"skill:{target}") == persona


@pytest.mark.parametrize("missing", ["", "aa-missing", "aa-case-reviewer", "skill:aa-explore"])
def test_expected_assurance_persona_rejects_missing_target(missing: str) -> None:
    with pytest.raises(KeyError, match="unknown assurance persona target"):
        expected_assurance_persona(missing)


def test_packaged_schema_personas_match_registry() -> None:
    schema = parse_workflow_v2(resources.read_text("schemas", "workflow-schema.yaml"))
    seen: dict[str, str] = {}
    for graph in schema.graphs.values():
        for node in graph.nodes.values():
            if not node.uses.startswith("skill:"):
                continue
            skill = node.uses.removeprefix("skill:")
            if skill not in ASSURANCE_PERSONA_BY_TARGET:
                continue
            expected = expected_assurance_persona(skill)
            assert node.agent == expected, f"{node.uses}: schema={node.agent!r} registry={expected!r}"
            seen[skill] = expected
    assert set(seen) == set(ASSURANCE_PERSONA_BY_TARGET)


def test_packaged_persona_documents_cover_registry_values() -> None:
    for persona in sorted(set(ASSURANCE_PERSONA_BY_TARGET.values())):
        text = resources.read_text("opencode", "agents", f"{persona}.md")
        parts = text.split("---", 2)
        assert len(parts) >= 3, persona
        frontmatter = parts[1]
        assert f"name: {persona}" in frontmatter


def _agent_request(workspace_root: Path, **overrides: object) -> AgentRequest:
    fields: dict[str, object] = {
        "target": "skill:aa-api-plan",
        "node_id": "plan",
        "change_id": "CH-1",
        "workspace_root": workspace_root,
        "allowed_writes": ("change:plans/**",),
        "prompt": "plan it",
        "timeout_seconds": 30.0,
        "agent": "aa-doc-author",
    }
    fields.update(overrides)
    return AgentRequest.model_validate(fields)


def _directory_of(request: httpx.Request) -> str:
    return request.url.params["directory"]


def test_invoke_create_prompt_status_and_reconnect_use_attempt_workspace_only(tmp_path: Path) -> None:
    host_root = "/sut-host"
    change_root = str(tmp_path / "change" / "CH-1")
    prior_attempt = str(tmp_path / "attempts" / "prior")
    attempt_a = tmp_path / "attempts" / "a"
    attempt_b = tmp_path / "attempts" / "b"
    attempt_a.mkdir(parents=True)
    attempt_b.mkdir(parents=True)
    Path(change_root).mkdir(parents=True)
    Path(prior_attempt).mkdir(parents=True)

    forbidden = {host_root, change_root, prior_attempt}

    first = _StatusScript(["idle"] * 10, session_id="ses_a")
    adapter = OpenCodeAdapter(
        "http://host",
        host_root,
        client=_client(first),
        **_fast(),
    )
    result_a = adapter.invoke(_agent_request(attempt_a))
    assert result_a.ok is True
    assert result_a.session_id == "ses_a"
    assert first.seen, "expected create/prompt/status traffic"
    for req in first.seen:
        directory = _directory_of(req)
        assert directory == str(attempt_a)
        assert directory not in forbidden
    paths_a = [req.url.path for req in first.seen]
    assert paths_a[0] == "/session"
    assert paths_a[1] == "/session/ses_a/prompt_async"
    assert "/session/status" in paths_a
    assert paths_a.count("/session/status") >= 2

    second = _StatusScript(["idle"] * 10, session_id="ses_b")
    adapter_reconnect = OpenCodeAdapter(
        "http://host",
        host_root,
        parent_session="ses_stale_parent",
        client=_client(second),
        **_fast(),
    )
    result_b = adapter_reconnect.invoke(
        _agent_request(attempt_b, reconnect_session_id="ses_a", node_id="plan-retry")
    )
    assert result_b.ok is True
    assert result_b.session_id == "ses_b"
    assert second.seen
    for req in second.seen:
        directory = _directory_of(req)
        assert directory == str(attempt_b)
        assert directory not in forbidden
        assert directory != str(attempt_a)
    create_body = json.loads(second.seen[0].content)
    assert create_body["parentID"] == "ses_a"
    assert _directory_of(second.seen[0]) == str(attempt_b)
    assert all(
        _directory_of(req) == str(attempt_b) for req in second.seen if req.url.path == "/session/status"
    )
