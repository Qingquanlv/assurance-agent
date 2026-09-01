from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from graph_engine.application import (
    AssuranceApplication,
    AssuranceRuntimeContext,
    InvocationStatus,
)
from graph_engine.boot.graph_revision import BootArtifact, GraphBuildManifest, GraphRevision
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease, RunnerConflict


class HoldState(TypedDict):
    marker: str


class RecurseState(TypedDict):
    n: int


class BudgetState(TypedDict):
    remaining: int
    terminal: dict[str, str] | None


def _artifact(entrypoints: dict[str, object]) -> BootArtifact:
    names = tuple(entrypoints)
    revision = GraphRevision.build(
        product_lock_digest="b" * 64,
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


def _context(artifact: BootArtifact) -> AssuranceRuntimeContext:
    return AssuranceRuntimeContext(
        revision_id=artifact.manifest.revision.revision_id,
        fencing_token=1,
        attempt_kernel=object(),
        secret_resolver=object(),
        workspace_provider=object(),
    )


def _hold_graph(entered: asyncio.Event, release: asyncio.Event):
    async def hold(state: HoldState) -> HoldState:
        entered.set()
        await release.wait()
        return state

    builder = StateGraph(HoldState)
    builder.add_node("hold", hold)
    builder.add_edge(START, "hold")
    builder.add_edge("hold", END)
    return builder.compile(checkpointer=InMemorySaver())


def _recurse_graph():
    def loop(state: RecurseState) -> RecurseState:
        return {"n": state["n"] + 1}

    builder = StateGraph(RecurseState)
    builder.add_node("loop", loop)
    builder.add_edge(START, "loop")
    builder.add_edge("loop", "loop")
    return builder.compile(checkpointer=InMemorySaver())


def _budget_graph():
    def tick(state: BudgetState) -> BudgetState:
        remaining = state["remaining"] - 1
        if remaining <= 0:
            return {
                "remaining": 0,
                "terminal": {"status": "completed", "reason": "round_budget_exhausted"},
            }
        return {"remaining": remaining, "terminal": None}

    def route(state: BudgetState) -> str:
        return END if state["remaining"] <= 0 else "tick"

    builder = StateGraph(BudgetState)
    builder.add_node("tick", tick)
    builder.add_edge(START, "tick")
    builder.add_conditional_edges("tick", route, {END: END, "tick": "tick"})
    return builder.compile(checkpointer=InMemorySaver())


async def test_concurrent_run_produces_one_owner_and_one_runner_conflict(tmp_path: Path) -> None:
    lease = LocalInvocationRunnerLease(tmp_path)
    owner = AssuranceApplication(lease=lease, owner_id="runner-a")
    contender = AssuranceApplication(lease=lease, owner_id="runner-b")
    entered = asyncio.Event()
    release = asyncio.Event()
    artifact = _artifact({"execute": _hold_graph(entered, release)})
    context = _context(artifact)
    await owner.start(
        artifact=artifact,
        invocation_id="inv-1",
        entrypoint="execute",
        graph_input={"marker": "go"},
        runtime_context=context,
    )

    async def run_owner() -> InvocationStatus:
        return await owner.run(
            artifact=artifact,
            invocation_id="inv-1",
            runtime_context=context,
        )

    async def run_contender() -> None:
        await entered.wait()
        with pytest.raises(RunnerConflict):
            await contender.run(
                artifact=artifact,
                invocation_id="inv-1",
                runtime_context=context,
            )
        release.set()

    owned, _ = await asyncio.gather(run_owner(), run_contender())
    assert owned == InvocationStatus(status="completed")


async def test_graph_recursion_limit_is_failed_runtime_state(tmp_path: Path) -> None:
    application = AssuranceApplication(
        lease=LocalInvocationRunnerLease(tmp_path),
        owner_id="runner-a",
        recursion_limits={"recurse": 3},
    )
    artifact = _artifact({"recurse": _recurse_graph()})
    context = _context(artifact)
    await application.start(
        artifact=artifact,
        invocation_id="inv-recurse",
        entrypoint="recurse",
        graph_input={"n": 0},
        runtime_context=context,
    )
    result = await application.run(
        artifact=artifact,
        invocation_id="inv-recurse",
        runtime_context=context,
    )
    assert result == InvocationStatus(status="failed", reason="graph_recursion_limit")


async def test_business_budget_terminal_is_distinct_from_graph_recursion(tmp_path: Path) -> None:
    application = AssuranceApplication(
        lease=LocalInvocationRunnerLease(tmp_path),
        owner_id="runner-a",
    )
    artifact = _artifact({"budget": _budget_graph()})
    context = _context(artifact)
    await application.start(
        artifact=artifact,
        invocation_id="inv-budget",
        entrypoint="budget",
        graph_input={"remaining": 2, "terminal": None},
        runtime_context=context,
    )
    result = await application.run(
        artifact=artifact,
        invocation_id="inv-budget",
        runtime_context=context,
    )
    assert result == InvocationStatus(status="completed", reason="round_budget_exhausted")
    assert result.reason != "graph_recursion_limit"
