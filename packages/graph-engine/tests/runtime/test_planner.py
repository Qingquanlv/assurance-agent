from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from graph_engine.canonical import canonical_digest
from graph_engine.composition import (
    CapabilityRegistry,
    ExecutableBindingMode,
    ExecutableKind,
    ExecutableModuleProvenance,
    ExecutableProvenance,
    SourceIdentity,
    SourceKey,
    SourceKind,
    SourceRole,
    SourceSnapshot,
)
from graph_engine.composition.models import (
    AuthenticatedContribution,
    ExecutableAuthority,
    ContributionAuthority,
)
from graph_engine.composition.provenance import StandardLoader
from graph_engine.composition.registries import _build_registries
from graph_engine.graph.compiler import CompiledWorkflow, compile_workflow
from graph_engine.graph.schema import parse_workflow
from graph_engine.plugin_api import PluginContribution, PluginDescriptor, TaskFailure
from graph_engine.runtime.events import (
    EffectApplyStarted,
    EffectIntentCommitted,
    EffectReceiptRecorded,
    EventEnvelope,
    GraphStarted,
    HeadAdvanced,
    InvocationStarted,
    NodeActivated,
    NodeCompleted,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskAttemptSucceeded,
    TaskCommitPrepared,
    TaskLeaseAcquired,
    TaskLeaseHeartbeat,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.models import ActivationRecord, InvocationProjection, fold_events
from graph_engine.runtime.planner import (
    PlanningError,
    activation_id,
    plan_next,
    task_id,
    validate_event_history,
)


class _PlaceholderHandler:
    async def execute(self, _request: object, _context: object) -> object:
        raise AssertionError("planner tests never execute task handlers")


def _registry() -> CapabilityRegistry:
    source = SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.WHEEL_PLUGIN,
            root=Path("/sources/test.tasks"),
            distribution="test-tasks",
            version="1.0.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name="test.tasks",
            entrypoint_value="test_tasks:provider",
            declaration_path="test_tasks/plugin-declaration.json",
            import_roots=("",),
            plugin_id="test.tasks",
            plugin_version="1.0.0",
        ),
        (),
    )
    contribution = PluginContribution(task_handlers={"test.tasks.run": _PlaceholderHandler()})
    source_key = SourceKey(SourceRole.PLUGIN, "test.tasks")
    provenance = ExecutableProvenance.create(
        kind=ExecutableKind.TASK_HANDLER,
        registry_id="test.tasks.run",
        owner_id="test.tasks",
        source_key=source_key,
        source_digest=source.digest,
        module=ExecutableModuleProvenance(
            module_name="test_tasks.implementation",
            standard_loader=StandardLoader.SOURCE,
            standard_is_package=False,
            relative_origin="implementation.py",
            authenticated_locations=(),
            physical_sha256="0" * 64,
            source_digest=source.digest,
        ),
        callable_path="test_tasks.implementation:Handler.execute",
        binding_mode=ExecutableBindingMode.INSTANCE_METHOD,
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id="test.tasks",
        plugin_version="1.0.0",
        engine_api="1.0.0",
        task_handlers=("test.tasks.run",),
        commit_validators=(),
    )
    handler = contribution.task_handlers["test.tasks.run"]
    authority_set = ContributionAuthority(
        provider_binding=object(),
        descriptor=descriptor,
        owner_id="test.tasks",
        source_key=source_key,
        source_digest=source.digest,
        contribution=contribution,
        authorities=(
            ExecutableAuthority(
                executable=handler,
                function=type(handler).__dict__["execute"],
                bound_self=handler,
                descriptor=type(handler).__dict__["execute"],
                provenance=provenance,
            ),
        ),
    )
    authenticated = AuthenticatedContribution(
        owner_id="test.tasks",
        source_key=source_key,
        source_digest=source.digest,
        descriptor=descriptor,
        contribution=contribution,
        executables=(provenance,),
        authority=authority_set,
    )
    return _build_registries((source,), (authenticated,), ("test.tasks",)).capabilities


def _compiled(
    nodes: str,
    edges: str,
    *,
    start: str,
    maximum: int = 20,
    retry_on: str = "[]",
    max_attempts: int = 2,
    extra_graphs: str = "",
) -> CompiledWorkflow:
    text = f"""
name: planner-test
entrypoints: {{main: root}}
retry:
  policy: {{max_attempts: {max_attempts}, retry_on: {retry_on}}}
timeout:
  short: {{run_seconds: 5}}
graphs:
  root:
    max_activations: {maximum}
    start: {start}
    nodes:
{nodes}
    edges:
{edges}
{extra_graphs}
"""
    return compile_workflow(parse_workflow(text), _registry())


def _task_node(name: str, *, input_: str = "") -> str:
    suffix = f", input: {input_}" if input_ else ""
    return f"      {name}: {{kind: task, capability: test.tasks.run, retry: policy, timeout: short{suffix}}}"


def _projection(*events: object) -> InvocationProjection:
    envelopes = tuple(
        EventEnvelope.from_event(index, event)  # type: ignore[arg-type]
        for index, event in enumerate(events, start=1)
    )
    return fold_events(envelopes)


def _projection_after(events: tuple[object, ...], plan_events: tuple[object, ...]) -> InvocationProjection:
    return _projection(*events, *plan_events)


def _invocation() -> InvocationStarted:
    return InvocationStarted(invocation_id="inv-1", lock_digest="a" * 64, entrypoint="main")


def _root() -> GraphStarted:
    return GraphStarted(graph_instance_id="root", graph_id="root")


def _task_activation_events(
    compiled: CompiledWorkflow,
    *,
    node: str = "work",
) -> tuple[object, ...]:
    token = _canonical_start_token(compiled)
    activation = activation_id("root", node, 0, (token.token_id,))
    return (
        token,
        TokenConsumed(token_id=token.token_id, graph_instance_id="root", node_id=node),
        NodeActivated(
            activation_id=activation,
            graph_instance_id="root",
            node_id=node,
            token_ids=(token.token_id,),
        ),
    )


