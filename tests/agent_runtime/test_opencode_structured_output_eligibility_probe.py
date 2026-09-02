"""Scripted-HTTP tests for the standalone OpenCode structured-output eligibility probe.

The probe is a standalone two-phase CLI (``pre-restart`` / ``post-restart``) that lives at
``scripts/opencode_structured_output_eligibility_probe.py`` and talks to a direct native
OpenCode loopback origin with ``httpx``. These tests drive it through a scripted
``httpx.MockTransport`` -- no live OpenCode server is required. They exercise real probe
behaviour (arg parsing, endpoint/file/auth validation, streaming size bounds, redirect
refusal, request-directory scoping, atomic state/report emission) rather than mocking the
probe's own internals.

The probe deliberately does not import ``graph_engine``, ``agent_runtime_contracts``,
``agent_runtime_opencode``, a Capability package, or Product code, so this module loads it
by file path instead of as a workspace import.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import sys
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE_PATH = _REPO_ROOT / "scripts" / "opencode_structured_output_eligibility_probe.py"
_FIXTURE_PATH = _REPO_ROOT / "tests" / "fixtures" / "opencode" / "v1.18.26-message-roundtrip-red.json"


def _load_probe() -> Any:
    spec = importlib.util.spec_from_file_location("opencode_s0_eligibility_probe", _PROBE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot build import spec for the eligibility probe")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve string annotations under
    # ``from __future__ import annotations`` (it looks the module up in sys.modules).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


VALID_COMMIT = "774cc7c1914e4329eefde5a669f938b0cf566661"
WORKSPACE_DIRECTORY = "/opt/opencode-s0/candidate-workspace"
PROVIDER = "anthropic"
MODEL = "claude-3-5-sonnet-20241022"
EXPECTED_VERSION = "1.18.26"


# --------------------------------------------------------------------------------------
# Scripted HTTP transport
# --------------------------------------------------------------------------------------


class Scripted:
    """A deterministic scripted OpenCode HTTP transport.

    Routes are matched on ``(method, path_regex)`` and consume queued responses in order,
    reusing the last queued response once a queue drains. Every request is recorded so
    tests can assert directory scoping, auth headers, and provider-prompt counts.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._routes: list[tuple[str, re.Pattern[str], deque[dict[str, Any]]]] = []

    def add(self, method: str, path_regex: str, *responses: Mapping[str, Any]) -> "Scripted":
        self._routes.append((method, re.compile(path_regex), deque(dict(r) for r in responses)))
        return self

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def count(self, method: str, path_regex: str) -> int:
        pattern = re.compile(path_regex)
        return sum(1 for r in self.requests if r["method"] == method and pattern.fullmatch(r["path"]))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        directory = request.url.params.get("directory")
        body: Any = None
        raw = request.content
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = raw
        self.requests.append(
            {
                "method": request.method,
                "path": path,
                "directory": directory,
                "authorization": request.headers.get("Authorization"),
                "json": body,
            }
        )
        for method, pattern, queued in self._routes:
            if method == request.method and pattern.fullmatch(path):
                spec = queued[0] if len(queued) == 1 else queued.popleft()
                return _build_response(spec)
        return httpx.Response(500, json={"error": f"no scripted route for {request.method} {path}"})


def _build_response(spec: Mapping[str, Any]) -> httpx.Response:
    status = int(spec["status"])
    headers = dict(spec.get("headers", {}))
    if "content" in spec:
        return httpx.Response(status, content=spec["content"], headers=headers)
    if "json" in spec:
        return httpx.Response(status, json=spec["json"], headers=headers)
    return httpx.Response(status, headers=headers)


# --------------------------------------------------------------------------------------
# WithParts builders (use the probe's fixed caller identities/schema/canary)
# --------------------------------------------------------------------------------------


