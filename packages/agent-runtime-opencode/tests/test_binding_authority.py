from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace

import tempfile
from pathlib import Path

import httpx
import pytest
from agent_runtime_contracts import rebind_agent_run_workspace
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.plugin_api import (
    InvocationMetadata,
    SecretHandleUnauthorized,
    TaskContext,
    TaskRequest,
)

from agent_runtime_opencode.config import AdapterConfigurationError, OpenCodeAdapterConfig
from agent_runtime_opencode.discovery import agent_run_from_request
from agent_runtime_opencode.handler import OpenCodeHandler
from agent_runtime_opencode.protocol import OpenCodeHttpClient
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


async def test_execute_stamps_binding_title_after_create_before_prompt_admission() -> None:
    fixture = _open_code_fixture()
    try:
        fixture.fake.sse_mode = "fast_idle"
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        assert outcome.status == "succeeded"
        session_id = fixture.reference.session_id
        assert session_id is not None
        title = fixture.fake.session_title(session_id)
        assert title is not None
        assert title.startswith("aa-workspace-binding-v1:")
        document = json.loads(title.split(":", 1)[1])
        agent_run = agent_run_from_request(fixture.request)
        assert document["session_id"] == session_id
        assert document["agent_profile"] == agent_run.workspace.agent_profile
        assert document["agent_profile"] != agent_run.execution.worker_profile
        assert document["write_root"] == WRITE_ROOT
        assert document["allowed_outputs"] == list(ALLOWED_OUTPUTS)
        assert document["task_id"] == "task-1"
        assert document["attempt"] == 1
        assert document["attempt_id"] == "attempt-1"
        record = fixture.fake._sessions[session_id]
        assert record.get("agent") == document["agent_profile"]
        records = fixture.fake.records
        create_at = _first_record_index(records, method="POST", path="/session")
        stamp_at = _first_record_index(records, method="PATCH", path_suffix=f"/session/{session_id}")
        admit_at = _first_record_index(records, method="POST", path_suffix="/prompt_async")
        assert create_at < stamp_at < admit_at
        assert fixture.fake.title_update_bodies == [{"title": title}]
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
        execute_root = fixture.context.project_root / "qa/changes/CH-1/.staging/execute-task/attempt-1"
        execute_root.mkdir(parents=True)
        context = replace(fixture.context, write_root=execute_root)
        fixture.fake.terminal_mode = "success"
        fixture.fake.sse_mode = "fast_idle"

        outcome = await fixture.handler.execute(fixture.request, context)

        assert outcome.status == "succeeded"
        session_id = fixture.reference.session_id
        assert session_id is not None
        title = fixture.fake.session_title(session_id)
        assert title is not None
        document = json.loads(title.split(":", 1)[1])
        assert document["write_root"] == execute_root.relative_to(context.project_root).as_posix()
        assert document["allowed_outputs"] == list(ALLOWED_OUTPUTS)
        assert agent_run_from_request(fixture.request).workspace.write_root == WRITE_ROOT
        effective = rebind_agent_run_workspace(
            agent_run_from_request(fixture.request),
            project_root=context.project_root,
            write_root=context.write_root,
        )
        assert fixture.reference.prompt_body_digest == canonical_digest(effective.model_dump(mode="json"))
        assert fixture.fake.prompt_bodies[0]["agent"] == effective.workspace.agent_profile
        assert fixture.fake.title_update_bodies == [{"title": title}]
        reconciled = await fixture.handler.reconcile(fixture.request, context, fixture.activity)
        assert reconciled.status == "terminal"
        canceled = await fixture.handler.cancel(fixture.request, context, fixture.activity)
        assert canceled.status == "terminal"
    finally:
        fixture.close()


async def test_invalid_binding_title_fails_closed_before_prompt_admission() -> None:
    fixture = _bound_fixture(terminal_mode="success", sse_mode="fast_idle")
    try:
        fixture.fake.reject_title_updates = True
        assert fixture.fake.session_title(fixture.reference.session_id or "") == "seed"
        try:
            outcome = await fixture.handler.execute(fixture.request, fixture.context)
        except Exception:
            outcome = None
        assert fixture.fake.prompt_posts == 0
        if outcome is not None:
            assert outcome.status != "succeeded"
    finally:
        fixture.close()


async def test_binding_update_wrong_session_id_fails_closed_before_prompt_admission() -> None:
    fixture = _open_code_fixture()
    try:
        fixture.fake.sse_mode = "fast_idle"
        fixture.fake.title_update_response_session_id = "ses_foreign"

        outcome = await fixture.handler.execute(fixture.request, fixture.context)

        assert outcome.status == "failed"
        assert fixture.fake.prompt_posts == 0
        assert fixture.fake.title_update_bodies
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