def _task_lease(activation: str, *, attempt: int = 1) -> TaskLeaseAcquired:
    return TaskLeaseAcquired(
        task_id=task_id(activation),
        activation_id=activation,
        attempt=attempt,
        owner_id=f"worker-{attempt}",
        acquired_at=1.0,
        heartbeat_at=1.0,
        expires_at=2.0,
    )


def _completed_start_gate_events(compiled: CompiledWorkflow, node: str) -> tuple[object, ...]:
    token = _canonical_start_token(compiled)
    activation = activation_id("root", node, 0, (token.token_id,))
    expression = compiled.graphs["root"].nodes[node].definition.expression
    assert expression in {"true", "false"}
    return (
        token,
        TokenConsumed(token_id=token.token_id, graph_instance_id="root", node_id=node),
        NodeActivated(
            activation_id=activation,
            graph_instance_id="root",
            node_id=node,
            token_ids=(token.token_id,),
        ),
        NodeCompleted(activation_id=activation, output={"value": expression == "true"}),
    )


def _canonical_start_token(compiled: CompiledWorkflow) -> TokenOffered:
    graph = compiled.graphs["root"]
    return TokenOffered(
        token_id=canonical_digest(
            {
                "graph_instance_id": "root",
                "kind": "graph_start",
                "target": graph.start,
            }
        ),
        graph_instance_id="root",
        source=None,
        target=graph.start,
        payload=None,
    )


def test_start_token_plans_the_first_task_with_canonical_input() -> None:
    compiled = _compiled(
        f"{_task_node('seed', input_='{mode: strict}')}\n      done: {{kind: end}}",
        "      - {from: seed, to: done}",
        start="seed",
    )
    projection = _projection(_invocation())

    plan = plan_next(compiled, projection)

    assert [task.node_id for task in plan.tasks] == ["seed"]
    assert plan.tasks[0].model_dump(mode="json")["input"] == {
        "config": {"mode": "strict"},
        "tokens": [None],
    }
    assert plan.tasks[0].task_id == task_id(plan.tasks[0].activation_id)
    assert [event.kind for event in plan.events] == [
        "graph_started",
        "token_offered",
        "token_consumed",
        "node_activated",
    ]
    assert plan.terminal is None


def test_planning_is_deterministic_and_does_not_mutate_projection() -> None:
    compiled = _compiled(
        f"{_task_node('seed')}\n      done: {{kind: end}}",
        "      - {from: seed, to: done}",
        start="seed",
    )
    projection = _projection(_invocation())
    before = projection.model_dump_json()

    first = plan_next(compiled, projection)
    second = plan_next(compiled, projection)

    assert first == second
    assert projection.model_dump_json() == before


def test_materialized_graph_requires_its_canonical_start_token() -> None:
    compiled = _compiled(
        f"{_task_node('seed')}\n      done: {{kind: end}}",
        "      - {from: seed, to: done}",
        start="seed",
    )
    projection = _projection(
        _invocation(),
        _root(),
        TokenOffered(
            token_id="unrelated",
            graph_instance_id="root",
            source="seed",
            target="done",
            payload="already-present",
        ),
    )

    with pytest.raises(PlanningError, match="canonical start token"):
        plan_next(compiled, projection)


def test_each_started_graph_instance_gets_its_own_start_token() -> None:
    compiled = compile_workflow(
        parse_workflow(
            """
name: planner-test
entrypoints: {main: root}
retry: {}
timeout: {}
graphs:
  root:
    max_activations: 3
    start: child_call
    nodes:
      child_call: {kind: subgraph, graph: child}
      root_done: {kind: end}
    edges:
      - {from: child_call, to: root_done}
  child:
    max_activations: 2
    start: child_done
    nodes:
      child_done: {kind: end}
    edges: []
"""
        ),
        CapabilityRegistry.empty(),
    )
    events = (_invocation(),)

    plan = plan_next(compiled, _projection(*events))

    start_tokens = [
        (event.graph_instance_id, event.target)
        for event in plan.events
        if event.kind == "token_offered" and event.source is None
    ]
    assert [target for _instance, target in start_tokens] == ["child_call", "child_done"]
    completed = [event.graph_instance_id for event in plan.events if event.kind == "graph_completed"]
    assert completed[-1] == "root"
    assert len(completed) == 2
    assert _projection_after(events, plan.events).status == "succeeded"


def test_nested_graphs_settle_deepest_first_independent_of_instance_id_order() -> None:
    compiled = compile_workflow(
        parse_workflow(
            """
name: planner-test
entrypoints: {main: root}
retry: {}
timeout: {}
graphs:
  root:
    max_activations: 3
    start: child_call
    nodes:
      child_call: {kind: subgraph, graph: child}
      root_done: {kind: end}
    edges:
      - {from: child_call, to: root_done}
  child:
    max_activations: 3
    start: grandchild_call
    nodes:
      grandchild_call: {kind: subgraph, graph: grandchild}
      child_done: {kind: end}
    edges:
      - {from: grandchild_call, to: child_done}
  grandchild:
    max_activations: 2
    start: grandchild_done
    nodes:
      grandchild_done: {kind: end}
    edges: []
"""
        ),
        CapabilityRegistry.empty(),
    )
    events = (_invocation(),)

    plan = plan_next(compiled, _projection(*events))

    completed = [event.graph_instance_id for event in plan.events if event.kind == "graph_completed"]
    assert completed[-1] == "root"
    assert len(completed) == 3
    assert plan.terminal == "succeeded"
    assert _projection_after(events, plan.events).status == "succeeded"


@pytest.mark.parametrize("case", ["foreign_id", "wrong_payload", "duplicate"])
def test_source_less_start_token_must_be_unique_and_canonical(case: str) -> None:
    compiled = _compiled(
        f"{_task_node('seed')}\n      done: {{kind: end}}",
        "      - {from: seed, to: done}",
        start="seed",
    )
    canonical = _canonical_start_token(compiled)
    if case == "foreign_id":
        tokens = (canonical.model_copy(update={"token_id": "foreign"}),)
    elif case == "wrong_payload":
        tokens = (canonical.model_copy(update={"payload": {"wrong": True}}),)
    else:
        tokens = (
            canonical,
            canonical.model_copy(update={"token_id": "duplicate"}),
        )

    with pytest.raises(PlanningError, match="canonical start token"):
        plan_next(compiled, _projection(_invocation(), _root(), *tokens))