def user_withparts(session_id: str, caller_id: str, *, retry: int | None = 2, schema: Any = None) -> dict:
    fmt: dict[str, Any] = {"type": "json_schema", "schema": schema or probe.FIXED_CANARY_SCHEMA}
    if retry is not None:
        fmt["retryCount"] = retry
    return {
        "info": {
            "id": caller_id,
            "sessionID": session_id,
            "role": "user",
            "format": fmt,
        },
        "parts": [{"type": "text", "text": "instruction"}],
    }


def assistant_withparts(
    session_id: str,
    parent_id: str,
    assistant_id: str,
    *,
    structured: Any,
    provider: str = PROVIDER,
    model: str = MODEL,
    completed: bool = True,
    error: Any = None,
) -> dict:
    info: dict[str, Any] = {
        "id": assistant_id,
        "sessionID": session_id,
        "parentID": parent_id,
        "role": "assistant",
        "providerID": provider,
        "modelID": model,
        "time": {"created": 1, **({"completed": 2} if completed else {})},
        "format": {"type": "json_schema", "schema": probe.FIXED_CANARY_SCHEMA, "retryCount": 2},
    }
    if structured is not None:
        info["structured"] = structured
    if error is not None:
        info["error"] = error
    return {"info": info, "parts": [{"type": "text", "text": "done"}]}


def tool_assistant_withparts(session_id: str, parent_id: str, assistant_id: str) -> dict:
    return {
        "info": {
            "id": assistant_id,
            "sessionID": session_id,
            "parentID": parent_id,
            "role": "assistant",
            "providerID": PROVIDER,
            "modelID": MODEL,
            "time": {"created": 1},
        },
        "parts": [{"type": "tool", "state": {"status": "running"}}],
    }


# --------------------------------------------------------------------------------------
# Environment / argv builders
# --------------------------------------------------------------------------------------


def _owner_only_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _owner_only_file(path: Path, content: bytes, *, mode: int = 0o600) -> Path:
    path.write_bytes(content)
    os.chmod(path, mode)
    return path


class Env:
    def __init__(self, tmp_path: Path, *, binary_content: bytes | None = None) -> None:
        self.root = tmp_path
        self.data_root = _owner_only_dir(tmp_path / "opencode-data")
        self.evidence_dir = _owner_only_dir(tmp_path / "evidence")
        self.password_file = _owner_only_file(tmp_path / "password", b"s0-basic-secret-9f3c")
        self.binary = _owner_only_file(
            tmp_path / "opencode",
            binary_content if binary_content is not None else b"opencode-candidate-binary-bytes",
            mode=0o700,
        )
        self.state_path = self.evidence_dir / "pre-restart.json"
        self.output_path = self.evidence_dir / "eligibility-v1.json"
        self.endpoint = "http://127.0.0.1:4096"
        self.username = "operator"

    def binary_sha256(self) -> str:
        return hashlib.sha256(self.binary.read_bytes()).hexdigest()

    def pre_args(self, *, auth_mode: str = "none", **overrides: Any) -> list[str]:
        args = [
            "pre-restart",
            "--endpoint",
            overrides.get("endpoint", self.endpoint),
            "--auth-mode",
            auth_mode,
            "--server-data-root",
            str(overrides.get("data_root", self.data_root)),
            "--workspace-directory",
            overrides.get("workspace_directory", WORKSPACE_DIRECTORY),
            "--server-binary",
            str(self.binary),
            "--release-tag",
            overrides.get("release_tag", "v1.18.26"),
            "--release-commit",
            overrides.get("release_commit", VALID_COMMIT),
            "--release-asset",
            overrides.get("release_asset", "opencode-darwin-arm64.zip"),
            "--platform",
            overrides.get("platform", "darwin"),
            "--architecture",
            overrides.get("architecture", "arm64"),
            "--expected-server-version",
            overrides.get("version", EXPECTED_VERSION),
            "--provider",
            PROVIDER,
            "--model",
            MODEL,
            "--state",
            str(overrides.get("state", self.state_path)),
        ]
        if auth_mode == "opencode-basic":
            args += ["--username", self.username, "--password-file", str(self.password_file)]
        # Fast, bounded polling so the suite never sleeps meaningfully.
        args += ["--poll-interval-seconds", "0.0", "--poll-deadline-seconds", "1.0"]
        return args

    def post_args(self, **overrides: Any) -> list[str]:
        args = [
            "post-restart",
            "--endpoint",
            overrides.get("endpoint", self.endpoint),
            "--auth-mode",
            overrides.get("auth_mode", "none"),
            "--server-data-root",
            str(self.data_root),
            "--workspace-directory",
            WORKSPACE_DIRECTORY,
            "--server-binary",
            str(self.binary),
            "--expected-server-version",
            EXPECTED_VERSION,
            "--state",
            str(overrides.get("state", self.state_path)),
            "--output",
            str(overrides.get("output", self.output_path)),
        ]
        args += ["--poll-interval-seconds", "0.0", "--poll-deadline-seconds", "1.0"]
        return args


