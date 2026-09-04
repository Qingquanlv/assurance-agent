from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from graph_engine.application import (
    AssuranceApplication,
    FixedExecutionFactory,
    InvalidResume,
    RevisionMismatch,
)
from graph_engine.boot.graph_revision import BootArtifact, GraphBuildManifest, GraphRevision
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease

pytestmark = pytest.mark.usefixtures("product_runner")

_FORBIDDEN_TREE_NAMES = frozenset({"workspace", "trees", "attempts", "HEAD.json"})


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
        attempt_kernel=cast(Any, object()),
        secret_resolver=object(),
        workspace_provider=object(),
    )


def test_invalid_resume_input_fails(tmp_path: Path) -> None:
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    class _State(TypedDict, total=False):
        decision: str

    def approve(state: _State) -> _State:
        raw = interrupt({"kind": "human", "actions": ["approve", "reject"]})
        return {"decision": str(raw)}

    builder = StateGraph(_State)
    builder.add_node("approve", approve)
    builder.add_edge(START, "approve")
    builder.add_edge("approve", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    artifact = _artifact({"execute": graph})
    application = AssuranceApplication(lease=LocalInvocationRunnerLease(tmp_path), owner_id="runner-a")

    async def _run() -> None:
        await application.start(
            invocation_id="inv-invalid",
            entrypoint="execute",
            graph_input={"decision": ""},
            execution_factory=_factory(artifact),
        )
        blocked = await application.run(
            invocation_id="inv-invalid",
            execution_factory=_factory(artifact),
        )
        assert blocked.status == "interrupted"
        with pytest.raises((InvalidResume, ValueError, TypeError)):
            await application.resume(
                invocation_id="inv-invalid",
                execution_factory=_factory(artifact),
                resume={"decision": "not-allowed", "extra": "field"},
            )

    import asyncio

    asyncio.run(_run())


def test_revision_mismatch_rejects_drifted_resume(tmp_path: Path) -> None:
    from langgraph.graph import END, START, StateGraph

    class _State(TypedDict, total=False):
        value: str

    def execute(state: _State) -> _State:
        return state

    builder = StateGraph(_State)
    builder.add_node("execute", execute)
    builder.add_edge(START, "execute")
    builder.add_edge("execute", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    artifact = _artifact({"execute": graph})
    other = _artifact({"execute": graph}, lock="e" * 64)
    application = AssuranceApplication(lease=LocalInvocationRunnerLease(tmp_path), owner_id="runner-a")

    async def _run() -> None:
        await application.start(
            invocation_id="inv-drift",
            entrypoint="execute",
            graph_input={"change_id": "CH-1"},
            execution_factory=_factory(artifact),
        )
        with pytest.raises(RevisionMismatch):
            await application.run(
                invocation_id="inv-drift",
                execution_factory=_factory(other),
            )

    import asyncio

    asyncio.run(_run())


def test_interrupt_runtime_lives_under_the_change_without_tree_store(tmp_path: Path) -> None:
    change = tmp_path / "qa" / "changes" / "CH-DEMO-001"
    runtime = change / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "leases").mkdir()
    names = {path.name for path in change.rglob("*")}
    assert names.isdisjoint(_FORBIDDEN_TREE_NAMES)
    assert runtime.parent.parent.name == "changes"
    assert runtime.parent.parent.parent.name == "qa"
