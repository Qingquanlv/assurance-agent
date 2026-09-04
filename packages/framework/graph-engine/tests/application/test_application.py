from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from graph_engine.application import (
    AssuranceApplication,
    AssuranceRuntimeContext,
    InvocationBoundExecution,
    InvocationStatus,
)
from graph_engine.application.application import _read_only_artifact
from graph_engine.boot.graph_revision import BootArtifact, GraphBuildManifest, GraphRevision
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease, RunnerLease


class ExecuteState(TypedDict):
    change_id: str


def _artifact() -> BootArtifact:
    def execute(state: ExecuteState) -> ExecuteState:
        return state

    builder = StateGraph(ExecuteState)
    builder.add_node("execute", execute)
    builder.add_edge(START, "execute")
    builder.add_edge("execute", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    revision = GraphRevision.build(
        product_lock_digest="b" * 64,
        wheel_source_digests={"assurance.product": "d" * 64},
        factory_symbols=("assurance_product.graphs.factory:build_product_graphs",),
        state_schema_versions={"execute": "1"},
        langgraph_version="1.2.11",
        checkpoint_contract_version="1",
    )
    return BootArtifact(
        manifest=GraphBuildManifest(
            revision=revision,
            entrypoint_contract_digests={"execute": canonical_digest({"entrypoint": "execute"})},
            attempt_contract_digests={},
        ),
        entrypoints={"execute": graph},  # type: ignore[arg-type]
        attempt_contracts={},
        checkpointer_backend_id="memory",
    )


class RecordingExecutionFactory:
    def __init__(self, artifact: BootArtifact) -> None:
        self._artifact = artifact
        self.bound_fences: list[int] = []
        self.bind_count = 0

    def bind(self, runner_lease: RunnerLease) -> InvocationBoundExecution:
        self.bind_count += 1
        self.bound_fences.append(runner_lease.fencing_token)
        return InvocationBoundExecution(
            artifact=self._artifact,
            runtime_context=AssuranceRuntimeContext(
                revision_id=self._artifact.manifest.revision.revision_id,
                fencing_token=runner_lease.fencing_token,
                attempt_kernel=object(),
                secret_resolver=object(),
                workspace_provider=object(),
            ),
        )


@dataclass
class _ApplicationFixture:
    application: AssuranceApplication
    lease: LocalInvocationRunnerLease
    artifact: BootArtifact


@pytest.fixture
def application_fixture(tmp_path: Path) -> _ApplicationFixture:
    artifact = _artifact()
    lease = LocalInvocationRunnerLease(tmp_path)
    application = AssuranceApplication(lease=lease, owner_id="runner-a")
    return _ApplicationFixture(application=application, lease=lease, artifact=artifact)


async def test_execution_binding_observes_the_acquired_fence(application_fixture) -> None:
    starter = RecordingExecutionFactory(application_fixture.artifact)
    await application_fixture.application.start(
        invocation_id="inv-1",
        entrypoint="execute",
        graph_input={"change_id": "chg-1"},
        execution_factory=starter,
    )
    factory = RecordingExecutionFactory(application_fixture.artifact)
    await application_fixture.application.run(
        invocation_id="inv-1",
        execution_factory=factory,
    )
    assert factory.bound_fences == [application_fixture.lease.current("inv-1").fencing_token]
    assert factory.bind_count == 1


async def test_status_uses_read_only_view_without_acquiring_a_fence(application_fixture) -> None:
    factory = RecordingExecutionFactory(application_fixture.artifact)
    await application_fixture.application.start(
        invocation_id="inv-status",
        entrypoint="execute",
        graph_input={"change_id": "chg-1"},
        execution_factory=factory,
    )
    fence_after_start = application_fixture.lease.current("inv-status").fencing_token
    context = AssuranceRuntimeContext(
        revision_id=application_fixture.artifact.manifest.revision.revision_id,
        fencing_token=fence_after_start,
        attempt_kernel=object(),
        secret_resolver=object(),
        workspace_provider=object(),
    )
    result = await application_fixture.application.status(
        artifact=application_fixture.artifact,
        invocation_id="inv-status",
        runtime_context=context,
    )
    assert result == InvocationStatus(status="running")
    assert application_fixture.lease.current("inv-status").fencing_token == fence_after_start
    readonly = _read_only_artifact(application_fixture.artifact)
    graph = readonly.entrypoints["execute"]
    with pytest.raises(RuntimeError, match="read-only"):
        await graph.aupdate_state({}, {}, as_node="execute")
    with pytest.raises(RuntimeError, match="read-only"):
        await graph.ainvoke({}, {})
