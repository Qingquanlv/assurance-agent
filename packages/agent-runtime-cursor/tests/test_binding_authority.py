from __future__ import annotations

from pathlib import Path

import pytest
from graph_engine.plugin_api import SecretHandleUnauthorized, TaskContext, TaskRequest

from agent_runtime_cursor.config import AdapterConfigurationError, CursorAdapterConfig
from agent_runtime_cursor.handler import CursorHandler
from cursor_harness import (  # pyright: ignore[reportMissingImports]
    SECRET_TEXT,
    ExactSecretPort,
    binding_data,
    complete_stream,
    config,
    context,
    request,
)
from fake_process_host import FakeConfinedProcessHost  # pyright: ignore[reportMissingImports]


def valid_cursor_binding(root: Path) -> dict[str, object]:
    return binding_data(config(root))


@pytest.fixture
def cursor_task_request(tmp_path: Path) -> TaskRequest:
    return request(binding_data=valid_cursor_binding(tmp_path))


@pytest.fixture
def cursor_context(tmp_path: Path) -> tuple[TaskContext, object]:
    task_context, port = context(tmp_path)
    return task_context, port


async def test_cursor_rejects_missing_locked_executable(
    cursor_task_request: TaskRequest, cursor_context: tuple[TaskContext, object]
) -> None:
    task_context, _ = cursor_context
    configured = cursor_task_request.model_copy(update={"binding_data": {"expected_version": "1.0.0"}})
    outcome = await CursorHandler(FakeConfinedProcessHost()).execute(configured, task_context)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "configuration"
    assert "executable" in outcome.failure.message
    assert outcome.failure.retryable is False


async def test_cursor_uses_request_binding_not_constructor(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost(stdout=complete_stream(str(tmp_path.resolve())))
    task_context, _ = context(tmp_path)
    configured = request(binding_data=valid_cursor_binding(tmp_path))
    outcome = await CursorHandler(host).execute(configured, task_context)
    assert outcome.status == "succeeded"


async def test_cursor_rejects_extra_binding_fields(tmp_path: Path) -> None:
    binding = valid_cursor_binding(tmp_path)
    binding["default_model"] = "composer-2"
    task_context, _ = context(tmp_path)
    host = FakeConfinedProcessHost()
    configured = request(binding_data=binding)
    outcome = await CursorHandler(host).execute(configured, task_context)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "configuration"
    assert host.spawn_count == 0


async def test_cursor_rejects_unauthorized_secret_before_dispatch(tmp_path: Path) -> None:
    task_context, _ = context(tmp_path, secrets=ExactSecretPort({}))
    host = FakeConfinedProcessHost()
    configured = request(binding_data=valid_cursor_binding(tmp_path))
    with pytest.raises(SecretHandleUnauthorized):
        await CursorHandler(host).execute(configured, task_context)
    assert host.spawn_count == 0


def test_cursor_from_request_redacts_secret_material_in_errors(tmp_path: Path) -> None:
    payload = valid_cursor_binding(tmp_path)
    payload["secret"] = SECRET_TEXT
    with pytest.raises(AdapterConfigurationError) as error:
        CursorAdapterConfig.from_request(request(binding_data=payload))
    assert SECRET_TEXT not in str(error.value)


async def test_cursor_reconcile_parses_binding_independently(tmp_path: Path) -> None:
    host = FakeConfinedProcessHost(stdout=complete_stream(str(tmp_path.resolve())))
    task_context, port = context(tmp_path)
    configured = request(binding_data=valid_cursor_binding(tmp_path))
    handler = CursorHandler(host)
    await handler.execute(configured, task_context)
    drifted = configured.model_copy(update={"binding_data": {"executable": "/missing/cursor"}})
    result = await handler.reconcile(drifted, task_context, port.snapshot)
    assert result.status == "indeterminate"
    assert result.reason is not None
    assert "executable" in result.reason