def test_canonical_start_token_id_rejects_non_start_provenance() -> None:
    compiled = _compiled(
        f"{_task_node('seed')}\n      done: {{kind: end}}",
        "      - {from: seed, to: done}",
        start="seed",
    )
    canonical = _canonical_start_token(compiled)
    collision = canonical.model_copy(update={"source": "done"})

    with pytest.raises(PlanningError, match="canonical start token"):
        plan_next(compiled, _projection(_invocation(), _root(), collision))


@pytest.mark.parametrize(
    ("node_id", "sources"),
    [
        ("work", ()),
        ("work", ("left", "right")),
        ("work", ("start",)),
        ("any_join", ()),
        ("any_join", ("left", "right")),
        ("any_join", ("start",)),
        ("all_join", ("right", "left")),
        ("all_join", ("left", "left")),
        ("all_join", ("left",)),
        ("all_join", ("left", "start")),
        ("all_join", ("left", "right", "left")),
    ],
)
def test_existing_activation_must_match_compiled_consumption_contract(
    node_id: str, sources: tuple[str, ...]
) -> None:
    compiled = _compiled(
        f"""      start: {{kind: gate, expression: 'false'}}
      left: {{kind: gate, expression: 'false'}}
      right: {{kind: gate, expression: 'false'}}
{_task_node("work")}
      any_join: {{kind: join, join: any}}
      all_join: {{kind: join, join: all}}
      done: {{kind: end}}""",
        """      - {from: start, to: left, condition: 'false'}
      - {from: start, to: right, condition: 'false'}
      - {from: left, to: work, condition: 'false'}
      - {from: right, to: work, condition: 'false'}
      - {from: left, to: any_join, condition: 'false'}
      - {from: right, to: any_join, condition: 'false'}
      - {from: left, to: all_join, condition: 'false'}
      - {from: right, to: all_join, condition: 'false'}
      - {from: work, to: done}
      - {from: any_join, to: done}
      - {from: all_join, to: done}""",
        start="start",
    )
    token_ids = tuple(f"input-{index}" for index in range(len(sources)))
    activation = activation_id("root", node_id, 0, token_ids)
    input_events: list[object] = []
    for token_id_, source in zip(token_ids, sources, strict=True):
        input_events.extend(
            (
                TokenOffered(
                    token_id=token_id_,
                    graph_instance_id="root",
                    source=source,
                    target=node_id,
                    payload=source,
                ),
                TokenConsumed(token_id=token_id_, graph_instance_id="root", node_id=node_id),
            )
        )
    projection = _projection(
        _invocation(),
        _root(),
        *_completed_start_gate_events(compiled, "start"),
        *input_events,
        NodeActivated(
            activation_id=activation,
            graph_instance_id="root",
            node_id=node_id,
            token_ids=token_ids,
        ),
    )

    with pytest.raises(PlanningError, match="consumption contract"):
        plan_next(compiled, projection)


def test_existing_end_activation_requires_canonical_token_id_order() -> None:
    compiled = _compiled(
        """      source: {kind: gate, expression: 'false'}
      done: {kind: end}""",
        "      - {from: source, to: done, condition: 'false'}",
        start="source",
    )
    tokens = (
        TokenOffered(
            token_id="z-token",
            graph_instance_id="root",
            source="source",
            target="done",
            payload="Z",
        ),
        TokenOffered(
            token_id="a-token",
            graph_instance_id="root",
            source="source",
            target="done",
            payload="A",
        ),
    )
    token_ids = tuple(token.token_id for token in tokens)
    activation = activation_id("root", "done", 0, token_ids)
    projection = _projection(
        _invocation(),
        _root(),
        *_completed_start_gate_events(compiled, "source"),
        *tokens,
        *(
            TokenConsumed(token_id=token.token_id, graph_instance_id="root", node_id="done")
            for token in tokens
        ),
        NodeActivated(
            activation_id=activation,
            graph_instance_id="root",
            node_id="done",
            token_ids=token_ids,
        ),
    )

    with pytest.raises(PlanningError, match="consumption contract"):
        plan_next(compiled, projection)


@pytest.mark.parametrize(
    ("unsupported_node", "extra_graphs"),
    [
        (
            "{kind: subgraph, graph: child}",
            """  child:
    max_activations: 1
    start: done
    nodes:
      done: {kind: end}
    edges: []""",
        ),
        ("{kind: interrupt, reason: review, actions: [continue]}", ""),
    ],
)
def test_completed_unsupported_activation_is_rejected_fail_closed(
    unsupported_node: str,
    extra_graphs: str,
) -> None:
    compiled = _compiled(
        f"""      start: {{kind: gate, expression: 'false'}}
      blocked: {unsupported_node}
      done: {{kind: end}}""",
        """      - {from: start, to: blocked, condition: 'false'}
      - {from: blocked, to: done}""",
        start="start",
        extra_graphs=extra_graphs,
    )
    token = TokenOffered(
        token_id="blocked-input",
        graph_instance_id="root",
        source="start",
        target="blocked",
        payload=None,
    )
    activation = activation_id("root", "blocked", 0, (token.token_id,))
    projection = _projection(
        _invocation(),
        _root(),
        *_completed_start_gate_events(compiled, "start"),
        token,
        TokenConsumed(token_id=token.token_id, graph_instance_id="root", node_id="blocked"),
        NodeActivated(
            activation_id=activation,
            graph_instance_id="root",
            node_id="blocked",
            token_ids=(token.token_id,),
        ),
        NodeCompleted(activation_id=activation, output=None),
    )

    with pytest.raises(PlanningError, match="does not support node kind"):
        plan_next(compiled, projection)