# Scripted flows -----------------------------------------------------------------------


NO_MODEL_SID = "ses_nomodel000000000000001"
CANARY_SID = "ses_canary0000000000000001"
ASSISTANT_ID = "msg_asst0000000000000000001"


def _add_health(s: Scripted) -> Scripted:
    s.add("GET", r"/global/health", {"status": 200, "json": {"healthy": True, "version": EXPECTED_VERSION}})
    return s


def add_canary_success(
    s: Scripted,
    *,
    canary_sid: str = CANARY_SID,
    assistant_id: str = ASSISTANT_ID,
    structured: Any = None,
    with_tool_intermediate: bool = False,
) -> Scripted:
    """Add the canary prompt/list/single routes.

    The caller must already have scripted ``POST /session`` so that its *second* response
    returns ``canary_sid`` (the first response is the no-model session).
    """

    structured = structured if structured is not None else {"canary": probe.CANARY_VALUE}
    s.add("POST", rf"/session/{canary_sid}/prompt_async", {"status": 204})
    listing: list[dict] = [user_withparts(canary_sid, probe.CALLER_CANARY_MESSAGE_ID)]
    if with_tool_intermediate:
        listing.append(
            tool_assistant_withparts(
                canary_sid, probe.CALLER_CANARY_MESSAGE_ID, "msg_tool0000000000000000001"
            )
        )
    listing.append(
        assistant_withparts(canary_sid, probe.CALLER_CANARY_MESSAGE_ID, assistant_id, structured=structured)
    )
    s.add("GET", rf"/session/{canary_sid}/message", {"status": 200, "json": listing})
    s.add(
        "GET",
        rf"/session/{canary_sid}/message/{assistant_id}",
        {
            "status": 200,
            "json": assistant_withparts(
                canary_sid, probe.CALLER_CANARY_MESSAGE_ID, assistant_id, structured=structured
            ),
        },
    )
    return s


def full_green_script(*, structured: Any = None, with_tool_intermediate: bool = False) -> Scripted:
    s = Scripted()
    _add_health(s)
    # A single POST /session route yields the no-model session first, then the canary session.
    s.add(
        "POST",
        r"/session",
        {"status": 200, "json": {"id": NO_MODEL_SID, "directory": WORKSPACE_DIRECTORY}},
        {"status": 200, "json": {"id": CANARY_SID, "directory": WORKSPACE_DIRECTORY}},
    )
    s.add("POST", rf"/session/{NO_MODEL_SID}/prompt_async", {"status": 204})
    s.add(
        "GET",
        rf"/session/{NO_MODEL_SID}/message",
        {"status": 200, "json": [user_withparts(NO_MODEL_SID, probe.CALLER_NO_REPLY_MESSAGE_ID)]},
    )
    s.add(
        "GET",
        rf"/session/{NO_MODEL_SID}/message/{probe.CALLER_NO_REPLY_MESSAGE_ID}",
        {"status": 200, "json": user_withparts(NO_MODEL_SID, probe.CALLER_NO_REPLY_MESSAGE_ID)},
    )
    return add_canary_success(s, structured=structured, with_tool_intermediate=with_tool_intermediate)


