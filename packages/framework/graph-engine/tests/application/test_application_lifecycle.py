from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from graph_engine.application import (
    AssuranceApplication,
    AssuranceRuntimeContext,
    FixedExecutionFactory,
    InvocationStatus,
    RevisionMismatch,
)
from graph_engine.boot.graph_revision import BootArtifact, GraphBuildManifest, GraphRevision
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease


class ExecuteState(TypedDict):
    change_id: str


def _artifact(entrypoints: dict[str, object], *, lock: str = "b" * 64) -> BootArtifact:
    names = tuple(entrypoints)
    revision = GraphRevision.build(
        product_lock_digest=lock,
        wheel_source_digests={"assurance.product": "d" * 64},
        factory_symbols=("assurance_product.graphs.factory:build_product_graphs",),
        state_schema_versions={name: "1" for name in names},
        langgraph_version="1.2.11",
        checkpoint_contract_version="1",
    )
    return BootArtifact(
        manifest=GraphBuildManifest(
            revision=revision,
            entrypoint_contract_digests={name: canonical_digest({"entrypoint": name}) for name in names},
            attempt_contract_digests={},
        ),
        entrypoints=entrypoints,  # type: ignore[arg-type]
        attempt_contracts={},
        checkpointer_backend_id="memory",
    )


def _factory(artifact: BootArtifact) -> FixedExecutionFactory:
    return FixedExecutionFactory(
        artifact=artifact,
        attempt_kernel=object(),
        secret_resolver=object(),
        workspace_provider=object(),
    )


def _execute_graph(runs: dict[str, int] | None = None):
    def execute(state: ExecuteState) -> ExecuteState:
        if runs is not None:
            runs["execute"] = runs.get("execute", 0) + 1
        return state

    builder = StateGraph(ExecuteState)
    builder.add_node("execute", execute)
    builder.add_edge(START, "execute")
    builder.add_edge("execute", END)
    return builder.compile(checkpointer=InMemorySaver())


@pytest.fixture
def artifact() -> BootArtifact:
    return _artifact({"execute": _execute_graph()})


@pytest.fixture
def context(artifact: BootArtifact) -> AssuranceRuntimeContext:
    return AssuranceRuntimeContext(
        revision_id=artifact.manifest.revision.revision_id,
        fencing_token=1,
        attempt_kernel=object(),
        secret_resolver=object(),
        workspace_provider=object(),
    )


@pytest.fixture
def application(tmp_path: Path) -> AssuranceApplication:
    return AssuranceApplication(lease=LocalInvocationRunnerLease(tmp_path), owner_id="runner-a")


async def test_start_pins_identity_and_run_uses_same_thread(application, artifact, context) -> None:
    del context
    started = await application.start(
        invocation_id="inv-1",
        entrypoint="execute",
        graph_input={"change_id": "chg-1"},
        execution_factory=_factory(artifact),
    )
    assert started.thread_id == "inv-1"
    assert started.revision_id == artifact.manifest.revision.revision_id
    result = await application.run(
        invocation_id="inv-1",
        execution_factory=_factory(artifact),
    )
    assert result.status == "completed"


async def test_start_does_not_execute_business_node_and_status_is_running(
    application: AssuranceApplication,
    tmp_path: Path,
) -> None:
    del tmp_path
    runs = {"execute": 0}
    artifact = _artifact({"execute": _execute_graph(runs)})
    context = AssuranceRuntimeContext(
        revision_id=artifact.manifest.revision.revision_id,
        fencing_token=1,
        attempt_kernel=object(),
        secret_resolver=object(),
        workspace_provider=object(),
    )
    await application.start(
        invocation_id="inv-1",
        entrypoint="execute",
        graph_input={"change_id": "chg-1"},
        execution_factory=_factory(artifact),
    )
    assert runs["execute"] == 0
    assert await application.status(
        artifact=artifact,
        invocation_id="inv-1",
        runtime_context=context,
    ) == InvocationStatus(status="running")
    snapshot = await artifact.entrypoints["execute"].aget_state({"configurable": {"thread_id": "inv-1"}})
    assert snapshot.config["configurable"]["thread_id"] == "inv-1"
    assert snapshot.metadata is not None
    assert snapshot.metadata["assurance_revision_id"] == artifact.manifest.revision.revision_id
    assert (
        snapshot.metadata["assurance_product_lock_digest"] == artifact.manifest.revision.product_lock_digest
    )
    assert snapshot.metadata["assurance_root_input_digest"] == canonical_digest({"change_id": "chg-1"})
    assert snapshot.metadata["assurance_fencing_token"] == 1
    assert snapshot.metadata["assurance_initial_checkpoint"] is True


async def test_start_and_run_completes_on_same_thread(
    application: AssuranceApplication,
    artifact: BootArtifact,
    context: AssuranceRuntimeContext,
) -> None:
    result = await application.start_and_run(
        invocation_id="inv-1",
        entrypoint="execute",
        graph_input={"change_id": "chg-1"},
        execution_factory=_factory(artifact),
    )
    assert result == InvocationStatus(status="completed")
    assert await application.status(
        artifact=artifact,
        invocation_id="inv-1",
        runtime_context=context,
    ) == InvocationStatus(status="completed")


async def test_run_rejects_revision_mismatch_before_invocation(
    application: AssuranceApplication,
    artifact: BootArtifact,
    context: AssuranceRuntimeContext,
) -> None:
    await application.start(
        invocation_id="inv-1",
        entrypoint="execute",
        graph_input={"change_id": "chg-1"},
        execution_factory=_factory(artifact),
    )
    other = _artifact({"execute": artifact.entrypoints["execute"]}, lock="e" * 64)
    with pytest.raises(RevisionMismatch, match=artifact.manifest.revision.revision_id):
        await application.run(
            invocation_id="inv-1",
            execution_factory=_factory(other),
        )
