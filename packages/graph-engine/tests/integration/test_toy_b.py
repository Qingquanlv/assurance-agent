from __future__ import annotations

from collections.abc import Mapping
import importlib
from pathlib import Path
import shutil
import sys
from typing import cast, get_args

import pytest

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.plugin_api import TaskContext, TaskHandler
from graph_engine.runtime.seed import empty_invocation_seed
from graph_engine.runtime.engine import Engine, EngineError, RunResult
from graph_engine.runtime.host_protocol import TaskHostCallResult, TaskHostExecuteCall
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphStarted,
    NodeActivated,
    RuntimeEvent,
    RuntimeEventModel,
)
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import ActivationRecord


def _toy_composition(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    toy: str,
) -> FrozenComposition:
    source = root / "source"
    repository = Path(__file__).parents[4]
    distribution = f"graph-engine-toy-{toy}"
    package_name = f"graph_engine_toy_{toy}"
    entrypoint_name = f"toy-{toy}"
    shutil.copytree(
        repository / "examples" / distribution,
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    source_files = tuple(
        sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
    )
    monkeypatch.syspath_prepend(str(source))
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    return RegistryPlatform().resolve(
        ResolutionRequest(
            product=EditableWheelProductSource(
                distribution=distribution,
                entrypoint_name=entrypoint_name,
                declaration_path=f"{package_name}/product-declaration.json",
                source_root=source,
                source_files=source_files,
            ),
            plugins=(
                EditableWheelPluginSource(
                    distribution=distribution,
                    entrypoint_name=entrypoint_name,
                    declaration_path=f"{package_name}/plugin-declaration.json",
                    source_root=source,
                    source_files=source_files,
                ),
            ),
        )
    )


def test_toy_b_static_declarations_match_live_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_composition(tmp_path, monkeypatch, "b")

    assert composition.manifest.product_id == "toy.b"
    assert tuple(descriptor.plugin_id for descriptor in composition.descriptors) == ("toy.b",)


class _InProcessTestHost:
    """Deliberately unconfined test double; never a production host."""

    def __init__(self) -> None:
        self._handlers: Mapping[str, TaskHandler] = {}
        self._store: object | None = None

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: object,
    ) -> None:
        self._handlers = handlers
        self._store = store

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        assert self._store is not None
        handler = self._handlers[call.request.capability_id]
        workspace_root = Path(self._store.root) / "attempts" / call.attempt_root.attempt_directory_id  # type: ignore[attr-defined]
        outcome = await handler.execute(
            call.request,
            TaskContext(
                workspace_root=workspace_root,
                heartbeat=lambda: None,
                cancel_requested=lambda: False,
                invocation=call.request.invocation,
            ),
        )
        return TaskHostCallResult(operation="execute", outcome=outcome)


def _activation(result: RunResult, graph_instance_id: str, node_id: str) -> ActivationRecord:
    return next(
        activation
        for activation in result.projection.activations
        if activation.graph_instance_id == graph_instance_id and activation.node_id == node_id
    )


EventIDValue = str | tuple[str, ...] | None
EventIDFields = tuple[tuple[str, EventIDValue], ...]
EventIDSignature = tuple[int, str, EventIDFields]

_ID_FIELDS_BY_EVENT_KIND: dict[str, tuple[str, ...]] = {
    "invocation_started": ("invocation_id", "initial_tree_id"),
    "graph_started": (
        "graph_instance_id",
        "graph_id",
        "parent_graph_instance_id",
        "parent_node_id",
        "parent_activation_id",
    ),
    "token_offered": ("token_id", "graph_instance_id"),
    "token_consumed": ("token_id", "graph_instance_id", "node_id"),
    "node_activated": ("activation_id", "graph_instance_id", "node_id", "token_ids"),
    "task_attempt_started": ("activation_id",),
    "task_lease_acquired": ("task_id", "activation_id", "owner_id"),
    "task_lease_heartbeat": ("task_id", "activation_id", "owner_id"),
    "task_activity_prepared": ("activity_id", "task_id", "activation_id"),
    "task_activity_dispatch_started": ("activity_id",),
    "task_activity_bound": ("activity_id",),
    "task_activity_cancel_requested": ("activity_id",),
    "task_activity_terminal_observed": ("activity_id", "candidate_tree_id"),
    "task_lease_adopted": ("activity_id", "task_id", "activation_id", "owner_id"),
    "task_commit_prepared": ("task_id", "activation_id", "previous_tree_id", "tree_id", "effect_ids"),
    "effect_intent_committed": ("effect_id", "activation_id"),
    "effect_apply_started": ("effect_id",),
    "effect_receipt_recorded": ("effect_id",),
    "task_attempt_succeeded": ("activation_id",),
    "task_attempt_failed": ("activation_id",),
    "task_attempt_stopped": ("activation_id",),
    "head_advanced": ("task_id", "activation_id", "previous_tree_id", "tree_id"),
    "node_completed": ("activation_id",),
    "node_failed": ("activation_id",),
    "node_interrupted": ("activation_id", "interrupt_id", "graph_instance_id"),
    "interrupt_resumed": ("interrupt_id",),
    "graph_completed": ("graph_instance_id",),
    "graph_failed": ("graph_instance_id",),
    "invocation_finished": ("invocation_id",),
}