def post_restart_script(
    *,
    no_model_sid: str = "ses_nomodel000000000000001",
    canary_sid: str = "ses_canary0000000000000001",
    assistant_id: str = "msg_asst0000000000000000001",
    structured: Any = None,
) -> Scripted:
    structured = structured if structured is not None else {"canary": probe.CANARY_VALUE}
    s = Scripted()
    s.add("GET", r"/global/health", {"status": 200, "json": {"healthy": True, "version": EXPECTED_VERSION}})
    s.add(
        "GET",
        rf"/session/{canary_sid}/message",
        {
            "status": 200,
            "json": [
                user_withparts(canary_sid, probe.CALLER_CANARY_MESSAGE_ID),
                assistant_withparts(
                    canary_sid, probe.CALLER_CANARY_MESSAGE_ID, assistant_id, structured=structured
                ),
            ],
        },
    )
    s.add(
        "GET",
        rf"/session/{canary_sid}/message/{probe.CALLER_CANARY_MESSAGE_ID}",
        {"status": 200, "json": user_withparts(canary_sid, probe.CALLER_CANARY_MESSAGE_ID)},
    )
    s.add(
        "GET",
        rf"/session/{canary_sid}/message/{assistant_id}",
        {
            "status": 200,
            "json": assistant_withparts(
                canary_sid, probe.CALLER_CANARY_MESSAGE_ID, assistant_id, structured=structured
            ),
        },
    )
    return s


def _run(argv: list[str], scripted: Scripted) -> int:
    return probe.run_cli(argv, transport=scripted.transport())


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# Tests: pre-restart negative fixture (the fixed v1.18.26 message-roundtrip-red case)
# --------------------------------------------------------------------------------------


def test_fixture_v11826_message_roundtrip_red(tmp_path: Path) -> None:
    fixture = _read_json(_FIXTURE_PATH)
    release = fixture["release"]
    binary_content = fixture["binary_content_utf8"].encode("utf-8")
    env = Env(tmp_path, binary_content=binary_content)
    assert env.binary_sha256() == release["binary_sha256"]

    script = fixture["http_script"]
    s = Scripted()
    s.add("GET", r"/global/health", {"status": script["health"]["status"], "json": script["health"]["json"]})
    s.add(
        "POST",
        r"/session",
        {
            "status": script["create_no_model_session"]["status"],
            "json": script["create_no_model_session"]["json"],
        },
    )
    sid = script["create_no_model_session"]["json"]["id"]
    s.add("POST", rf"/session/{sid}/prompt_async", {"status": script["prompt_async"]["status"]})
    s.add(
        "GET",
        rf"/session/{sid}/message",
        {"status": script["list_messages"]["status"], "json": script["list_messages"]["json"]},
    )
    s.add(
        "GET",
        rf"/session/{sid}/message/{probe.CALLER_NO_REPLY_MESSAGE_ID}",
        {"status": script["single_message"]["status"], "json": script["single_message"]["json"]},
    )

    argv = env.pre_args(
        workspace_directory=fixture["workspace_directory"],
        release_tag=release["release_tag"],
        release_commit=release["release_commit"],
        release_asset=release["release_asset"],
        platform=release["platform"],
        architecture=release["architecture"],
        version=release["expected_server_version"],
    )
    # provider/model are still declared but must never be prompted for this fixture.
    exit_code = _run(argv, s)

    assert exit_code != 0
    assert env.state_path.exists()
    state = _read_json(env.state_path)
    expected = fixture["expected_pre_state"]
    assert state["status"] == expected["status"] == "message_roundtrip_red"
    assert state["prompt_status_code"] == 204
    assert state["list_status_code"] == 400
    assert state["single_status_code"] == 400
    assert state["candidate_digest"] is None
    assert state["binary_sha256"] == release["binary_sha256"]

    # Zero provider/model prompt calls: exactly one session create + one prompt_async.
    assert s.count("POST", r"/session") == 1
    assert s.count("POST", r"/session/.*/prompt_async") == 1


