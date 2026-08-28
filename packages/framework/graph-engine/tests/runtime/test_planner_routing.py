from __future__ import annotations

from pathlib import Path

import pytest

from bootstrap_fixtures import synthetic_invocation_started

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
    ContributionAuthority,
    ExecutableAuthority,
)
from graph_engine.composition.provenance import StandardLoader
from graph_engine.composition.registries import _build_registries
from graph_engine.graph.compiler import CompiledWorkflow, compile_workflow
from graph_engine.graph.schema import parse_workflow
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    StagedWriteSet,
    TaskFailure,
    TaskWorkspaceIdentity,
)
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphFailed,
    GraphStarted,
    InvocationFinished,
    InvocationStarted,
    NodeCompleted,
    NodeFailed,
    TaskAttemptFailed,
    TaskAttemptStarted,
    TaskAttemptSucceeded,
    TaskCommitPrepared,
    TaskLeaseAcquired,
    TaskPromotionCompleted,
    TokenOffered,
)
from graph_engine.runtime.models import InvocationProjection, ProjectionError, fold_events
from graph_engine.runtime.planner import (
    PlanningError,
    activation_id,
    plan_next,
    plan_running_tasks,
    subgraph_instance_id,
    validate_event_history,
)


class _PlaceholderHandler:
    async def execute(self, _request: object, _context: object) -> object:
        raise AssertionError("routing tests never execute task handlers")


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
    extra_graphs: str = "",
    maximum: int = 20,
) -> CompiledWorkflow:
    text = f"""
name: planner-routing
entrypoints: {{main: root}}
retry:
  policy: {{max_attempts: 1, retry_on: []}}
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


def _projection(*events: object) -> InvocationProjection:
    envelopes = tuple(
        EventEnvelope.from_event(index, event)  # type: ignore[arg-type]
        for index, event in enumerate(events, start=1)
    )
    return fold_events(envelopes)


def _envelopes(*events: object) -> tuple[EventEnvelope, ...]:
    return tuple(
        EventEnvelope.from_event(index, event)  # type: ignore[arg-type]
        for index, event in enumerate(events, start=1)
    )


def _invocation() -> InvocationStarted:
    return synthetic_invocation_started()


def _root() -> GraphStarted:
    return GraphStarted(graph_instance_id="root", graph_id="root")


def _canonical_start_token(compiled: CompiledWorkflow, graph_id: str = "root") -> TokenOffered:
    graph = compiled.graphs[graph_id]
    return TokenOffered(
        token_id=canonical_digest(
            {
                "graph_instance_id": graph_id,
                "kind": "graph_start",
                "target": graph.start,
            }
        ),
        graph_instance_id=graph_id,
        source=None,
        target=graph.start,
        payload=None,
    )


def _edge_token_id(
    graph_instance_id: str,
    source_activation_id: str,
    edge_index: int,
    source: str,
    target: str,
) -> str:
    return canonical_digest(
        {
            "edge_index": edge_index,
            "graph_instance_id": graph_instance_id,
            "kind": "edge",
            "source": source,
            "source_activation_id": source_activation_id,
            "target": target,
        }
    )


def _route_key(to: str, condition: str | None, otherwise: bool = False) -> tuple[str, str, bool]:
    return (to, condition or "", otherwise)


def _canonical_rank(edges: list[dict[str, object]], target: dict[str, object]) -> int:
    keys = sorted(
        _route_key(str(edge["to"]), edge.get("condition"), bool(edge.get("otherwise", False)))
        for edge in edges
    )
    key = _route_key(str(target["to"]), target.get("condition"), bool(target.get("otherwise", False)))
    return keys.index(key)


def _format_route_keys(edges: list[dict[str, object]]) -> str:
    keys = sorted(
        _route_key(str(edge["to"]), edge.get("condition"), bool(edge.get("otherwise", False)))
        for edge in edges
    )
    return ";".join(f"{to}|{condition}|{str(otherwise).lower()}" for to, condition, otherwise in keys)


def _offered_edge_targets(plan: object) -> list[str]:
    return [event.target for event in plan.events if event.kind == "token_offered"]


def _source_completed_ids(events: tuple[object, ...], node_id: str) -> list[str]:
    activated = {
        event.activation_id: event.node_id
        for event in events
        if getattr(event, "kind", None) == "node_activated"
    }
    return [
        event.activation_id
        for event in events
        if isinstance(event, NodeCompleted) and activated.get(event.activation_id) == node_id
    ]


def _assert_graph_level_route_failure(
    plan: object,
    *,
    source_node: str,
    reason: str,
    history: tuple[object, ...] = (),
) -> None:
    combined = (*history, *plan.events)
    completed = _source_completed_ids(combined, source_node)
    assert len(completed) == 1
    source_activation = completed[0]
    after_completed = False
    later_source_failed = False
    for event in combined:
        if isinstance(event, NodeCompleted) and event.activation_id == source_activation:
            after_completed = True
            continue
        if after_completed and isinstance(event, NodeFailed) and event.activation_id == source_activation:
            later_source_failed = True
    assert later_source_failed is False
    assert not any(isinstance(event, TokenOffered) and event.source == source_node for event in plan.events)
    kinds = [event.kind for event in plan.events]
    failed_at = kinds.index("graph_failed")
    assert kinds[failed_at] == "graph_failed"
    assert kinds[-1] == "invocation_finished"
    assert all(kind in {"graph_failed", "node_failed", "invocation_finished"} for kind in kinds[failed_at:])
    assert not any(
        isinstance(event, NodeFailed) and event.activation_id == source_activation
        for event in plan.events[failed_at:]
    )
    assert plan.terminal == "failed"
    assert plan.reason == reason
    finished = next(event for event in plan.events if isinstance(event, InvocationFinished))
    assert finished.status == "failed"
    assert finished.terminal_reason == reason
    assert next(event for event in plan.events if isinstance(event, GraphFailed)).reason == reason


_EXCLUSIVE_ONE_MATCH_EDGES = [
    {"from": "choose", "to": "zebra", "condition": "output.value == true"},
    {"from": "choose", "to": "alpha", "condition": 'output.value == "other"'},
    {"from": "choose", "to": "fallback", "otherwise": True},
]
_EXCLUSIVE_ONE_MATCH_REORDERED = [
    {"from": "choose", "to": "fallback", "otherwise": True},
    {"from": "choose", "to": "alpha", "condition": 'output.value == "other"'},
    {"from": "choose", "to": "zebra", "condition": "output.value == true"},
]


def _exclusive_nodes(*, expression: str = "true") -> str:
    return f"""      choose:
        kind: gate
        expression: '{expression}'
        routing: {{mode: exclusive}}
      zebra: {{kind: end}}
      alpha: {{kind: end}}
      fallback: {{kind: end}}"""


def _edges_yaml(edges: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for edge in edges:
        items = [f"from: {edge['from']}", f"to: {edge['to']}"]
        if edge.get("condition") is not None:
            items.append(f"condition: '{edge['condition']}'")
        if edge.get("otherwise"):
            items.append("otherwise: true")
        lines.append("      - {" + ", ".join(items) + "}")
    return "\n".join(lines)


def _exclusive_workflow(edges: list[dict[str, object]], *, expression: str = "true") -> CompiledWorkflow:
    return _compiled(_exclusive_nodes(expression=expression), _edges_yaml(edges), start="choose")


def _choose_activation(compiled: CompiledWorkflow) -> str:
    start = _canonical_start_token(compiled)
    return activation_id("root", "choose", 0, (start.token_id,))


def _expected_edge_token(
    compiled: CompiledWorkflow,
    edges: list[dict[str, object]],
    selected: dict[str, object],
    *,
    payload: object,
) -> TokenOffered:
    activation = _choose_activation(compiled)
    return TokenOffered(
        token_id=_edge_token_id(
            "root",
            activation,
            _canonical_rank(edges, selected),
            "choose",
            str(selected["to"]),
        ),
        graph_instance_id="root",
        source="choose",
        target=str(selected["to"]),
        payload=payload,
    )


def test_exclusive_one_match_emits_only_the_matching_branch() -> None:
    compiled = _exclusive_workflow(_EXCLUSIVE_ONE_MATCH_EDGES)
    plan = plan_next(compiled, _projection(_invocation()))

    selected = _EXCLUSIVE_ONE_MATCH_EDGES[0]
    expected = _expected_edge_token(compiled, _EXCLUSIVE_ONE_MATCH_EDGES, selected, payload={"value": True})
    offered = [event for event in plan.events if event.kind == "token_offered"]
    assert [event.target for event in offered] == ["choose", "zebra"]
    assert offered[1].model_dump(mode="json") == expected.model_dump(mode="json")
    assert plan.terminal == "succeeded"


def test_exclusive_all_false_selects_otherwise() -> None:
    compiled = _exclusive_workflow(_EXCLUSIVE_ONE_MATCH_EDGES, expression="false")
    plan = plan_next(compiled, _projection(_invocation()))

    selected = _EXCLUSIVE_ONE_MATCH_EDGES[2]
    expected = _expected_edge_token(compiled, _EXCLUSIVE_ONE_MATCH_EDGES, selected, payload={"value": False})
    offered = [event for event in plan.events if event.kind == "token_offered"]
    assert [event.target for event in offered] == ["choose", "fallback"]
    assert offered[1].model_dump(mode="json") == expected.model_dump(mode="json")
    assert plan.terminal == "succeeded"


def _overlap_reason() -> str:
    matching = [
        {"to": "left", "condition": "true"},
        {"to": "right", "condition": "output.value == true"},
    ]
    return f"ambiguous_route:root/choose:{_format_route_keys(matching)}"


def _overlap_workflow() -> CompiledWorkflow:
    return _compiled(
        """      choose:
        kind: gate
        expression: 'true'
        routing: {mode: exclusive}
      left: {kind: end}
      right: {kind: end}
      fallback: {kind: end}""",
        """      - {from: choose, to: left, condition: 'true'}
      - {from: choose, to: right, condition: 'output.value == true'}
      - {from: choose, to: fallback, otherwise: true}""",
        start="choose",
    )


def test_exclusive_overlap_fails_graph_without_source_node_failed_or_tokens() -> None:
    compiled = _overlap_workflow()
    plan = plan_next(compiled, _projection(_invocation()))
    reason = _overlap_reason()
    _assert_graph_level_route_failure(plan, source_node="choose", reason=reason)
    assert not any(isinstance(event, TokenOffered) and event.source is not None for event in plan.events)


def _fanout_edges(*, always_false: bool = False) -> list[dict[str, object]]:
    if always_false:
        return [
            {"from": "choose", "to": "zebra", "condition": "output.value == true"},
            {"from": "choose", "to": "alpha", "condition": 'output.value == "other"'},
            {"from": "choose", "to": "gamma", "condition": "false"},
        ]
    return [
        {"from": "choose", "to": "zebra", "condition": "output.value == true"},
        {"from": "choose", "to": "alpha", "condition": "true"},
        {"from": "choose", "to": "gamma", "condition": "false"},
    ]


def _fanout_workflow(
    *,
    min_matches: int,
    expression: str = "true",
    always_false: bool = False,
) -> CompiledWorkflow:
    return _compiled(
        f"""      choose:
        kind: gate
        expression: '{expression}'
        routing: {{mode: fanout, min_matches: {min_matches}}}
      zebra: {{kind: end}}
      alpha: {{kind: end}}
      gamma: {{kind: end}}""",
        _edges_yaml(_fanout_edges(always_false=always_false)),
        start="choose",
    )


def test_fanout_min_zero_allows_no_matches() -> None:
    compiled = _fanout_workflow(min_matches=0, expression="false", always_false=True)
    plan = plan_next(compiled, _projection(_invocation()))

    assert _offered_edge_targets(plan) == ["choose"]
    assert plan.terminal is None
    assert not any(isinstance(event, GraphFailed) for event in plan.events)


def test_fanout_min_one_with_no_matches_fails_graph() -> None:
    compiled = _fanout_workflow(min_matches=1, expression="false", always_false=True)
    plan = plan_next(compiled, _projection(_invocation()))
    _assert_graph_level_route_failure(
        plan,
        source_node="choose",
        reason="insufficient_route_matches:root/choose:0/1",
    )
    assert not any(isinstance(event, TokenOffered) and event.source is not None for event in plan.events)


def test_fanout_multiple_matches_emit_in_canonical_order() -> None:
    edges = _fanout_edges()
    compiled = _fanout_workflow(min_matches=1)
    plan = plan_next(compiled, _projection(_invocation()))
    activation = _choose_activation(compiled)
    expected = [
        _expected_edge_token(compiled, edges, edges[1], payload={"value": True}),
        _expected_edge_token(compiled, edges, edges[0], payload={"value": True}),
    ]
    offered = [event for event in plan.events if event.kind == "token_offered" and event.source == "choose"]
    assert [event.target for event in offered] == ["alpha", "zebra"]
    assert [event.token_id for event in offered] == [item.token_id for item in expected]
    assert [event.payload for event in offered] == [{"value": True}, {"value": True}]
    assert offered[0].token_id == _edge_token_id(
        "root", activation, _canonical_rank(edges, edges[1]), "choose", "alpha"
    )
    assert offered[1].token_id == _edge_token_id(
        "root", activation, _canonical_rank(edges, edges[0]), "choose", "zebra"
    )
    assert plan.terminal == "succeeded"


def test_reordered_explicit_routes_yield_identical_tokens_and_terminal() -> None:
    first = _exclusive_workflow(_EXCLUSIVE_ONE_MATCH_EDGES)
    second = _exclusive_workflow(_EXCLUSIVE_ONE_MATCH_REORDERED)
    first_plan = plan_next(first, _projection(_invocation()))
    second_plan = plan_next(second, _projection(_invocation()))

    first_offered = [event for event in first_plan.events if event.kind == "token_offered"]
    second_offered = [event for event in second_plan.events if event.kind == "token_offered"]
    assert [event.model_dump(mode="json") for event in first_offered] == [
        event.model_dump(mode="json") for event in second_offered
    ]
    assert first_plan.terminal == second_plan.terminal == "succeeded"
    first_projection = _projection(_invocation(), *first_plan.events)
    second_projection = _projection(_invocation(), *second_plan.events)
    assert first_projection.model_dump(mode="json") == second_projection.model_dump(mode="json")


def _task_node(name: str) -> str:
    return f"      {name}: {{kind: task, capability: test.tasks.run, retry: policy, timeout: short}}"


def _workspace_identity(task_identifier: str, *, attempt: int = 1) -> TaskWorkspaceIdentity:
    payload = {
        "task_id": task_identifier,
        "attempt": attempt,
        "attempt_id": f"attempt-{attempt}",
        "output_paths": [],
        "baseline_files": [],
        "project_digest": "a" * 64,
        "write_root_digest": canonical_digest(
            {"task_id": task_identifier, "attempt": attempt, "kind": "write-root"}
        ),
        "layout_schema_version": "1",
    }
    return TaskWorkspaceIdentity(
        **payload,
        identity_digest=canonical_digest(payload),
    )


def _staged_write_set(identity: TaskWorkspaceIdentity) -> StagedWriteSet:
    payload = {"identity_digest": identity.identity_digest, "files": []}
    return StagedWriteSet(
        identity_digest=identity.identity_digest,
        files=(),
        staged_digest=canonical_digest(payload),
    )


def _start_sibling(task_identifier: str, activation: str) -> tuple[object, ...]:
    return (
        TaskAttemptStarted(activation_id=activation, attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
        TaskLeaseAcquired(
            task_id=task_identifier,
            activation_id=activation,
            attempt=1,
            owner_id="worker-1",
            acquired_at=1.0,
            heartbeat_at=1.0,
            expires_at=2.0,
        ),
    )


def _sibling_success(task_identifier: str, activation: str) -> tuple[object, ...]:
    identity = _workspace_identity(task_identifier)
    staged = _staged_write_set(identity)
    return (
        TaskCommitPrepared(
            task_id=task_identifier,
            activation_id=activation,
            attempt=1,
            output={"ok": True},
            workspace_identity=identity,
            staged_write_set=staged,
            staged_write_set_digest=staged.staged_digest,
            effect_ids=(),
        ),
        TaskPromotionCompleted(
            task_id=task_identifier,
            activation_id=activation,
            attempt=1,
            staged_write_set_digest=staged.staged_digest,
            promotion_receipt_digest="c" * 64,
        ),
        TaskAttemptSucceeded(
            activation_id=activation,
            attempt=1,
            output={"ok": True},
            staged_write_set_digest=staged.staged_digest,
            promotion_receipt_digest="c" * 64,
        ),
    )


def _sibling_failure(activation: str) -> tuple[object, ...]:
    return (
        TaskAttemptFailed(
            activation_id=activation,
            attempt=1,
            failure=TaskFailure(kind="transient", message="sibling failed", retryable=False),
        ),
    )


def _parallel_route_failure_workflow(*, mode: str) -> CompiledWorkflow:
    if mode == "overlap":
        choose = """      choose:
        kind: gate
        expression: 'true'
        routing: {mode: exclusive}
      left: {kind: end}
      right: {kind: end}
      fallback: {kind: end}"""
        choose_edges = """      - {from: choose, to: left, condition: 'true'}
      - {from: choose, to: right, condition: 'output.value == true'}
      - {from: choose, to: fallback, otherwise: true}"""
    else:
        choose = """      choose:
        kind: gate
        expression: 'false'
        routing: {mode: fanout, min_matches: 1}
      left: {kind: end}"""
        choose_edges = "      - {from: choose, to: left, condition: 'output.value == true'}"
    return _compiled(
        f"""      fork: {{kind: gate, expression: 'true'}}
{_task_node("sibling")}
{choose}
      done: {{kind: end}}""",
        f"""      - {{from: fork, to: sibling}}
      - {{from: fork, to: choose}}
      - {{from: sibling, to: done}}
{choose_edges}""",
        start="fork",
    )


def _bootstrap_route_failure_with_running_sibling(
    compiled: CompiledWorkflow,
) -> tuple[list[object], str, str]:
    events: list[object] = [_invocation(), _root(), _canonical_start_token(compiled)]
    planned = plan_next(compiled, _projection(*events))
    events.extend(planned.events)
    if not planned.tasks:
        planned = plan_next(compiled, _projection(*events))
        events.extend(planned.events)
    assert [task.node_id for task in planned.tasks] == ["sibling"]
    sibling = planned.tasks[0]
    events.extend(_start_sibling(sibling.task_id, sibling.activation_id))
    return events, sibling.task_id, sibling.activation_id


def _assert_no_terminal_progress(plan: object) -> None:
    assert plan.terminal is None
    assert plan.tasks == ()
    assert not any(isinstance(event, NodeFailed) for event in plan.events)
    assert not any(isinstance(event, GraphFailed) for event in plan.events)
    assert not any(isinstance(event, InvocationFinished) for event in plan.events)
    assert not any(isinstance(event, TokenOffered) and event.source is not None for event in plan.events)


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("overlap", _overlap_reason()),
        ("insufficient", "insufficient_route_matches:root/choose:0/1"),
    ],
)
def test_route_failure_barrier_holds_while_sibling_attempt_is_running(mode: str, reason: str) -> None:
    compiled = _parallel_route_failure_workflow(mode=mode)
    events, task_identifier, activation = _bootstrap_route_failure_with_running_sibling(compiled)
    projection = _projection(*events)

    first = plan_next(compiled, projection)
    second = plan_next(compiled, projection)
    running = plan_running_tasks(compiled, projection)

    _assert_no_terminal_progress(first)
    _assert_no_terminal_progress(second)
    assert first.events == second.events
    assert [task.activation_id for task in running] == [activation]
    assert [task.task_id for task in running] == [task_identifier]
    assert [task.attempt for task in running] == [1]
    assert _source_completed_ids(tuple(events), "choose") == [
        next(
            event.activation_id
            for event in events
            if getattr(event, "kind", None) == "node_activated" and event.node_id == "choose"
        )
    ]
    del reason


@pytest.mark.parametrize("outcome", ("success", "failure"))
@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("overlap", _overlap_reason()),
        ("insufficient", "insufficient_route_matches:root/choose:0/1"),
    ],
)
def test_route_failure_emits_canonical_graph_cause_once_after_sibling_settles(
    mode: str, reason: str, outcome: str
) -> None:
    compiled = _parallel_route_failure_workflow(mode=mode)
    events, task_identifier, activation = _bootstrap_route_failure_with_running_sibling(compiled)
    held = plan_next(compiled, _projection(*events))
    _assert_no_terminal_progress(held)

    if outcome == "success":
        events.extend(_sibling_success(task_identifier, activation))
    else:
        events.extend(_sibling_failure(activation))
    settled = plan_next(compiled, _projection(*events))
    _assert_graph_level_route_failure(
        settled,
        source_node="choose",
        reason=reason,
        history=tuple(events),
    )
    again = plan_next(compiled, _projection(*events, *settled.events))
    assert again.events == ()
    assert again.terminal == "failed"
    assert again.reason == reason

    history = (*events, *settled.events)
    envelopes = _envelopes(*history)
    validate_event_history(compiled, envelopes, fold_events(envelopes))


def test_exclusive_overlap_in_child_propagates_through_ancestors() -> None:
    compiled = _compiled(
        """      call: {kind: subgraph, graph: child}
      done: {kind: end}""",
        "      - {from: call, to: done, condition: 'false'}",
        start="call",
        extra_graphs="""  child:
    max_activations: 4
    start: choose
    nodes:
      choose:
        kind: gate
        expression: 'true'
        routing: {mode: exclusive}
      left: {kind: end}
      right: {kind: end}
      fallback: {kind: end}
    edges:
      - {from: choose, to: left, condition: 'true'}
      - {from: choose, to: right, condition: 'output.value == true'}
      - {from: choose, to: fallback, otherwise: true}