def test_multiple_tokens_plan_in_token_id_order_with_distinct_generations() -> None:
    compiled = _compiled(
        f"      source: {{kind: gate, expression: 'false'}}\n{_task_node('work')}\n      done: {{kind: end}}",
        """      - {from: source, to: work, condition: 'false'}
      - {from: work, to: done}""",
        start="source",
    )
    projection = _projection(
        _invocation(),
        _root(),
        *_completed_start_gate_events(compiled, "source"),
        TokenOffered(token_id="tok-z", graph_instance_id="root", source="source", target="work", payload=2),
        TokenOffered(token_id="tok-a", graph_instance_id="root", source="source", target="work", payload=1),
    )

    plan = plan_next(compiled, projection)

    assert [task.model_dump(mode="json")["input"] for task in plan.tasks] == [
        {"config": {}, "tokens": [1]},
        {"config": {}, "tokens": [2]},
    ]
    assert [task.activation_id for task in plan.tasks] == [
        activation_id("root", "work", 0, ("tok-a",)),
        activation_id("root", "work", 1, ("tok-z",)),
    ]


def test_all_join_waits_for_each_distinct_predecessor() -> None:
    compiled = _compiled(
        """      left: {kind: gate, expression: 'true'}
      right: {kind: gate, expression: 'true'}
      joined: {kind: join, join: all}
      done: {kind: end}""",
        """      - {from: left, to: right, condition: 'false'}
      - {from: left, to: joined, condition: 'false'}
      - {from: right, to: joined}
      - {from: joined, to: done}""",
        start="left",
    )
    projection = _projection(
        _invocation(),
        _root(),
        *_completed_start_gate_events(compiled, "left"),
        TokenOffered(
            token_id="left-1", graph_instance_id="root", source="left", target="joined", payload="L"
        ),
        TokenOffered(
            token_id="left-2", graph_instance_id="root", source="left", target="joined", payload="duplicate"
        ),
    )

    plan = plan_next(compiled, projection)

    assert not any(event.kind == "node_activated" for event in plan.events)
    assert plan.tasks == ()


def test_all_join_consumes_one_token_per_predecessor_in_compiled_order() -> None:
    compiled = _compiled(
        """      left: {kind: gate, expression: 'true'}
      right: {kind: gate, expression: 'true'}
      joined: {kind: join, join: all}
      done: {kind: end}""",
        """      - {from: left, to: right, condition: 'false'}
      - {from: right, to: joined}
      - {from: left, to: joined, condition: 'false'}
      - {from: joined, to: done}""",
        start="left",
    )
    projection = _projection(
        _invocation(),
        _root(),
        *_completed_start_gate_events(compiled, "left"),
        TokenOffered(
            token_id="z-right", graph_instance_id="root", source="right", target="joined", payload="R"
        ),
        TokenOffered(
            token_id="a-left", graph_instance_id="root", source="left", target="joined", payload="L"
        ),
        TokenOffered(
            token_id="b-left", graph_instance_id="root", source="left", target="joined", payload="unused"
        ),
    )

    plan = plan_next(compiled, projection)
    activated = next(event for event in plan.events if event.kind == "node_activated")
    completed = next(event for event in plan.events if event.kind == "node_completed")

    assert activated.token_ids == ("z-right", "a-left")
    assert completed.model_dump(mode="json")["output"] == {"tokens": ["R", "L"]}
    assert not any(event.kind == "token_consumed" and event.token_id == "b-left" for event in plan.events)


def test_any_join_consumes_only_the_earliest_token() -> None:
    compiled = _compiled(
        """      first: {kind: gate, expression: 'true'}
      second: {kind: gate, expression: 'true'}
      joined: {kind: join, join: any}
      done: {kind: end}""",
        """      - {from: first, to: second, condition: 'false'}
      - {from: first, to: joined, condition: 'false'}
      - {from: second, to: joined}
      - {from: joined, to: done}""",
        start="first",
    )
    projection = _projection(
        _invocation(),
        _root(),
        *_completed_start_gate_events(compiled, "first"),
        TokenOffered(
            token_id="tok-z", graph_instance_id="root", source="first", target="joined", payload="Z"
        ),
        TokenOffered(
            token_id="tok-a", graph_instance_id="root", source="second", target="joined", payload="A"
        ),
    )

    plan = plan_next(compiled, projection)
    activated = [event for event in plan.events if event.kind == "node_activated"]

    assert activated[0].token_ids == ("tok-a",)
    assert activated[1].token_ids == ("tok-z",)


def test_start_any_join_batch_can_be_appended_folded_and_replayed() -> None:
    compiled = _compiled(
        """      joined: {kind: join, join: any}
      done: {kind: end}""",
        "      - {from: joined, to: done, condition: 'false'}",
        start="joined",
    )
    starting_events = (_invocation(),)

    first = plan_next(compiled, _projection(*starting_events))
    appended = _projection_after(starting_events, first.events)
    replay = plan_next(compiled, appended)

    assert first.terminal is None
    assert appended.status == "running"
    assert replay == plan_next(compiled, appended)
    assert replay.events == ()
    assert replay.tasks == ()
    assert replay.terminal is None


def test_conditional_edges_use_activation_input_and_completion_output() -> None:
    compiled = _compiled(
        """      choose: {kind: gate, expression: 'tokens == tokens'}
      yes_node: {kind: end}
      no_node: {kind: end}""",
        """      - {from: choose, to: yes_node, condition: 'output.value == true and input.tokens == input.tokens'}
      - {from: choose, to: no_node, condition: 'output.value == false'}""",
        start="choose",
    )

    plan = plan_next(compiled, _projection(_invocation()))

    offered_targets = [event.target for event in plan.events if event.kind == "token_offered"]
    assert offered_targets == ["choose", "yes_node"]
    assert plan.terminal == "succeeded"


def test_false_gate_with_no_matching_edge_emits_no_outgoing_token() -> None:
    compiled = _compiled(
        """      choose: {kind: gate, expression: 'false'}
      done: {kind: end}""",
        "      - {from: choose, to: done, condition: 'output.value == true'}",
        start="choose",
    )

    plan = plan_next(compiled, _projection(_invocation()))

    assert [event.target for event in plan.events if event.kind == "token_offered"] == ["choose"]
    assert plan.terminal is None