def test_red_state_never_leaks_secret_material(tmp_path: Path) -> None:
    env = Env(tmp_path)
    # Break the roundtrip so it stays red but a state is still written.
    s = Scripted()
    s.add("GET", r"/global/health", {"status": 200, "json": {"healthy": True, "version": EXPECTED_VERSION}})
    s.add("POST", r"/session", {"status": 200, "json": {"id": "ses_nomodel000000000000001"}})
    s.add("POST", r"/session/.*/prompt_async", {"status": 204})
    s.add("GET", r"/session/.*/message/.+", {"status": 400, "json": {"error": "x"}})
    s.add("GET", r"/session/.*/message", {"status": 400, "json": {"error": "x"}})

    argv = env.pre_args(auth_mode="opencode-basic")
    exit_code = _run(argv, s)
    assert exit_code != 0
    raw_state = env.state_path.read_text(encoding="utf-8")
    password = env.password_file.read_bytes().decode("utf-8")
    b64 = base64.b64encode(f"{env.username}:{password}".encode()).decode()
    assert password not in raw_state
    assert b64 not in raw_state
    assert "Authorization" not in raw_state
    assert "Basic " not in raw_state
    assert str(env.data_root) not in raw_state


# --------------------------------------------------------------------------------------
# Tests: pre-restart green path -> awaiting_restart, then post-restart -> eligible
# --------------------------------------------------------------------------------------


def test_pre_restart_green_reaches_awaiting_restart(tmp_path: Path) -> None:
    env = Env(tmp_path)
    exit_code = _run(env.pre_args(), full_green_script())
    assert exit_code == 0
    state = _read_json(env.state_path)
    assert state["status"] == "awaiting_restart"
    assert state["candidate_digest"] and len(state["candidate_digest"]) == 64
    assert state["canary_assistant_message_id"] == "msg_asst0000000000000000001"


def test_intermediate_tool_assistant_is_not_terminal_candidate(tmp_path: Path) -> None:
    env = Env(tmp_path)
    exit_code = _run(env.pre_args(), full_green_script(with_tool_intermediate=True))
    assert exit_code == 0
    assert _read_json(env.state_path)["status"] == "awaiting_restart"


