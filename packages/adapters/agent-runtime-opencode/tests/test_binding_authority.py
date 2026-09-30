from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace

import tempfile
from pathlib import Path

import httpx
import pytest
from agent_runtime_contracts import InstructionPart, rebind_agent_run_workspace
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.plugin_api import (
    InvocationMetadata,
    SecretHandleUnauthorized,
    TaskContext,
    TaskRequest,
)

from agent_runtime_opencode.config import AdapterConfigurationError, OpenCodeAdapterConfig
from agent_runtime_opencode.handler import OpenCodeHandler
from agent_runtime_opencode.session.binding import activity_label_for_session, child_session_title
from agent_runtime_opencode.session.discovery import agent_run_from_request
from agent_runtime_opencode.transport.http import OpenCodeHttpClient
from fake_server import OpenCodeFakeServer  # pyright: ignore[reportMissingImports]
from harness import (  # pyright: ignore[reportMissingImports]
    ALLOWED_OUTPUTS,
    WRITE_ROOT,
    _CANARY,
    _SECRET_TEXT,
    _SHA,
    _binding_data,
    _bound_fixture,
    _config,
    _open_code_fixture,
    _terminal_success_fixture,
    agent_run_request,
    profile,
    task_request,
    workspace_identity,
)


class _ExactSecretPort:
    def __init__(self, authorized: dict[str, bytes]) -> None:
        self._authorized = dict(authorized)

    def resolve(self, handle: str) -> bytes:
        try:
            return bytes(self._authorized[handle])
        except KeyError as error:
            raise SecretHandleUnauthorized(f"unauthorized secret handle: {handle}") from error


@pytest.fixture
def opencode_context() -> Iterator[TaskContext]:
    root = tempfile.TemporaryDirectory()
    project_root = Path(root.name) / "project"
    write_root = project_root / WRITE_ROOT
    write_root.mkdir(parents=True)
    yield TaskContext(
        project_root=project_root,
        write_root=write_root,
        workspace_identity=workspace_identity(),
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest=_SHA,
            composition_digest="b" * 64,
            entrypoint="runtime.opencode.execute",
        ),
        secrets=_ExactSecretPort({"opencode.token": _CANARY}),
    )
    root.cleanup()


@pytest.fixture
def task_request_fixture() -> TaskRequest:
    fake = OpenCodeFakeServer(profile=profile())
    config = _config(fake)
    fake.close()
    return task_request(agent_run_request(), binding_data=_binding_data(config))


async def test_opencode_rejects_missing_locked_endpoint(
    task_request_fixture: TaskRequest, opencode_context: TaskContext
) -> None:
    configured = task_request_fixture.model_copy(update={"binding_data": {"model": "gpt-5.6-terra"}})
    outcome = await OpenCodeHandler().execute(configured, opencode_context)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "configuration"
    assert "endpoint" in outcome.failure.message
    assert outcome.failure.retryable is False


async def test_opencode_rejects_extra_binding_fields(opencode_context: TaskContext) -> None:
    fake = OpenCodeFakeServer(profile=profile())
    try:
        binding = _binding_data(_config(fake))
        binding["model_fallback"] = "auto"
        configured = task_request(agent_run_request(), binding_data=binding)
        outcome = await OpenCodeHandler().execute(configured, opencode_context)
        assert outcome.status == "failed"
        assert outcome.failure is not None
        assert outcome.failure.kind == "configuration"
        assert not fake.records
    finally:
        fake.close()


async def test_opencode_rejects_unauthorized_secret_before_dispatch(
    opencode_context: TaskContext,
) -> None:
    fake = OpenCodeFakeServer(profile=profile())
    try:
        configured = task_request(agent_run_request(), binding_data=_binding_data(_config(fake)))
        context = replace(opencode_context, secrets=_ExactSecretPort({}))
        with pytest.raises(SecretHandleUnauthorized):
            await OpenCodeHandler().preflight(configured, context)
        assert not fake.records
    finally:
        fake.close()


def test_opencode_from_request_redacts_secret_material_in_errors() -> None:
    fake = OpenCodeFakeServer(profile=profile())
    try:
        binding = _binding_data(_config(fake))
        binding["secret"] = _SECRET_TEXT
        with pytest.raises(AdapterConfigurationError) as error:
            OpenCodeAdapterConfig.from_request(task_request(agent_run_request(), binding_data=binding))
        assert _SECRET_TEXT not in str(error.value)
    finally:
        fake.close()