def test_cycle_is_bounded_before_creating_more_work() -> None:
    compiled = _compiled(
        "      loop: {kind: gate, expression: 'true'}",
        "      - {from: loop, to: loop}",
        start="loop",
        maximum=1,
    )

    plan = plan_next(compiled, _projection(_invocation()))

    assert plan.tasks == ()
    assert plan.terminal == "failed"
    assert plan.reason == "max_activations_exceeded:root"
    assert [event.kind for event in plan.events[-2:]] == ["graph_failed", "invocation_finished"]
    assert _projection_after((_invocation(),), plan.events).status == "failed"


def test_deep_acyclic_child_failure_propagates_without_python_recursion() -> None:
    depth = sys.getrecursionlimit() + 50
    graph_ids = [f"graph-{index:04d}" for index in range(depth)]
    graphs: dict[str, object] = {}
    for graph_id, child_id in zip(graph_ids[:-1], graph_ids[1:], strict=True):
        graphs[graph_id] = {
            "max_activations": 1,
            "start": "call",
            "nodes": {"call": {"kind": "subgraph", "graph": child_id}},
            "edges": [],
        }
    graphs[graph_ids[-1]] = {
        "max_activations": 1,
        "start": "work",
        "nodes": {
            "work": {
                "kind": "task",
                "capability": "test.tasks.run",
                "retry": "policy",
                "timeout": "short",
            }
        },
        "edges": [],
    }
    workflow = {
        "name": "deep-failure",
        "entrypoints": {"main": graph_ids[0]},
        "retry": {"policy": {"max_attempts": 1, "retry_on": []}},
        "timeout": {"short": {"run_seconds": 5}},
        "graphs": graphs,
    }
    compiled = compile_workflow(
        parse_workflow(yaml.safe_dump(workflow, sort_keys=False)),
        _registry(),
    )
    first = plan_next(compiled, _projection(_invocation()))
    assert len(first.tasks) == 1
    task = first.tasks[0]
    failure = TaskFailure(kind="internal", message="deep child failed")
    predecessor = _projection(
        _invocation(),
        *first.events,
        TaskAttemptStarted(
            activation_id=task.activation_id,
            attempt=1,
            lease_expires_at="2030-01-01T00:00:00Z",
        ),
        _task_lease(task.activation_id),
        TaskAttemptFailed(
            activation_id=task.activation_id,
            attempt=1,
            failure=failure,
        ),
    )

    terminal = plan_next(compiled, predecessor)

    assert terminal.terminal == "failed"
    assert terminal.reason == "task_failed:work:internal"
    assert terminal.events[-1].kind == "invocation_finished"


def test_event_history_accepts_parallel_successes_before_deterministic_all_join() -> None:
    compiled = _compiled(
        f"""      split: {{kind: gate, expression: 'true'}}
{_task_node("left")}
{_task_node("right")}
      joined: {{kind: join, join: all}}
      done: {{kind: end}}""",
        """      - {from: split, to: left}
      - {from: split, to: right}
      - {from: left, to: joined}
      - {from: right, to: joined}
      - {from: joined, to: done}""",
        start="split",
        maximum=5,
    )
    events: list[object] = [_invocation(), _root(), _canonical_start_token(compiled)]
    initial = plan_next(compiled, _projection(*events))
    assert len(initial.tasks) == 2
    events.extend(initial.events)
    for task in initial.tasks:
        events.extend(
            (
                TaskAttemptStarted(
                    activation_id=task.activation_id,
                    attempt=1,
                    lease_expires_at="2",
                ),
                _task_lease(task.activation_id),
            )
        )
    previous_tree_id = "0" * 64
    for index, task in enumerate(initial.tasks, start=1):
        tree_id = str(index) * 64
        events.extend(
            (
                TaskAttemptSucceeded(
                    activation_id=task.activation_id,
                    attempt=1,
                    output={task.node_id: True},
                ),
                HeadAdvanced(
                    task_id=task.task_id,
                    activation_id=task.activation_id,
                    attempt=1,
                    previous_tree_id=previous_tree_id,
                    tree_id=tree_id,
                ),
            )
        )
        previous_tree_id = tree_id
    settled = plan_next(compiled, _projection(*events))
    assert settled.terminal == "succeeded"
    events.extend(settled.events)
    envelopes = tuple(
        EventEnvelope.from_event(index, event)  # type: ignore[arg-type]
        for index, event in enumerate(events, start=1)
    )
    projection = fold_events(envelopes)

    validate_event_history(compiled, envelopes, projection)

    joined = next(activation for activation in projection.activations if activation.node_id == "joined")
    assert len(joined.token_ids) == 2


@pytest.mark.parametrize(
    ("transition", "match"),
    [
        ("start", "partial task-start publication"),
        ("success", "partial task-success publication"),
    ],
)
def test_event_history_rejects_partial_external_atomic_transition(
    transition: str,
    match: str,
) -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
    )
    bootstrap = (_invocation(), _root(), _canonical_start_token(compiled))
    planned = plan_next(compiled, _projection(*bootstrap))
    assert len(planned.tasks) == 1
    task = planned.tasks[0]
    started = TaskAttemptStarted(
        activation_id=task.activation_id,
        attempt=task.attempt,
        lease_expires_at="2",
    )
    events: tuple[object, ...] = (*bootstrap, *planned.events, started)
    if transition == "success":
        events = (
            *events,
            _task_lease(task.activation_id),
            TaskAttemptSucceeded(
                activation_id=task.activation_id,
                attempt=task.attempt,
                output={"ok": True},
            ),
        )
    envelopes = tuple(
        EventEnvelope.from_event(index, event)  # type: ignore[arg-type]
        for index, event in enumerate(events, start=1)
    )
    projection = fold_events(envelopes)

    with pytest.raises(PlanningError, match=match):
        validate_event_history(compiled, envelopes, projection)


