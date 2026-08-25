from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import tempfile
from pathlib import Path

import pytest
from graph_engine.plugin_api import (
    InvocationMetadata,
    SecretHandleUnauthorized,
    TaskContext,
    TaskRequest,
)

from agent_runtime_opencode.config import AdapterConfigurationError, OpenCodeAdapterConfig
from agent_runtime_opencode.handler import OpenCodeHandler
from fake_server import OpenCodeFakeServer  # pyright: ignore[reportMissingImports]
from harness import (  # pyright: ignore[reportMissingImports]
    WRITE_ROOT,
    _CANARY,
    _SECRET_TEXT,
    _SHA,
    _binding_data,
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
