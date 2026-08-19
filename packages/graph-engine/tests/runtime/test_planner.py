from __future__ import annotations

from collections.abc import Mapping

import pytest

from graph_engine.graph.compiler import CompiledWorkflow, compile_workflow
from graph_engine.graph.schema import parse_workflow
from graph_engine.plugin_api import CapabilityRegistry, TaskFailure
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphStarted,
    InvocationStarted,
    NodeActivated,
    NodeCompleted,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptStopped,
    TaskAttemptSucceeded,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.models import ActivationRecord, InvocationProjection, fold_events
from graph_engine.runtime.planner import PlanningError, activation_id, plan_next, task_id


def _compiled(
    nodes: str,
    edges: str,
    *,
    start: str,
    maximum: int = 20,
    retry_on: str = "[]",
    max_attempts: int = 2,
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
"""
    registry = CapabilityRegistry(task_handlers={"test.tasks.run": object()}, commit_validators={})
    return compile_workflow(parse_workflow(text), registry)  # type: ignore[arg-type]


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
    return InvocationStarted(invocation_id="inv-1", product_digest="a" * 64, entrypoint="main")


def _root() -> GraphStarted:
    return GraphStarted(graph_instance_id="root", graph_id="root", input={"request": 1})


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


def _completed_start_gate_events(compiled: CompiledWorkflow, node: str) -> tuple[object, ...]:
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
        NodeCompleted(activation_id=activation, output={"value": False}),
    )


def _canonical_start_token(compiled: CompiledWorkflow) -> TokenOffered:
    plan = plan_next(compiled, _projection(_invocation(), _root()))
    return next(event for event in plan.events if event.kind == "token_offered" and event.source is None)


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


def test_unrelated_token_does_not_suppress_the_graph_start_token() -> None:
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

    plan = plan_next(compiled, projection)

    assert [task.node_id for task in plan.tasks] == ["seed"]
    assert any(
        event.kind == "token_offered" and event.source is None and event.target == "seed"
        for event in plan.events
    )


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
    max_activations: 2
    start: root_done
    nodes:
      root_done: {kind: end}
    edges: []
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
    events = (
        _invocation(),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        GraphStarted(
            graph_instance_id="z-child",
            graph_id="child",
            parent_graph_instance_id="root",
            parent_node_id="root_done",
            input={"child": 1},
        ),
    )

    plan = plan_next(compiled, _projection(*events))

    start_tokens = [
        (event.graph_instance_id, event.target)
        for event in plan.events
        if event.kind == "token_offered" and event.source is None
    ]
    assert start_tokens == [("root", "root_done"), ("z-child", "child_done")]
    assert [event.graph_instance_id for event in plan.events if event.kind == "graph_completed"] == [
        "z-child",
        "root",
    ]
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
    max_activations: 2
    start: root_done
    nodes:
      root_done: {kind: end}
    edges: []
  child:
    max_activations: 2
    start: child_done
    nodes:
      child_done: {kind: end}
    edges: []
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
    events = (
        _invocation(),
        GraphStarted(graph_instance_id="root", graph_id="root"),
        GraphStarted(
            graph_instance_id="z-child",
            graph_id="child",
            parent_graph_instance_id="root",
            parent_node_id="root_done",
        ),
        GraphStarted(
            graph_instance_id="zz-grandchild",
            graph_id="grandchild",
            parent_graph_instance_id="z-child",
            parent_node_id="child_done",
        ),
    )

    plan = plan_next(compiled, _projection(*events))

    assert [event.graph_instance_id for event in plan.events if event.kind == "graph_completed"] == [
        "zz-grandchild",
        "z-child",
        "root",
    ]
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
    "unsupported_node",
    [
        "{kind: subgraph, graph: root}",
        "{kind: interrupt, reason: review, actions: [continue]}",
    ],
)
def test_completed_unsupported_activation_is_rejected_fail_closed(unsupported_node: str) -> None:
    compiled = _compiled(
        f"""      start: {{kind: gate, expression: 'false'}}
      blocked: {unsupported_node}
      done: {{kind: end}}""",
        """      - {from: start, to: blocked, condition: 'false'}
      - {from: blocked, to: done}""",
        start="start",
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


def test_successful_task_completion_is_routed_structurally() -> None:
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
        TaskAttemptSucceeded(activation_id=activation, attempt=1, output={"result": 2}),
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
        TaskAttemptSucceeded(activation_id=work_activation, attempt=1, output="ignored"),
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
    projection = InvocationProjection(
        status="running",
        invocation_id="inv-1",
        product_digest="a" * 64,
        entrypoint="main",
        graph_instances=(_projection(_invocation(), _root()).graph_instances[0],),
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