def test_stopped_task_terminates_without_outgoing_token() -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
    )
    start_token = _canonical_start_token(compiled)
    activation = activation_id("root", "work", 0, (start_token.token_id,))
    events = (
        _invocation(),
        _root(),
        *_task_activation_events(compiled),
        TaskAttemptStarted(activation_id=activation, attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
        _task_lease(activation),
        TaskAttemptStopped(activation_id=activation, attempt=1, reason="operator_stop"),
    )
    projection = _projection(*events)

    plan = plan_next(compiled, projection)

    assert plan.terminal == "stopped"
    assert plan.reason == "operator_stop"
    assert [event.kind for event in plan.events] == ["invocation_finished"]
    assert _projection_after(events, plan.events).terminal_reason == "operator_stop"


@pytest.mark.parametrize(
    ("payloads", "expected"),
    [
        (({"one": 1},), {"one": 1}),
        (({"one": 1}, {"two": 2}), {"tokens": [{"one": 1}, {"two": 2}]}),
    ],
)
def test_end_node_completes_root_with_consumed_payloads(
    payloads: tuple[object, ...], expected: object
) -> None:
    compiled = _compiled(
        """      source: {kind: gate, expression: 'false'}
      done: {kind: end}""",
        "      - {from: source, to: done, condition: 'false'}",
        start="source",
    )
    token_events = tuple(
        TokenOffered(
            token_id=f"tok-{index}",
            graph_instance_id="root",
            source="source",
            target="done",
            payload=payload,
        )
        for index, payload in enumerate(payloads)
    )
    projection = _projection(
        _invocation(),
        _root(),
        *_completed_start_gate_events(compiled, "source"),
        *token_events,
    )

    plan = plan_next(compiled, projection)

    completed = next(event for event in plan.events if event.kind == "graph_completed")
    assert completed.model_dump(mode="json")["output"] == expected
    assert plan.terminal == "succeeded"


def test_allowed_retry_preserves_ids_and_includes_prior_failure() -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
        retry_on="[transient, timeout]",
        max_attempts=3,
    )
    start_token = _canonical_start_token(compiled)
    activation = activation_id("root", "work", 0, (start_token.token_id,))
    failure = TaskFailure(kind="timeout", message="lease expired")
    events = (
        _invocation(),
        _root(),
        *_task_activation_events(compiled),
        TaskAttemptStarted(activation_id=activation, attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
        _task_lease(activation),
        TaskAttemptFailed(activation_id=activation, attempt=1, failure=failure),
    )
    projection = _projection(*events)

    plan = plan_next(compiled, projection)

    assert len(plan.tasks) == 1
    assert plan.tasks[0].activation_id == activation
    assert plan.tasks[0].task_id == task_id(activation)
    assert plan.tasks[0].attempt == 2
    assert plan.tasks[0].prior_failure == failure
    assert plan.events == ()


@pytest.mark.parametrize(
    ("retry_on", "max_attempts", "failure_kind"),
    [("[timeout]", 3, "internal"), ("[internal]", 1, "internal")],
)
def test_disallowed_or_exhausted_failure_fails_node_graph_and_invocation(
    retry_on: str, max_attempts: int, failure_kind: str
) -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
        retry_on=retry_on,
        max_attempts=max_attempts,
    )
    start_token = _canonical_start_token(compiled)
    activation = activation_id("root", "work", 0, (start_token.token_id,))
    failure = TaskFailure(kind=failure_kind, message="closed failure")  # type: ignore[arg-type]
    events = (
        _invocation(),
        _root(),
        *_task_activation_events(compiled),
        TaskAttemptStarted(activation_id=activation, attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
        _task_lease(activation),
        TaskAttemptFailed(activation_id=activation, attempt=1, failure=failure),
    )
    projection = _projection(*events)

    plan = plan_next(compiled, projection)

    assert plan.tasks == ()
    assert plan.terminal == "failed"
    assert [event.kind for event in plan.events] == [
        "node_failed",
        "graph_failed",
        "invocation_finished",
    ]
    folded = _projection_after(events, plan.events)
    assert folded.status == "failed"
    assert folded.graph_instances[0].status == "failed"
    assert folded.activations[0].status == "failed"
    assert not folded.activations[0].structural_failure


def test_successful_task_completion_is_routed_structurally() -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
    )
    start_token = _canonical_start_token(compiled)
    activation = activation_id("root", "work", 0, (start_token.token_id,))
    identifier = task_id(activation)
    events = (
        _invocation(),
        _root(),
        *_task_activation_events(compiled),
        TaskAttemptStarted(activation_id=activation, attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
        TaskLeaseAcquired(
            task_id=identifier,
            activation_id=activation,
            attempt=1,
            owner_id="worker",
            acquired_at=1.0,
            heartbeat_at=1.0,
            expires_at=2.0,
        ),
        TaskAttemptSucceeded(activation_id=activation, attempt=1, output={"result": 2}),
        HeadAdvanced(
            task_id=identifier,
            activation_id=activation,
            attempt=1,
            previous_tree_id="a" * 64,
            tree_id="b" * 64,
        ),
    )
    projection = _projection(*events)

    plan = plan_next(compiled, projection)

    assert plan.tasks == ()
    assert [event.kind for event in plan.events] == [
        "node_completed",
        "token_offered",
        "token_consumed",
        "node_activated",
        "node_completed",
        "graph_completed",
        "invocation_finished",
    ]
    assert plan.terminal == "succeeded"
    assert _projection_after(events, plan.events).status == "succeeded"


def test_completed_end_is_rechecked_after_later_task_settlement() -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done, condition: 'false'}",
        start="work",
    )
    end_activation = activation_id("root", "done", 0, ("end-token",))
    start_token = _canonical_start_token(compiled)
    work_activation = activation_id("root", "work", 0, (start_token.token_id,))
    identifier = task_id(work_activation)
    assert end_activation < work_activation
    events = (
        _invocation(),
        _root(),
        TokenOffered(
            token_id="end-token",
            graph_instance_id="root",
            source="work",
            target="done",
            payload="final",
        ),
        TokenConsumed(token_id="end-token", graph_instance_id="root", node_id="done"),
        NodeActivated(
            activation_id=end_activation,
            graph_instance_id="root",
            node_id="done",
            token_ids=("end-token",),
        ),
        NodeCompleted(activation_id=end_activation, output="final"),
        start_token,
        TokenConsumed(token_id=start_token.token_id, graph_instance_id="root", node_id="work"),
        NodeActivated(
            activation_id=work_activation,
            graph_instance_id="root",
            node_id="work",
            token_ids=(start_token.token_id,),
        ),
        TaskAttemptStarted(
            activation_id=work_activation,
            attempt=1,
            lease_expires_at="2030-01-01T00:00:00Z",
        ),
        TaskLeaseAcquired(
            task_id=identifier,
            activation_id=work_activation,
            attempt=1,
            owner_id="worker",
            acquired_at=1.0,
            heartbeat_at=1.0,
            expires_at=2.0,
        ),
        TaskAttemptSucceeded(activation_id=work_activation, attempt=1, output="ignored"),
        HeadAdvanced(
            task_id=identifier,
            activation_id=work_activation,
            attempt=1,
            previous_tree_id="a" * 64,
            tree_id="b" * 64,
        ),
    )

    plan = plan_next(compiled, _projection(*events))

    assert plan.terminal == "succeeded"
    assert [event.kind for event in plan.events] == [
        "node_completed",
        "graph_completed",
        "invocation_finished",
    ]
    assert _projection_after(events, plan.events).status == "succeeded"


