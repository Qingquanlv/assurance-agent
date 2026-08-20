from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import TaskContext, TaskHandler, TaskOutcome, TaskRequest
from graph_engine.product import load_plugin_entrypoint, load_product_entrypoint, resolve_product
from graph_engine.runtime.engine import Engine, EngineError, RunResult
from graph_engine.runtime.events import EventEnvelope
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import ActivationRecord


class _InProcessTestHost:
    """Deliberately unconfined test double; never a production host."""

    async def execute(
        self,
        handler: TaskHandler,
        request: TaskRequest,
        *,
        workspace_root: Path,
        heartbeat: Callable[[], None],
    ) -> TaskOutcome:
        return await handler(
            request,
            TaskContext(workspace_root=workspace_root, heartbeat=heartbeat),
        )


def _activation(result: RunResult, graph_instance_id: str, node_id: str) -> ActivationRecord:
    return next(
        activation
        for activation in result.projection.activations
        if activation.graph_instance_id == graph_instance_id and activation.node_id == node_id
    )


def _event_id_sequence(envelopes: tuple[EventEnvelope, ...]) -> tuple[tuple[object, ...], ...]:
    identity_fields = (
        "invocation_id",
        "graph_instance_id",
        "token_id",
        "activation_id",
        "task_id",
        "interrupt_id",
    )
    return tuple(
        (
            envelope.seq,
            envelope.event.kind,
            *(getattr(envelope.event, field) for field in identity_fields if hasattr(envelope.event, field)),
        )
        for envelope in envelopes
    )


def _run_to_completion(
    root: Path,
    *,
    invocation_id: str,
) -> tuple[str, tuple[tuple[object, ...], ...], str, JSONValue]:
    product = load_product_entrypoint("toy-b")
    plugin = load_plugin_entrypoint("toy-b")
    resolved = resolve_product(product, {"toy.b": plugin})
    with Engine(root, host=_InProcessTestHost()) as engine:
        with engine.start(resolved, entrypoint="review", invocation_id=invocation_id) as handle:
            blocked = engine.run_until_blocked(handle)
            assert blocked.status == "interrupted"
            with engine.resume(
                handle,
                action="approve",
                payload={"reviewer": "Ada"},
            ) as resumed:
                completed = engine.run_until_blocked(resumed)
                assert completed.status == "succeeded"
                with resumed.workspace as workspace:
                    final_tree_id = workspace.head_tree_id()
                envelopes = Ledger(resumed.invocation_root / "ledger").read_all()
                terminal_output = cast(
                    JSONValue,
                    completed.model_dump(mode="json")["output"],
                )
    return resolved.workflow.digest, _event_id_sequence(envelopes), final_tree_id, terminal_output


def test_toy_b_recovers_then_interrupts_and_resumes(tmp_path: Path) -> None:
    product = load_product_entrypoint("toy-b")
    plugin = load_plugin_entrypoint("toy-b")
    resolved = resolve_product(product, {"toy.b": plugin})

    assert tuple(resolved.registry.task_handlers) == (
        "toy.b.child",
        "toy.b.combine",
        "toy.b.left",
        "toy.b.seed",
    )
    root = resolved.workflow.graphs["root"]
    child = resolved.workflow.graphs["child"]
    left_claims = root.nodes["left"].definition.resources
    child_claims = child.nodes["child"].definition.resources
    assert left_claims.writes == ("left.txt",)
    assert child_claims.writes == ("child.txt",)
    assert set(left_claims.writes).isdisjoint(child_claims.writes)

    with Engine(tmp_path / "engine", host=_InProcessTestHost()) as engine:
        with engine.start(resolved, entrypoint="review", invocation_id="toy-b-1") as handle:
            blocked = engine.run_until_blocked(handle)
            assert blocked.status == "interrupted"
            assert blocked.actions == ("approve", "reject")
            assert blocked.reason == "review combined result"

            left = _activation(blocked, "root", "left")
            assert tuple(attempt.attempt for attempt in left.attempts) == (1, 2)
            assert left.attempts[0].failure is not None
            assert left.attempts[0].failure.kind == "transient"
            assert left.attempts[1].status == "succeeded"

            child_subgraph = _activation(blocked, "root", "child-subgraph")
            child_graph = next(
                graph
                for graph in blocked.projection.graph_instances
                if graph.parent_activation_id == child_subgraph.activation_id
            )
            assert child_graph.graph_instance_id == canonical_digest(
                {
                    "parent_activation_id": child_subgraph.activation_id,
                    "graph_id": "child",
                }
            )
            assert child_graph.output == {"child": True}
            assert child_subgraph.output == {"child": True}

            joined = _activation(blocked, "root", "joined")
            assert len(joined.token_ids) == 2
            assert joined.model_dump(mode="json")["output"] == {"tokens": [{"left": True}, {"child": True}]}
            combined = _activation(blocked, "root", "combine")
            assert combined.output == {"combined": True}

            with engine.resume(
                handle,
                action="approve",
                payload={"reviewer": "Ada"},
            ) as resumed:
                with pytest.raises(EngineError, match="no pending interrupt"):
                    engine.resume(handle, action="approve", payload={"reviewer": "Grace"})
                completed = engine.run_until_blocked(resumed)
                assert completed.status == "succeeded"
                assert completed.output == {
                    "action": "approve",
                    "payload": {"reviewer": "Ada"},
                }
                with resumed.workspace as workspace:
                    assert workspace.read_head("left.txt") == b"left\n"
                    assert workspace.read_head("child.txt") == b"child\n"


def test_toy_b_replay_is_deterministic_and_products_are_separate(tmp_path: Path) -> None:
    first = _run_to_completion(tmp_path / "first", invocation_id="toy-b-replay")
    second = _run_to_completion(tmp_path / "second", invocation_id="toy-b-replay")
    assert second == first

    toy_a_product = load_product_entrypoint("toy-a")
    toy_a_plugin = load_plugin_entrypoint("toy-a")
    toy_a = resolve_product(toy_a_product, {"toy.a": toy_a_plugin})
    toy_b_product = load_product_entrypoint("toy-b")
    toy_b_plugin = load_plugin_entrypoint("toy-b")
    toy_b = resolve_product(toy_b_product, {"toy.b": toy_b_plugin})

    assert toy_a.manifest.product_id == "toy.a"
    assert toy_b.manifest.product_id == "toy.b"
    assert toy_a.digest != toy_b.digest
    assert not any(capability.startswith("toy.b.") for capability in toy_a.registry.task_handlers)
    assert not any(capability.startswith("toy.a.") for capability in toy_b.registry.task_handlers)