async def test_execute_uses_project_root_and_request_carries_stage_root() -> None:
    fixture = _open_code_fixture(config_overrides={"project_scope": "/tmp/harness-project-copy"})
    try:
        fixture.fake.project_scope = str(fixture.context.project_root.resolve())
        fixture.fake.terminal_mode = "success"
        fixture.fake.sse_mode = "fast_idle"
        agent_run = agent_run_request()
        assert agent_run.workspace.write_root == WRITE_ROOT
        assert fixture.context.write_root.resolve() != fixture.context.project_root.resolve()
        assert fixture.context.write_root == fixture.context.project_root / WRITE_ROOT
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        assert outcome.status == "succeeded"
        project = str(fixture.context.project_root.resolve())
        stage = str(fixture.context.write_root.resolve())
        assert fixture.fake.directories
        assert all(item == project for item in fixture.fake.directories)
        assert stage not in fixture.fake.directories
        assert "/tmp/harness-project-copy" not in fixture.fake.directories
        dumped = agent_run.model_dump(mode="json")
        assert dumped["workspace"]["write_root"] == WRITE_ROOT
        assert dumped["workspace"]["allowed_outputs"] == list(agent_run.workspace.allowed_outputs)
        assert dumped["workspace"]["read_roots"] == list(agent_run.workspace.read_roots)
    finally:
        fixture.close()


def _first_record_index(
    records: tuple[object, ...],
    *,
    method: str,
    path_suffix: str | None = None,
    path: str | None = None,
) -> int:
    for index, record in enumerate(records):
        recorded_path = getattr(record, "path")
        if getattr(record, "method") != method:
            continue
        if path is not None and recorded_path == path:
            return index
        if path_suffix is not None and recorded_path.endswith(path_suffix):
            return index
    raise AssertionError(f"missing {method} {path or path_suffix}")


def test_child_session_label_uses_the_skill_heading() -> None:
    agent_run = agent_run_request().model_copy(
        update={
            "instructions": (
                InstructionPart.text(
                    "text/plain",
                    "# Locator-bounded case repair\n\nKeep the locator.\n",
                ),
            )
        }
    )

    assert activity_label_for_session(agent_run, "case-design") == "Locator-bounded case repair"
    assert activity_label_for_session(agent_run_request(), "run") == "run"
    task_id = "ac8ddcc53c" + "ab" * 27
    assert (
        child_session_title(
            activity_label="Locator-bounded case repair",
            task_id=task_id,
            attempt=2,
        )
        == "Assurance · Locator-bounded case repair · ac8ddcc53c… · #2"
    )
    assert child_session_title(activity_label="run", task_id="task-1", attempt=1) == (
        "Assurance · run · task-1 · #1"
    )


async def test_execute_stores_binding_metadata_and_readable_title_before_prompt() -> None:
    fixture = _open_code_fixture()
    try:
        fixture.fake.sse_mode = "fast_idle"
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        assert outcome.status == "succeeded"
        session_id = fixture.reference.session_id
        assert session_id is not None
        assert fixture.fake.session_title(session_id) == "Assurance · run · task-1 · #1"
        record = fixture.fake._sessions[session_id]
        metadata = record.get("metadata")
        assert isinstance(metadata, dict)
        document = metadata.get("workspace_binding")
        assert isinstance(document, dict)
        agent_run = agent_run_from_request(fixture.request)
        assert "session_id" not in document
        assert document["schema_version"] == "2"
        assert document["agent_profile"] == agent_run.workspace.agent_profile
        assert document["agent_profile"] != agent_run.execution.worker_profile
        assert document["write_root"] == WRITE_ROOT
        assert document["allowed_outputs"] == list(ALLOWED_OUTPUTS)
        assert document["read_roots"] == []
        assert document["activity_label"] == "run"
        assert document["task_id"] == "task-1"
        assert document["attempt"] == 1
        assert document["attempt_id"] == "attempt-1"
        assert record.get("agent") == document["agent_profile"]
        assert fixture.fake.create_bodies[0]["metadata"] == metadata
        assert fixture.fake.title_update_bodies == []
        records = fixture.fake.records
        create_at = _first_record_index(records, method="POST", path="/session")
        admit_at = _first_record_index(records, method="POST", path_suffix="/prompt_async")
        assert create_at < admit_at
        fixture.fake._sessions[session_id]["title"] = "renamed by user"
        reconciled = await fixture.handler.reconcile(fixture.request, fixture.context, fixture.activity)
        assert reconciled.status == "terminal"
    finally:
        fixture.close()