def test_post_restart_eligible_after_awaiting_restart(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert _run(env.pre_args(), full_green_script()) == 0
    exit_code = _run(env.post_args(), post_restart_script())
    assert exit_code == 0
    report = _read_json(env.output_path)
    assert report["status"] == "eligible"
    assert report["restart_evidence_level"] == "operator_executed_unverified"
    assert report["candidate_digest"] == _read_json(env.state_path)["candidate_digest"]


def test_post_restart_refuses_non_awaiting_state(tmp_path: Path) -> None:
    env = Env(tmp_path)
    # Produce a message_roundtrip_red state first.
    s = Scripted()
    s.add("GET", r"/global/health", {"status": 200, "json": {"healthy": True, "version": EXPECTED_VERSION}})
    s.add("POST", r"/session", {"status": 200, "json": {"id": "ses_nomodel000000000000001"}})
    s.add("POST", r"/session/.*/prompt_async", {"status": 204})
    s.add("GET", r"/session/.*/message/.+", {"status": 400, "json": {"error": "x"}})
    s.add("GET", r"/session/.*/message", {"status": 400, "json": {"error": "x"}})
    assert _run(env.pre_args(), s) != 0
    assert _read_json(env.state_path)["status"] == "message_roundtrip_red"

    exit_code = _run(env.post_args(), post_restart_script())
    assert exit_code != 0
    assert not env.output_path.exists()


def test_post_restart_digest_drift_fails_closed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert _run(env.pre_args(), full_green_script()) == 0
    drifted = post_restart_script(structured={"canary": "AA_OPENCODE_S0_V1_DRIFTED"})
    exit_code = _run(env.post_args(), drifted)
    assert exit_code != 0
    if env.output_path.exists():
        assert _read_json(env.output_path)["status"] == "post_restart_red"


# --------------------------------------------------------------------------------------
# Tests: request-directory scoping and health carve-out
# --------------------------------------------------------------------------------------


def test_every_scoped_request_carries_directory_query(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = full_green_script()
    assert _run(env.pre_args(), s) == 0
    for r in s.requests:
        if r["path"] == "/global/health":
            assert r["directory"] is None
        else:
            assert r["directory"] == WORKSPACE_DIRECTORY


# --------------------------------------------------------------------------------------
# Tests: roundtrip format / retry-count enforcement
# --------------------------------------------------------------------------------------


def test_missing_retry_count_fails_closed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid = "ses_nomodel000000000000001"
    s = Scripted()
    s.add("GET", r"/global/health", {"status": 200, "json": {"healthy": True, "version": EXPECTED_VERSION}})
    s.add("POST", r"/session", {"status": 200, "json": {"id": sid}})
    s.add("POST", rf"/session/{sid}/prompt_async", {"status": 204})
    s.add(
        "GET",
        rf"/session/{sid}/message",
        {"status": 200, "json": [user_withparts(sid, probe.CALLER_NO_REPLY_MESSAGE_ID, retry=None)]},
    )
    s.add(
        "GET",
        rf"/session/{sid}/message/{probe.CALLER_NO_REPLY_MESSAGE_ID}",
        {"status": 200, "json": user_withparts(sid, probe.CALLER_NO_REPLY_MESSAGE_ID, retry=None)},
    )
    exit_code = _run(env.pre_args(), s)
    assert exit_code != 0
    assert _read_json(env.state_path)["status"] == "message_roundtrip_red"


# --------------------------------------------------------------------------------------
# Tests: canary failures fail closed with zero eligible claim
# --------------------------------------------------------------------------------------


def test_wrong_canary_object_fails_closed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = full_green_script(structured={"canary": "not-the-token"})
    exit_code = _run(env.pre_args(), s)
    assert exit_code != 0
    assert _read_json(env.state_path)["status"] == "provider_canary_red"


def test_assistant_error_fails_closed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    canary_sid = CANARY_SID
    assistant_id = ASSISTANT_ID
    s = Scripted()
    _add_health(s)
    s.add(
        "POST",
        r"/session",
        {"status": 200, "json": {"id": NO_MODEL_SID}},
        {"status": 200, "json": {"id": canary_sid}},
    )
    s.add("POST", rf"/session/{NO_MODEL_SID}/prompt_async", {"status": 204})
    s.add(
        "GET",
        rf"/session/{NO_MODEL_SID}/message",
        {"status": 200, "json": [user_withparts(NO_MODEL_SID, probe.CALLER_NO_REPLY_MESSAGE_ID)]},
    )
    s.add(
        "GET",
        rf"/session/{NO_MODEL_SID}/message/{probe.CALLER_NO_REPLY_MESSAGE_ID}",
        {"status": 200, "json": user_withparts(NO_MODEL_SID, probe.CALLER_NO_REPLY_MESSAGE_ID)},
    )
    s.add("POST", rf"/session/{canary_sid}/prompt_async", {"status": 204})
    listing = [
        user_withparts(canary_sid, probe.CALLER_CANARY_MESSAGE_ID),
        assistant_withparts(
            canary_sid,
            probe.CALLER_CANARY_MESSAGE_ID,
            assistant_id,
            structured={"canary": probe.CANARY_VALUE},
            error={"name": "ProviderError", "message": "boom"},
        ),
    ]
    s.add("GET", rf"/session/{canary_sid}/message", {"status": 200, "json": listing})
    exit_code = _run(env.pre_args(), s)
    assert exit_code != 0
    assert _read_json(env.state_path)["status"] == "provider_canary_red"


# --------------------------------------------------------------------------------------
# Tests: bounded eventual visibility
# --------------------------------------------------------------------------------------


def test_bounded_visibility_retries_then_succeeds(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid = "ses_nomodel000000000000001"
    s = Scripted()
    s.add("GET", r"/global/health", {"status": 200, "json": {"healthy": True, "version": EXPECTED_VERSION}})
    s.add(
        "POST",
        r"/session",
        {"status": 200, "json": {"id": sid}},
        {"status": 200, "json": {"id": "ses_canary0000000000000001"}},
    )
    s.add("POST", r"/session/.*/prompt_async", {"status": 204})
    # list: two transient empty 200s, then the target appears.
    s.add(
        "GET",
        rf"/session/{sid}/message",
        {"status": 200, "json": []},
        {"status": 404, "json": {"error": "not found"}},
        {"status": 200, "json": [user_withparts(sid, probe.CALLER_NO_REPLY_MESSAGE_ID)]},
    )
    s.add(
        "GET",
        rf"/session/{sid}/message/{probe.CALLER_NO_REPLY_MESSAGE_ID}",
        {"status": 404, "json": {"error": "not found"}},
        {"status": 200, "json": user_withparts(sid, probe.CALLER_NO_REPLY_MESSAGE_ID)},
    )
    add_canary_success(s)
    exit_code = _run(env.pre_args(), s)
    assert exit_code == 0
    assert _read_json(env.state_path)["status"] == "awaiting_restart"


def test_oversized_body_fails_closed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid = "ses_nomodel000000000000001"
    s = Scripted()
    s.add("GET", r"/global/health", {"status": 200, "json": {"healthy": True, "version": EXPECTED_VERSION}})
    s.add("POST", r"/session", {"status": 200, "json": {"id": sid}})
    s.add("POST", rf"/session/{sid}/prompt_async", {"status": 204})
    s.add("GET", rf"/session/{sid}/message", {"status": 200, "content": b"x" * 5_000_000})
    s.add(
        "GET",
        rf"/session/{sid}/message/{probe.CALLER_NO_REPLY_MESSAGE_ID}",
        {"status": 200, "content": b"x" * 5_000_000},
    )
    exit_code = _run(env.pre_args(), s)
    assert exit_code != 0
    assert _read_json(env.state_path)["status"] == "message_roundtrip_red"


# --------------------------------------------------------------------------------------
# Tests: transport hardening (redirect / proxy refusal) and endpoint validation
# --------------------------------------------------------------------------------------


def test_redirect_fails_closed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = Scripted()
    s.add(
        "GET", r"/global/health", {"status": 302, "headers": {"Location": "http://127.0.0.1:4096/elsewhere"}}
    )
    exit_code = _run(env.pre_args(), s)
    assert exit_code != 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://10.0.0.5:4096",
        "http://example.com:4096",
        "http://user:pass@127.0.0.1:4096",
        "http://127.0.0.1:4096/base",
        "http://127.0.0.1:4096/?directory=x",
    ],
)
def test_endpoint_must_be_bare_loopback_origin(tmp_path: Path, endpoint: str) -> None:
    env = Env(tmp_path)
    s = full_green_script()
    exit_code = _run(env.pre_args(endpoint=endpoint), s)
    assert exit_code != 0
    assert not env.state_path.exists()


def test_loopback_ipv6_is_accepted(tmp_path: Path) -> None:
    env = Env(tmp_path)
    exit_code = _run(env.pre_args(endpoint="http://[::1]:4096"), full_green_script())
    assert exit_code == 0


# --------------------------------------------------------------------------------------
# Tests: release-tag identity discipline
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("tag", ["current", "latest", ">=1.18.0", "1.18.x", ""])
def test_floating_release_tag_rejected(tmp_path: Path, tag: str) -> None:
    env = Env(tmp_path)
    exit_code = _run(env.pre_args(release_tag=tag), full_green_script())
    assert exit_code != 0
    assert not env.state_path.exists()


def test_bad_release_commit_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path)
    exit_code = _run(env.pre_args(release_commit="not-a-sha"), full_green_script())
    assert exit_code != 0
    assert not env.state_path.exists()


