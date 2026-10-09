from __future__ import annotations

import pytest

from graph_engine.plugin_api import WorkspaceProvider

from graph_engine.application.runtime_context import (
    AssuranceRuntimeContext,
    AttemptKernelPort,
    SecretResolverPort,
)
from graph_engine.canonical import canonical_json_bytes


def test_runtime_context_is_not_graph_state_or_json_serializable(
    kernel: AttemptKernelPort,
    secrets: SecretResolverPort,
    workspaces: WorkspaceProvider,
) -> None:
    context = AssuranceRuntimeContext(
        revision_id="a" * 64,
        fencing_token=7,
        attempt_kernel=kernel,
        secret_resolver=secrets,
        workspace_provider=workspaces,
    )
    assert "attempt_kernel" not in context.checkpoint_projection()
    with pytest.raises(TypeError):
        canonical_json_bytes(context)


def test_checkpoint_projection_is_data_only_and_excludes_service_refs(
    kernel: AttemptKernelPort,
    secrets: SecretResolverPort,
    workspaces: WorkspaceProvider,
) -> None:
    context = AssuranceRuntimeContext(
        revision_id="a" * 64,
        fencing_token=7,
        attempt_kernel=kernel,
        secret_resolver=secrets,
        workspace_provider=workspaces,
    )
    projection = context.checkpoint_projection()
    assert projection == {"revision_id": "a" * 64, "fencing_token": 7}
    assert "secret_resolver" not in projection
    assert "workspace_provider" not in projection
    assert canonical_json_bytes(projection) == canonical_json_bytes(
        {"fencing_token": 7, "revision_id": "a" * 64}
    )


@pytest.fixture
def kernel() -> AttemptKernelPort:
    return object()


@pytest.fixture
def secrets() -> SecretResolverPort:
    return object()


@pytest.fixture
def workspaces() -> WorkspaceProvider:
    return object()