def test_projection_order_does_not_change_task_order() -> None:
    compiled = _compiled(
        f"      source: {{kind: gate, expression: 'false'}}\n{_task_node('work')}\n      done: {{kind: end}}",
        """      - {from: source, to: work, condition: 'false'}
      - {from: work, to: done}""",
        start="source",
    )
    tokens = (
        TokenOffered(token_id="tok-b", graph_instance_id="root", source="source", target="work", payload="B"),
        TokenOffered(token_id="tok-a", graph_instance_id="root", source="source", target="work", payload="A"),
    )
    start_events = _completed_start_gate_events(compiled, "source")
    first = plan_next(compiled, _projection(_invocation(), _root(), *start_events, *tokens))
    second = plan_next(
        compiled,
        _projection(_invocation(), _root(), *start_events, *reversed(tokens)),
    )

    assert first == second


def test_illegal_projection_node_is_rejected_fail_closed() -> None:
    compiled = _compiled("      done: {kind: end}", "      []", start="done")
    base = _projection(_invocation(), _root(), _canonical_start_token(compiled))
    projection = InvocationProjection(
        status="running",
        invocation_id="inv-1",
        lock_digest="a" * 64,
        entrypoint="main",
        graph_instances=base.graph_instances,
        offered_tokens=base.offered_tokens,
        activations=(
            ActivationRecord(activation_id="bad", graph_instance_id="root", node_id="missing", token_ids=()),
        ),
    )
    # model_construct deliberately represents an input that bypassed normal ledger folding.
    projection = InvocationProjection.model_construct(**projection.__dict__)

    with pytest.raises(PlanningError, match="unknown node"):
        plan_next(compiled, projection)


def test_active_attempt_is_not_planned_twice() -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
    )
    start_token = _canonical_start_token(compiled)
    activation = activation_id("root", "work", 0, (start_token.token_id,))
    projection = _projection(
        _invocation(),
        _root(),
        *_task_activation_events(compiled),
        TaskAttemptStarted(activation_id=activation, attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
        _task_lease(activation),
    )

    assert plan_next(compiled, projection).tasks == ()


def test_planned_inputs_are_deeply_frozen() -> None:
    compiled = _compiled(
        f"{_task_node('work', input_='{nested: {items: [one]}}')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
    )
    task = plan_next(compiled, _projection(_invocation())).tasks[0]

    assert isinstance(task.input, Mapping)
    with pytest.raises(TypeError):
        task.input["new"] = True  # type: ignore[index]


_EMPTY_TREE = "0" * 64
_EFFECT_TREE = "b" * 64


def _effect_idempotency_key(effect_id: str, kind: str, payload: dict[str, object]) -> str:
    return canonical_digest(
        {
            "lock_digest": "a" * 64,
            "effect_id": effect_id,
            "kind": kind,
            "payload_digest": canonical_digest(payload),
        }
    )


def _effect_intent(
    activation: str,
    effect_id: str,
    index: int,
    payload: dict[str, object],
) -> EffectIntentCommitted:
    return EffectIntentCommitted(
        effect_id=effect_id,
        activation_id=activation,
        attempt=1,
        index=index,
        effect_kind="toy.audit",
        payload=payload,
        idempotency_key=_effect_idempotency_key(effect_id, "toy.audit", payload),
    )


def test_plan_next_emits_no_downstream_while_effect_pending() -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
    )
    start_token = _canonical_start_token(compiled)
    activation = activation_id("root", "work", 0, (start_token.token_id,))
    identifier = task_id(activation)
    projection = _projection(
        _invocation(),
        _root(),
        *_task_activation_events(compiled),
        TaskAttemptStarted(activation_id=activation, attempt=1, lease_expires_at="2"),
        _task_lease(activation),
        TaskCommitPrepared(
            task_id=identifier,
            activation_id=activation,
            attempt=1,
            output={"ok": True},
            previous_tree_id=_EMPTY_TREE,
            tree_id=_EFFECT_TREE,
            effect_ids=("effect-1", "effect-2"),
        ),
        HeadAdvanced(
            task_id=identifier,
            activation_id=activation,
            attempt=1,
            previous_tree_id=_EMPTY_TREE,
            tree_id=_EFFECT_TREE,
        ),
        _effect_intent(activation, "effect-1", 0, {"n": 1}),
        _effect_intent(activation, "effect-2", 1, {"n": 2}),
    )

    plan = plan_next(compiled, projection)

    assert plan.tasks == ()
    assert plan.events == ()
    assert plan.terminal is None
    assert projection.activations[-1].attempts[-1].status == "effect_pending"


def test_non_retryable_failure_never_creates_planned_task() -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
        retry_on="[transient, timeout, external_effect]",
        max_attempts=3,
    )
    start_token = _canonical_start_token(compiled)
    activation = activation_id("root", "work", 0, (start_token.token_id,))
    failure = TaskFailure(kind="external_effect", message="denied", retryable=False)
    projection = _projection(
        _invocation(),
        _root(),
        *_task_activation_events(compiled),
        TaskAttemptStarted(activation_id=activation, attempt=1, lease_expires_at="2"),
        _task_lease(activation),
        TaskAttemptFailed(activation_id=activation, attempt=1, failure=failure),
    )

    plan = plan_next(compiled, projection)

    assert plan.tasks == ()
    assert plan.terminal == "failed"
    assert [event.kind for event in plan.events] == [
        "node_failed",
        "graph_failed",
        "invocation_finished",
    ]


