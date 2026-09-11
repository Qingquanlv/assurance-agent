from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict, cast

import pytest

from tests.product.unused_runtime_ports import UNUSED_SECRET_RESOLVER, UNUSED_WORKSPACE_PROVIDER


def test_modular_resume_against_legacy_lock_leaves_ledger_bytes_unchanged(
    tmp_path: Path, installed_sources
) -> None:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from graph_engine.application import AssuranceApplication, FixedExecutionFactory, RevisionMismatch
    from graph_engine.boot.graph_revision import BootArtifact, GraphBuildManifest, GraphRevision
    from graph_engine.canonical import canonical_digest
    from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease

    del installed_sources
    legacy_lock = "b" * 64
    modular_lock = "c" * 64
    assert legacy_lock != modular_lock

    class _State(TypedDict, total=False):
        value: str

    def execute(state: _State) -> _State:
        return state

    builder = StateGraph(_State)
    builder.add_node("execute", execute)
    builder.add_edge(START, "execute")
    builder.add_edge("execute", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    def _artifact(lock: str) -> BootArtifact:
        revision = GraphRevision.build(
            product_lock_digest=lock,
            wheel_source_digests={"assurance.product": "d" * 64},
            factory_symbols=("assurance_product.graphs.factory:build_product_graphs",),
            state_schema_versions={"intake": "1"},
            langgraph_version="1.2.11",
            checkpoint_contract_version="1",
        )
        return BootArtifact(
            manifest=GraphBuildManifest(
                revision=revision,
                entrypoint_contract_digests={"intake": canonical_digest({"entrypoint": "intake"})},
                attempt_contract_digests={},
            ),
            entrypoints={"intake": graph},
            attempt_contracts={},
            checkpointer_backend_id="memory",
        )

    original = _artifact(legacy_lock)
    drifted = _artifact(modular_lock)
    lease_root = tmp_path / "replay-legacy-lock"
    lease_root.mkdir()
    application = AssuranceApplication(lease=LocalInvocationRunnerLease(lease_root), owner_id="runner-a")

    def _factory(artifact: BootArtifact) -> FixedExecutionFactory:
        return FixedExecutionFactory(
            artifact=artifact,
            attempt_kernel=cast(Any, object()),
            secret_resolver=UNUSED_SECRET_RESOLVER,
            workspace_provider=UNUSED_WORKSPACE_PROVIDER,
        )

    async def _run() -> None:
        await application.start(
            invocation_id="inv-replay-legacy-001",
            entrypoint="intake",
            graph_input={"change_id": "CH-1"},
            execution_factory=_factory(original),
        )

        def _durable() -> tuple[tuple[Path, bytes], ...]:
            return tuple(
                sorted(
                    (path, path.read_bytes())
                    for path in lease_root.rglob("*")
                    if path.is_file() and path.name != "runner.json"
                )
            )

        before = _durable()
        with pytest.raises(RevisionMismatch):
            await application.run(
                invocation_id="inv-replay-legacy-001",
                execution_factory=_factory(drifted),
            )
        after = _durable()
        assert after == before

    import asyncio

    asyncio.run(_run())