async def test_fake_server_rejects_title_update_extra_fields() -> None:
    fixture = _open_code_fixture()
    client = OpenCodeHttpClient(
        fixture.config,
        secret=_CANARY,
        directory=str(fixture.context.project_root.resolve()),
    )
    try:
        session = fixture.fake.add_session()
        with pytest.raises(httpx.HTTPStatusError) as error:
            await client.update_session(str(session["id"]), {"title": "bound", "agent": "build"})
        assert error.value.response.status_code == 400
    finally:
        await client.aclose()
        fixture.close()


async def test_opencode_rebinds_prepare_workspace_to_current_execute_workspace() -> None:
    fixture = _open_code_fixture()
    try:
        execute_root = fixture.context.project_root / "qa/.staging/execute-task/attempt-1"
        execute_root.mkdir(parents=True)
        context = replace(fixture.context, write_root=execute_root)
        fixture.fake.terminal_mode = "success"
        fixture.fake.sse_mode = "fast_idle"

        outcome = await fixture.handler.execute(fixture.request, context)

        assert outcome.status == "succeeded"
        session_id = fixture.reference.session_id
        assert session_id is not None
        record = fixture.fake._sessions[session_id]
        metadata = record.get("metadata")
        assert isinstance(metadata, dict)
        document = metadata.get("workspace_binding")
        assert isinstance(document, dict)
        assert document["write_root"] == execute_root.relative_to(context.project_root).as_posix()
        assert document["allowed_outputs"] == list(ALLOWED_OUTPUTS)
        assert document["read_roots"] == []
        assert record.get("title") == child_session_title(
            activity_label="run",
            task_id=context.workspace_identity.task_id,
            attempt=context.workspace_identity.attempt,
        )
        assert agent_run_from_request(fixture.request).workspace.write_root == WRITE_ROOT
        effective = rebind_agent_run_workspace(
            agent_run_from_request(fixture.request),
            project_root=context.project_root,
            write_root=context.write_root,
        )
        assert fixture.reference.prompt_body_digest == canonical_digest(effective.model_dump(mode="json"))
        assert fixture.fake.prompt_bodies[0]["agent"] == effective.workspace.agent_profile
        assert fixture.fake.title_update_bodies == []
        reconciled = await fixture.handler.reconcile(fixture.request, context, fixture.activity)
        assert reconciled.status == "terminal"
        canceled = await fixture.handler.cancel(fixture.request, context, fixture.activity)
        assert canceled.status == "terminal"
    finally:
        fixture.close()


async def test_missing_workspace_binding_fails_closed_before_prompt_admission() -> None:
    fixture = _bound_fixture(terminal_mode="success", sse_mode="fast_idle")
    try:
        session_id = fixture.reference.session_id
        assert session_id is not None
        discovery = fixture.metadata["discovery"]
        fixture.fake.set_session_metadata(session_id, {"discovery": discovery})
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        assert fixture.fake.prompt_posts == 0
        assert outcome.status == "failed"
        assert outcome.failure is not None
        assert outcome.failure.message == "workspace binding is missing or invalid"
    finally:
        fixture.close()


async def test_stored_binding_mismatch_fails_closed_before_prompt_admission() -> None:
    fixture = _open_code_fixture()
    try:
        fixture.fake.sse_mode = "fast_idle"
        fixture.fake.omit_stored_binding = True

        outcome = await fixture.handler.execute(fixture.request, fixture.context)

        assert outcome.status == "failed"
        assert outcome.failure is not None
        assert outcome.failure.message == "workspace binding is missing or invalid"
        assert fixture.fake.prompt_posts == 0
        assert fixture.fake.title_update_bodies == []
    finally:
        fixture.close()


async def test_opencode_cancel_parses_binding_independently(opencode_context: TaskContext) -> None:
    fixture = _terminal_success_fixture()
    try:
        drifted = fixture.request.model_copy(update={"binding_data": {"model": "drift"}})
        result = await fixture.handler.cancel(drifted, fixture.context, fixture.activity)
        assert result.status == "indeterminate"
        assert result.reason is not None
        assert "endpoint" in result.reason
    finally:
        fixture.close()


