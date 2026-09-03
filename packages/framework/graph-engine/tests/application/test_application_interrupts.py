from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from graph_engine.application import (
    AmbiguousResume,
    AssuranceApplication,
    AssuranceRuntimeContext,
    InvalidResume,
    InvocationStatus,
)
from graph_engine.attempts.resolutions import PendingTaskResult, SystemReference
from graph_engine.boot.graph_revision import BootArtifact, GraphBuildManifest, GraphRevision
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease


class HumanState(TypedDict):
    decision: str


class DualHumanState(TypedDict):
    left: str
    right: str


class SystemState(TypedDict):
    settled: str


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


def _human_graph():
    def approve(state: HumanState) -> HumanState:
        raw = interrupt({"kind": "human", "actions": ["approve", "reject"]})
        return {"decision": str(raw)}

    builder = StateGraph(HumanState)
    builder.add_node("approve", approve)
    builder.add_edge(START, "approve")
    builder.add_edge("approve", END)
    return builder.compile(checkpointer=InMemorySaver())


def _dual_human_graph():
    def left(state: DualHumanState) -> dict[str, str]:
        raw = interrupt({"kind": "human", "actions": ["approve", "reject"]})
        return {"left": str(raw)}

    def right(state: DualHumanState) -> dict[str, str]:
        raw = interrupt({"kind": "human", "actions": ["approve", "reject"]})
        return {"right": str(raw)}

    builder = StateGraph(DualHumanState)
    builder.add_node("left", left)
    builder.add_node("right", right)
    builder.add_edge(START, "left")
    builder.add_edge(START, "right")
    builder.add_edge("left", END)
    builder.add_edge("right", END)
    return builder.compile(checkpointer=InMemorySaver())


def _system_graph():
    def settle(state: SystemState) -> SystemState:
        raw = interrupt({"kind": "system_wake", "reason": "effect_pending"})
        if isinstance(raw, dict) and "wakeup" in raw:
            return {"settled": str(raw["wakeup"]["reference_id"])}
        return {"settled": str(raw)}

    builder = StateGraph(SystemState)
    builder.add_node("settle", settle)
    builder.add_edge(START, "settle")
    builder.add_edge("settle", END)
    return builder.compile(checkpointer=InMemorySaver())


@pytest.fixture
def application(tmp_path: Path) -> AssuranceApplication:
    return AssuranceApplication(lease=LocalInvocationRunnerLease(tmp_path), owner_id="runner-a")


def _context(artifact: BootArtifact) -> AssuranceRuntimeContext:
    return AssuranceRuntimeContext(
        revision_id=artifact.manifest.revision.revision_id,
        fencing_token=1,
        attempt_kernel=object(),
        secret_resolver=object(),
        workspace_provider=object(),
    )


async def _start_and_block(
    application: AssuranceApplication,
    artifact: BootArtifact,
    *,
    invocation_id: str,
    entrypoint: str,
    graph_input: dict[str, str],
) -> tuple[AssuranceRuntimeContext, InvocationStatus]:
    context = _context(artifact)
    await application.start(
        artifact=artifact,
        invocation_id=invocation_id,
        entrypoint=entrypoint,
        graph_input=graph_input,
        runtime_context=context,
    )
    blocked = await application.run(
        artifact=artifact,
        invocation_id=invocation_id,
        runtime_context=context,
    )
    return context, blocked


async def test_single_human_interrupt_accepts_validated_scalar(application: AssuranceApplication) -> None:
    artifact = _artifact({"execute": _human_graph()})
    context, blocked = await _start_and_block(
        application,
        artifact,
        invocation_id="inv-human",
        entrypoint="execute",
        graph_input={"decision": ""},
    )
    assert blocked == InvocationStatus(status="interrupted")
    result = await application.resume(
        artifact=artifact,
        invocation_id="inv-human",
        runtime_context=context,
        resume="approve",
    )
    assert result == InvocationStatus(status="completed")


async def test_single_human_interrupt_rejects_invalid_scalar(application: AssuranceApplication) -> None:
    artifact = _artifact({"execute": _human_graph()})
    context, _blocked = await _start_and_block(
        application,
        artifact,
        invocation_id="inv-bad",
        entrypoint="execute",
        graph_input={"decision": ""},
    )
    with pytest.raises(ValidationError):
        await application.resume(
            artifact=artifact,
            invocation_id="inv-bad",
            runtime_context=context,
            resume="not-an-action",
        )


async def test_disallowed_human_action_raises_with_actual_value(
    application: AssuranceApplication,
) -> None:
    artifact = _artifact({"execute": _human_graph()})
    context, _blocked = await _start_and_block(
        application,
        artifact,
        invocation_id="inv-rework",
        entrypoint="execute",
        graph_input={"decision": ""},
    )
    with pytest.raises(InvalidResume, match="rework") as exc_info:
        await application.resume(
            artifact=artifact,
            invocation_id="inv-rework",
            runtime_context=context,
            resume="rework",
        )
    assert "not-an-action" not in str(exc_info.value)


async def test_multiple_interrupts_require_interrupt_id_mapping(application: AssuranceApplication) -> None:
    artifact = _artifact({"execute": _dual_human_graph()})
    context, blocked = await _start_and_block(
        application,
        artifact,
        invocation_id="inv-multi",
        entrypoint="execute",
        graph_input={"left": "", "right": ""},
    )
    assert blocked == InvocationStatus(status="interrupted")
    with pytest.raises(AmbiguousResume):
        await application.resume(
            artifact=artifact,
            invocation_id="inv-multi",
            runtime_context=context,
            resume="approve",
        )
    snapshot = await artifact.entrypoints["execute"].aget_state({"configurable": {"thread_id": "inv-multi"}})
    mapping = {item.id: "approve" for item in snapshot.interrupts}
    result = await application.resume(
        artifact=artifact,
        invocation_id="inv-multi",
        runtime_context=context,
        resume=mapping,
    )
    assert result == InvocationStatus(status="completed")


async def test_system_interrupt_accepts_only_wakeup_envelope(application: AssuranceApplication) -> None:
    artifact = _artifact({"execute": _system_graph()})
    context, blocked = await _start_and_block(
        application,
        artifact,
        invocation_id="inv-system",
        entrypoint="execute",
        graph_input={"settled": ""},
    )
    assert blocked == InvocationStatus(status="blocked", reason="effect_pending")
    with pytest.raises(InvalidResume):
        await application.resume(
            artifact=artifact,
            invocation_id="inv-system",
            runtime_context=context,
            resume="approve",
        )
    result = await application.resume(
        artifact=artifact,
        invocation_id="inv-system",
        runtime_context=context,
        resume=PendingTaskResult(wakeup=SystemReference(reference_id="wake-1")),
    )
    assert result == InvocationStatus(status="completed")