# --------------------------------------------------------------------------------------
# Tests: auth mode behaviour and secret hygiene on the wire
# --------------------------------------------------------------------------------------


def test_none_auth_sends_no_authorization(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = full_green_script()
    assert _run(env.pre_args(), s) == 0
    assert all(r["authorization"] is None for r in s.requests)


def test_basic_auth_sends_only_basic(tmp_path: Path) -> None:
    env = Env(tmp_path)
    s = full_green_script()
    assert _run(env.pre_args(auth_mode="opencode-basic"), s) == 0
    seen = [r["authorization"] for r in s.requests if r["authorization"] is not None]
    assert seen, "basic auth should send an Authorization header"
    password = env.password_file.read_bytes().decode("utf-8")
    expected = "Basic " + base64.b64encode(f"{env.username}:{password}".encode()).decode()
    assert all(h == expected for h in seen)
    assert all(h.startswith("Basic ") for h in seen)


def test_password_file_with_newline_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _owner_only_file(env.password_file, b"secret-with-newline\n")
    exit_code = _run(env.pre_args(auth_mode="opencode-basic"), full_green_script())
    assert exit_code != 0
    assert not env.state_path.exists()


# --------------------------------------------------------------------------------------
# Tests: evidence-directory / data-root discipline
# --------------------------------------------------------------------------------------


def test_non_empty_evidence_dir_rejected_for_pre_restart(tmp_path: Path) -> None:
    env = Env(tmp_path)
    (env.evidence_dir / "stray.txt").write_text("x", encoding="utf-8")
    exit_code = _run(env.pre_args(), full_green_script())
    assert exit_code != 0
    assert not env.state_path.exists()


def test_evidence_dir_inside_worktree_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path)
    worktree = tmp_path / "wt"
    _owner_only_dir(worktree)
    (worktree / ".git").mkdir()
    evidence = _owner_only_dir(worktree / "evidence")
    exit_code = _run(env.pre_args(state=evidence / "pre-restart.json"), full_green_script())
    assert exit_code != 0