def test_event_history_accepts_prepared_effect_apply_receipt_and_final_success() -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
    )
    bootstrap = (_invocation(), _root(), _canonical_start_token(compiled))
    planned = plan_next(compiled, _projection(*bootstrap))
    assert len(planned.tasks) == 1
    task = planned.tasks[0]
    events: list[object] = [
        *bootstrap,
        *planned.events,
        TaskAttemptStarted(
            activation_id=task.activation_id,
            attempt=1,
            lease_expires_at="2",
        ),
        _task_lease(task.activation_id),
        TaskCommitPrepared(
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=1,
            output={"ok": True},
            previous_tree_id=_EMPTY_TREE,
            tree_id=_EFFECT_TREE,
            effect_ids=("effect-1", "effect-2"),
        ),
        HeadAdvanced(
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=1,
            previous_tree_id=_EMPTY_TREE,
            tree_id=_EFFECT_TREE,
        ),
        _effect_intent(task.activation_id, "effect-1", 0, {"n": 1}),
        _effect_intent(task.activation_id, "effect-2", 1, {"n": 2}),
        EffectApplyStarted(effect_id="effect-1", apply_attempt=1),
        EffectReceiptRecorded(effect_id="effect-1", apply_attempt=1, receipt={"remote": 1}),
        EffectApplyStarted(effect_id="effect-2", apply_attempt=1),
        EffectReceiptRecorded(effect_id="effect-2", apply_attempt=1, receipt={"remote": 2}),
        TaskAttemptSucceeded(
            activation_id=task.activation_id,
            attempt=1,
            output={"ok": True},
        ),
    ]
    settled = plan_next(compiled, _projection(*events))
    assert settled.terminal == "succeeded"
    events.extend(settled.events)
    envelopes = tuple(
        EventEnvelope.from_event(index, event)  # type: ignore[arg-type]
        for index, event in enumerate(events, start=1)
    )
    projection = fold_events(envelopes)

    validate_event_history(compiled, envelopes, projection)

    assert projection.status == "succeeded"
    assert tuple(effect.status for effect in projection.effects) == ("applied", "applied")


def test_event_history_accepts_heartbeat_while_effect_pending() -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
    )
    bootstrap = (_invocation(), _root(), _canonical_start_token(compiled))
    planned = plan_next(compiled, _projection(*bootstrap))
    task = planned.tasks[0]
    prefix = (
        *bootstrap,
        *planned.events,
        TaskAttemptStarted(
            activation_id=task.activation_id,
            attempt=1,
            lease_expires_at="2",
        ),
        _task_lease(task.activation_id),
        TaskCommitPrepared(
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=1,
            output={"ok": True},
            previous_tree_id=_EMPTY_TREE,
            tree_id=_EFFECT_TREE,
            effect_ids=("effect-1", "effect-2"),
        ),
        HeadAdvanced(
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=1,
            previous_tree_id=_EMPTY_TREE,
            tree_id=_EFFECT_TREE,
        ),
        _effect_intent(task.activation_id, "effect-1", 0, {"n": 1}),
        _effect_intent(task.activation_id, "effect-2", 1, {"n": 2}),
        TaskLeaseHeartbeat(
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=1,
            owner_id="worker-1",
            heartbeat_at=1.5,
            expires_at=3.0,
        ),
    )
    pending = _projection(*prefix)
    attempt = pending.activations[-1].attempts[-1]
    assert attempt.status == "effect_pending"
    assert attempt.lease_heartbeat_at == 1.5
    events: list[object] = [
        *prefix,
        EffectApplyStarted(effect_id="effect-1", apply_attempt=1),
        EffectReceiptRecorded(effect_id="effect-1", apply_attempt=1, receipt={"remote": 1}),
        EffectApplyStarted(effect_id="effect-2", apply_attempt=1),
        EffectReceiptRecorded(effect_id="effect-2", apply_attempt=1, receipt={"remote": 2}),
        TaskAttemptSucceeded(
            activation_id=task.activation_id,
            attempt=1,
            output={"ok": True},
        ),
    ]
    settled = plan_next(compiled, _projection(*events))
    events.extend(settled.events)
    envelopes = tuple(
        EventEnvelope.from_event(index, event)  # type: ignore[arg-type]
        for index, event in enumerate(events, start=1)
    )
    projection = fold_events(envelopes)

    validate_event_history(compiled, envelopes, projection)

    assert projection.status == "succeeded"
    work = next(item for item in projection.activations if item.activation_id == task.activation_id)
    assert work.attempts[-1].lease_heartbeat_at == 1.5


def test_event_history_rejects_partial_prepared_effect_batch() -> None:
    compiled = _compiled(
        f"{_task_node('work')}\n      done: {{kind: end}}",
        "      - {from: work, to: done}",
        start="work",
    )
    bootstrap = (_invocation(), _root(), _canonical_start_token(compiled))
    planned = plan_next(compiled, _projection(*bootstrap))
    task = planned.tasks[0]
    events = (
        *bootstrap,
        *planned.events,
        TaskAttemptStarted(
            activation_id=task.activation_id,
            attempt=1,
            lease_expires_at="2",
        ),
        _task_lease(task.activation_id),
        TaskCommitPrepared(
            task_id=task.task_id,
            activation_id=task.activation_id,
            attempt=1,
            output={"ok": True},
            previous_tree_id=_EMPTY_TREE,
            tree_id=_EFFECT_TREE,
            effect_ids=("effect-1", "effect-2"),
        ),
    )
    envelopes = tuple(
        EventEnvelope.from_event(index, event)  # type: ignore[arg-type]
        for index, event in enumerate(events, start=1)
    )
    projection = fold_events(envelopes)

    with pytest.raises(PlanningError, match="partial prepared"):
        validate_event_history(compiled, envelopes, projection)