def test_isolated_execution_root_rejects_project_instruction_discovery(tmp_path: Path) -> None:
    from agent_runtime_opencode.session.binding import reject_isolated_root_discovery

    isolated = tmp_path / "isolated"
    isolated.mkdir()
    reject_isolated_root_discovery(isolated)
    forbidden = (
        "opencode.json",
        "opencode.jsonc",
        "AGENTS.md",
        "CLAUDE.md",
        "CONTEXT.md",
    )
    for name in forbidden:
        planted = isolated / name
        planted.write_text("canary")
        with pytest.raises(ValueError, match=name):
            reject_isolated_root_discovery(isolated)
        planted.unlink()
    for name in (".opencode", ".agents"):
        planted = isolated / name
        planted.mkdir()
        with pytest.raises(ValueError, match=name):
            reject_isolated_root_discovery(isolated)
        planted.rmdir()


async def test_one_root_session_per_stable_attempt_key() -> None:
    first = _open_code_fixture()
    second = _open_code_fixture()
    try:
        first.fake.sse_mode = "fast_idle"
        second.fake.sse_mode = "fast_idle"
        first_outcome = await first.handler.execute(first.request, first.context)
        second_outcome = await second.handler.execute(second.request, second.context)
        assert first_outcome.status == "succeeded"
        assert second_outcome.status == "succeeded"
        assert first.fake.create_calls == 1
        assert second.fake.create_calls == 1
        assert first.metadata["discovery"]["attempt"] == second.metadata["discovery"]["attempt"]
        assert first.metadata["discovery"]["activity_id"] == "activity-1"
        assert first.fake.create_bodies[0]["metadata"] == first.metadata
        assert "parentID" not in first.fake.create_bodies[0]
        replay = await first.handler.reconcile(first.request, first.context, first.activity)
        assert replay.status == "terminal"
        assert first.fake.create_calls == 1
    finally:
        first.close()
        second.close()


def _official_opencode_binary() -> Path | None:
    from shutil import which

    found = which("opencode")
    return Path(found) if found else None


@pytest.mark.skipif(_official_opencode_binary() is None, reason="official OpenCode binary is not available")
def test_official_binary_isolated_root_does_not_load_project_instruction_files(
    tmp_path: Path,
) -> None:
    import os
    import socket
    import subprocess
    import time
    import urllib.parse
    import urllib.request

    project = tmp_path / "project"
    isolated = project / "isolated"
    isolated.mkdir(parents=True)
    canaries = {
        "AGENTS.md": "CANARY_AGENTS",
        "CLAUDE.md": "CANARY_CLAUDE",
        "CONTEXT.md": "CANARY_CONTEXT",
        "opencode.json": json.dumps(
            {"model": "canary/parent-model", "instructions": ["CANARY_OPENCODE_JSON"]}
        ),
        "opencode.jsonc": '{ "instructions": ["CANARY_OPENCODE_JSONC"] }',
    }
    for name, body in canaries.items():
        (project / name).write_text(body)
        (isolated / name).write_text(body)
    (isolated / ".opencode").mkdir()
    (isolated / ".agents").mkdir()
    (isolated / ".opencode" / "agent.md").write_text("CANARY_DOT_OPENCODE")
    (isolated / ".agents" / "x.md").write_text("CANARY_DOT_AGENTS")
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(xdg)
    env["XDG_DATA_HOME"] = str(tmp_path / "data")
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "true"
    env.pop("OPENCODE_SERVER_PASSWORD", None)
    binary = _official_opencode_binary()
    assert binary is not None
    proc = subprocess.Popen(
        [str(binary), "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        cwd=str(isolated),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        config: dict[str, object] | None = None
        while time.monotonic() < deadline:
            try:
                url = f"http://127.0.0.1:{port}/config?directory={urllib.parse.quote(str(isolated))}"
                with urllib.request.urlopen(url, timeout=1) as response:
                    config = json.loads(response.read().decode())
                break
            except OSError:
                time.sleep(0.1)
        assert config is not None
        encoded = json.dumps(config)
        for canary in (
            "CANARY_AGENTS",
            "CANARY_CLAUDE",
            "CANARY_CONTEXT",
            "CANARY_OPENCODE_JSON",
            "CANARY_OPENCODE_JSONC",
            "CANARY_DOT_OPENCODE",
            "CANARY_DOT_AGENTS",
            "canary/parent-model",
        ):
            assert canary not in encoded
    finally:
        proc.terminate()
        proc.communicate(timeout=5)