def test_symlinked_binary_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path)
    real = _owner_only_file(tmp_path / "real-binary", b"real", mode=0o700)
    link = tmp_path / "linked-binary"
    link.symlink_to(real)
    argv = env.pre_args()
    idx = argv.index("--server-binary")
    argv[idx + 1] = str(link)
    exit_code = _run(argv, full_green_script())
    assert exit_code != 0
    assert not env.state_path.exists()


def test_data_root_inode_replacement_is_drift(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert _run(env.pre_args(), full_green_script()) == 0
    # Delete/recreate the data root -> new inode -> post-restart must detect drift.
    import shutil

    shutil.rmtree(env.data_root)
    _owner_only_dir(env.data_root)
    exit_code = _run(env.post_args(), post_restart_script())
    assert exit_code != 0


def test_sqlite_wal_mutation_under_same_inode_is_not_drift(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert _run(env.pre_args(), full_green_script()) == 0
    # Ordinary SQLite/WAL content/entry/size/timestamp churn under the SAME inode.
    (env.data_root / "opencode.db").write_bytes(b"sqlite-pages" * 100)
    (env.data_root / "opencode.db-wal").write_bytes(b"wal" * 50)
    os.utime(env.data_root, None)
    exit_code = _run(env.post_args(), post_restart_script())
    assert exit_code == 0
    assert _read_json(env.output_path)["status"] == "eligible"


def test_post_restart_binary_drift_fails_closed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert _run(env.pre_args(), full_green_script()) == 0
    _owner_only_file(env.binary, b"a-different-candidate-binary", mode=0o700)
    exit_code = _run(env.post_args(), post_restart_script())
    assert exit_code != 0
    if env.output_path.exists():
        assert _read_json(env.output_path)["status"] == "post_restart_red"


def test_post_restart_makes_zero_post_requests(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert _run(env.pre_args(), full_green_script()) == 0
    s = post_restart_script()
    _run(env.post_args(), s)
    assert all(r["method"] != "POST" for r in s.requests)