def _runtime_event_types() -> tuple[type[RuntimeEventModel], ...]:
    event_union = get_args(RuntimeEvent)[0]
    return cast(tuple[type[RuntimeEventModel], ...], get_args(event_union))


def _runtime_event_id_schema() -> dict[str, tuple[str, ...]]:
    schema: dict[str, tuple[str, ...]] = {}
    for event_type in _runtime_event_types():
        event_kind = cast(str, event_type.model_fields["kind"].default)
        schema[event_kind] = tuple(
            field_name for field_name in event_type.model_fields if field_name.endswith(("_id", "_ids"))
        )
    return schema


def _event_id_fields(event: RuntimeEvent) -> EventIDFields:
    try:
        field_names = _ID_FIELDS_BY_EVENT_KIND[event.kind]
    except KeyError as error:
        raise AssertionError(f"event ID schema does not cover {event.kind!r}") from error
    return tuple((field_name, cast(EventIDValue, getattr(event, field_name))) for field_name in field_names)


def _event_id_sequence(envelopes: tuple[EventEnvelope, ...]) -> tuple[EventIDSignature, ...]:
    return tuple(
        (
            envelope.seq,
            envelope.event.kind,
            _event_id_fields(envelope.event),
        )
        for envelope in envelopes
    )


def test_event_id_projection_preserves_parent_and_complete_token_bindings() -> None:
    parent_activation_id = "parent-activation"
    child_graph_id = canonical_digest(
        {
            "parent_activation_id": parent_activation_id,
            "graph_id": "child",
        }
    )
    envelopes = (
        EventEnvelope.from_event(
            1,
            GraphStarted(
                graph_instance_id=child_graph_id,
                graph_id="child",
                parent_graph_instance_id="root",
                parent_node_id="child-subgraph",
                parent_activation_id=parent_activation_id,
            ),
        ),
        EventEnvelope.from_event(
            2,
            NodeActivated(
                activation_id="joined-activation",
                graph_instance_id="root",
                node_id="joined",
                token_ids=("left-token", "child-token"),
            ),
        ),
    )

    assert _event_id_sequence(envelopes) == (
        (
            1,
            "graph_started",
            (
                ("graph_instance_id", child_graph_id),
                ("graph_id", "child"),
                ("parent_graph_instance_id", "root"),
                ("parent_node_id", "child-subgraph"),
                ("parent_activation_id", parent_activation_id),
            ),
        ),
        (
            2,
            "node_activated",
            (
                ("activation_id", "joined-activation"),
                ("graph_instance_id", "root"),
                ("node_id", "joined"),
                ("token_ids", ("left-token", "child-token")),
            ),
        ),
    )


def test_event_id_projection_covers_every_runtime_event_id_field() -> None:
    assert _ID_FIELDS_BY_EVENT_KIND == _runtime_event_id_schema()


def _run_to_completion(
    root: Path,
    composition: FrozenComposition,
    *,
    invocation_id: str,
) -> tuple[str, tuple[EventIDSignature, ...], str, JSONValue]:
    with Engine(root, host=_InProcessTestHost()) as engine:
        with engine.start(composition, entrypoint="review", invocation_id=invocation_id, seed=empty_invocation_seed()) as handle:
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
    return composition.workflow.digest, _event_id_sequence(envelopes), final_tree_id, terminal_output


def test_toy_b_recovers_then_interrupts_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _toy_composition(tmp_path / "composition", monkeypatch, "b")

    assert tuple(resolved.registries.capabilities.task_handlers) == (
        "toy.b.child",
        "toy.b.combine",
        "toy.b.left",
        "toy.b.seed",
    )
    root = resolved.workflow.graphs["root"]
    child = resolved.workflow.graphs["child"]
    left_claims = root.nodes["left"].definition.resources
    child_subgraph_claims = root.nodes["child-subgraph"].definition.resources
    child_task_claims = child.nodes["child"].definition.resources
    assert left_claims.writes == ("left.txt",)
    assert child_subgraph_claims.writes == ("child.txt",)
    assert child_task_claims.writes == ("child.txt",)
    assert set(left_claims.writes).isdisjoint(child_subgraph_claims.writes)

    with Engine(tmp_path / "engine", host=_InProcessTestHost()) as engine:
        with engine.start(resolved, entrypoint="review", invocation_id="toy-b-1", seed=empty_invocation_seed()) as handle:
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

            with engine.open("toy-b-1", resolved) as replayed_handle:
                replayed = engine.run_until_blocked(replayed_handle)
                assert replayed.status == "succeeded"
                assert replayed.output == completed.output


def test_toy_b_replay_is_deterministic_and_products_are_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toy_b = _toy_composition(tmp_path / "toy-b", monkeypatch, "b")
    first = _run_to_completion(tmp_path / "first", toy_b, invocation_id="toy-b-replay")
    second = _run_to_completion(tmp_path / "second", toy_b, invocation_id="toy-b-replay")
    assert second == first

    toy_a = _toy_composition(tmp_path / "toy-a", monkeypatch, "a")

    assert toy_a.manifest.product_id == "toy.a"
    assert toy_b.manifest.product_id == "toy.b"
    assert toy_a.lock_digest != toy_b.lock_digest
    assert not any(
        capability.startswith("toy.b.") for capability in toy_a.registries.capabilities.task_handlers
    )
    assert not any(
        capability.startswith("toy.a.") for capability in toy_b.registries.capabilities.task_handlers
    )