""",
    )
    plan = plan_next(compiled, _projection(_invocation()))
    reason = "ambiguous_route:child/choose:" + _format_route_keys(
        [
            {"to": "left", "condition": "true"},
            {"to": "right", "condition": "output.value == true"},
        ]
    )
    _assert_graph_level_route_failure(plan, source_node="choose", reason=reason)
    parent_activation = next(
        event.activation_id
        for event in plan.events
        if getattr(event, "kind", None) == "node_activated" and event.node_id == "call"
    )
    child_id = subgraph_instance_id(parent_activation, "child")
    kinds = [event.kind for event in plan.events]
    failed_at = kinds.index("graph_failed")
    chain = plan.events[failed_at:]
    assert [event.kind for event in chain] == [
        "graph_failed",
        "node_failed",
        "graph_failed",
        "invocation_finished",
    ]
    assert chain[0].graph_instance_id == child_id
    assert chain[1].activation_id == parent_activation
    assert chain[2].graph_instance_id == "root"


def _successful_exclusive_history() -> tuple[CompiledWorkflow, tuple[object, ...]]:
    compiled = _exclusive_workflow(_EXCLUSIVE_ONE_MATCH_EDGES)
    planned = plan_next(compiled, _projection(_invocation()))
    history = (_invocation(), *planned.events)
    return compiled, history


def _replace_edge_tokens(
    history: tuple[object, ...], replacements: tuple[TokenOffered, ...]
) -> tuple[object, ...]:
    rewritten: list[object] = []
    replaced = False
    for event in history:
        if isinstance(event, TokenOffered) and event.source == "choose":
            if not replaced:
                rewritten.extend(replacements)
                replaced = True
            continue
        rewritten.append(event)
    return tuple(rewritten)


def test_replay_rejects_missing_selected_branch() -> None:
    compiled, history = _successful_exclusive_history()
    forged = _replace_edge_tokens(history, ())
    envelopes = _envelopes(*forged)
    with pytest.raises((PlanningError, ProjectionError)):
        validate_event_history(compiled, envelopes, fold_events(envelopes))


def test_replay_rejects_extra_branch() -> None:
    compiled, history = _successful_exclusive_history()
    original = next(
        event for event in history if isinstance(event, TokenOffered) and event.source == "choose"
    )
    extra = TokenOffered(
        token_id=_edge_token_id("root", _choose_activation(compiled), 99, "choose", "alpha"),
        graph_instance_id="root",
        source="choose",
        target="alpha",
        payload=original.payload,
    )
    forged = _replace_edge_tokens(history, (original, extra))
    envelopes = _envelopes(*forged)
    with pytest.raises((PlanningError, ProjectionError)):
        validate_event_history(compiled, envelopes, fold_events(envelopes))


def test_replay_rejects_forged_otherwise_token() -> None:
    compiled, history = _successful_exclusive_history()
    otherwise = _expected_edge_token(
        compiled,
        _EXCLUSIVE_ONE_MATCH_EDGES,
        _EXCLUSIVE_ONE_MATCH_EDGES[2],
        payload={"value": True},
    )
    forged = _replace_edge_tokens(history, (otherwise,))
    envelopes = _envelopes(*forged)
    with pytest.raises((PlanningError, ProjectionError)):
        validate_event_history(compiled, envelopes, fold_events(envelopes))


def test_replay_rejects_forged_route_failure_reason() -> None:
    compiled = _overlap_workflow()
    planned = plan_next(compiled, _projection(_invocation()))
    rewritten: list[object] = [_invocation()]
    for event in planned.events:
        if isinstance(event, GraphFailed):
            rewritten.append(
                GraphFailed(graph_instance_id=event.graph_instance_id, reason="ambiguous_route:forged")
            )
        elif isinstance(event, InvocationFinished):
            rewritten.append(
                InvocationFinished(
                    invocation_id=event.invocation_id,
                    status="failed",
                    terminal_reason="ambiguous_route:forged",
                )
            )
        else:
            rewritten.append(event)
    envelopes = _envelopes(*rewritten)
    with pytest.raises((PlanningError, ProjectionError)):
        validate_event_history(compiled, envelopes, fold_events(envelopes))


def test_replay_rejects_premature_route_failure_chain() -> None:
    compiled = _parallel_route_failure_workflow(mode="overlap")
    events, task_identifier, activation = _bootstrap_route_failure_with_running_sibling(compiled)
    held = plan_next(compiled, _projection(*events))
    _assert_no_terminal_progress(held)
    canonical = plan_next(
        compiled,
        _projection(*events, *_sibling_success(task_identifier, activation)),
    )
    forged = _envelopes(*events, *canonical.events)
    with pytest.raises((PlanningError, ProjectionError)):
        folded = fold_events(forged)
        validate_event_history(compiled, forged, folded)


def test_replay_rejects_edge_token_with_route_failure() -> None:
    compiled = _overlap_workflow()
    planned = plan_next(compiled, _projection(_invocation()))
    prefix: list[object] = [_invocation()]
    for event in planned.events:
        prefix.append(event)
        if isinstance(event, NodeCompleted):
            break
    token = TokenOffered(
        token_id=_edge_token_id("root", _choose_activation(compiled), 0, "choose", "left"),
        graph_instance_id="root",
        source="choose",
        target="left",
        payload={"value": True},
    )
    reason = _overlap_reason()
    forged = (
        *prefix,
        token,
        GraphFailed(graph_instance_id="root", reason=reason),
        InvocationFinished(invocation_id="inv-1", status="failed", terminal_reason=reason),
    )
    envelopes = _envelopes(*forged)
    with pytest.raises((PlanningError, ProjectionError)):
        validate_event_history(compiled, envelopes, fold_events(envelopes))
