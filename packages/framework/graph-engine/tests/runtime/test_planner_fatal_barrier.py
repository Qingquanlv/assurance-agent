from __future__ import annotations

import json
from dataclasses import dataclass
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
    EffectRegistry,
    ExecutableAuthority,
    ResourceRegistry,
    SchemaEntry,
    SchemaRegistry,
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
    NodeActivated,
    NodeFailed,
    TokenConsumed,
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
    plan_next,
    plan_running_tasks,
    validate_event_history,
)


_INPUT_SCHEMA_ID = "toy.feature.workflow.run.input.v1"
_OUTPUT_SCHEMA_ID = "toy.feature.workflow.run.output.v1"
_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["change_id"],
    "properties": {"change_id": {"type": "string"}},
}


class _PlaceholderHandler:
    async def execute(self, _request: object, _context: object) -> object:
        raise AssertionError("fatal barrier tests never execute task handlers")


def _capability_registry() -> CapabilityRegistry:
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


@dataclass(frozen=True)
class _Registries:
    capabilities: CapabilityRegistry
    schemas: SchemaRegistry
    resources: ResourceRegistry
    effects: EffectRegistry


def _registries() -> _Registries:
    def _entry(schema_id: str, document: object) -> SchemaEntry:
        return SchemaEntry.from_content(
            schema_id=schema_id,
            owner_id="toy.feature",
            media_type="application/schema+json",
            content=json.dumps(document, separators=(",", ":")).encode(),
        )

    return _Registries(
        capabilities=_capability_registry(),
        schemas=SchemaRegistry(
            {
                _INPUT_SCHEMA_ID: _entry(_INPUT_SCHEMA_ID, _INPUT_SCHEMA),
                _OUTPUT_SCHEMA_ID: _entry(
                    _OUTPUT_SCHEMA_ID, {"type": "object", "additionalProperties": False}
                ),
            }
        ),
        resources=ResourceRegistry({}),
        effects=EffectRegistry({}),
    )


def _compiled() -> tuple[CompiledWorkflow, SchemaRegistry]:
    bundle = _registries()
    compiled = compile_workflow(
        parse_workflow(
            f"""
name: fatal-barrier
entrypoints: {{main: parent}}
schemas: [{_INPUT_SCHEMA_ID}, {_OUTPUT_SCHEMA_ID}]
retry: {{policy: {{max_attempts: 1, retry_on: []}}}}
timeout: {{short: {{run_seconds: 5}}}}
graphs:
  parent:
    max_activations: 8
    start: fork
    nodes:
      fork:
        kind: gate
        expression: 'true'
      sibling:
        kind: task
        capability: test.tasks.run
        retry: policy
        timeout: short
      child_call:
        kind: subgraph
        graph: child
        input_projection: {{type: root_pointer, pointer: /change_id}}
        input_schema: {_INPUT_SCHEMA_ID}
        output_schema: {_OUTPUT_SCHEMA_ID}
        output_projection:
          type: object
          fields:
            status: {{type: child_output_pointer, pointer: /status}}
      done: {{kind: end}}
    edges:
      - {{from: fork, to: sibling}}
      - {{from: fork, to: child_call}}
      - {{from: sibling, to: done}}
      - {{from: child_call, to: done}}
  child:
    max_activations: 2
    start: done
    nodes:
      done: {{kind: end}}
    edges: []
"""
        ),
        bundle,
    )
    return compiled, bundle.schemas


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
    return GraphStarted(graph_instance_id="parent", graph_id="parent", input={"present": True})


def _canonical_start_token(compiled: CompiledWorkflow) -> TokenOffered:
    graph = compiled.graphs["parent"]
    return TokenOffered(
        token_id=canonical_digest(
            {
                "graph_instance_id": "parent",
                "kind": "graph_start",
                "target": graph.start,
            }
        ),
        graph_instance_id="parent",
        source=None,
        target=graph.start,
        payload={"present": True},
    )


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


def _start_sibling(task_id: str, activation_id: str) -> tuple[object, ...]:
    return (
        TaskAttemptStarted(activation_id=activation_id, attempt=1, lease_expires_at="2030-01-01T00:00:00Z"),
        TaskLeaseAcquired(
            task_id=task_id,
            activation_id=activation_id,
            attempt=1,
            owner_id="worker-1",
            acquired_at=1.0,
            heartbeat_at=1.0,
            expires_at=2.0,
        ),
    )


def _sibling_success(task_id: str, activation_id: str) -> tuple[object, ...]:
    identity = _workspace_identity(task_id)
    staged = _staged_write_set(identity)
    return (
        TaskCommitPrepared(
            task_id=task_id,
            activation_id=activation_id,
            attempt=1,
            output={"ok": True},
            workspace_identity=identity,
            staged_write_set=staged,
            staged_write_set_digest=staged.staged_digest,
            effect_ids=(),
        ),
        TaskPromotionCompleted(
            task_id=task_id,
            activation_id=activation_id,
            attempt=1,
            staged_write_set_digest=staged.staged_digest,
            promotion_receipt_digest="c" * 64,
        ),
        TaskAttemptSucceeded(
            activation_id=activation_id,
            attempt=1,
            output={"ok": True},
            staged_write_set_digest=staged.staged_digest,
            promotion_receipt_digest="c" * 64,
        ),
    )


def _sibling_failure(activation_id: str) -> tuple[object, ...]:
    return (
        TaskAttemptFailed(
            activation_id=activation_id,
            attempt=1,
            failure=TaskFailure(kind="transient", message="sibling failed", retryable=False),
        ),
    )


def _bootstrap_to_running_sibling(
    compiled: CompiledWorkflow, schemas: SchemaRegistry
) -> tuple[list[object], str, str]:
    events: list[object] = [
        _invocation(),
        _root(),
        _canonical_start_token(compiled),
    ]
    planned = plan_next(compiled, _projection(*events), schemas=schemas)
    assert planned.terminal is None
    events.extend(planned.events)
    if not planned.tasks:
        planned = plan_next(compiled, _projection(*events), schemas=schemas)
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


def _invalid_input_chain(plan: object) -> tuple[object, ...]:
    kinds = [event.kind for event in plan.events]
    assert "node_failed" in kinds
    failed = next(event for event in plan.events if isinstance(event, NodeFailed))
    assert failed.failure.kind == "invalid_input"
    assert not any(isinstance(event, GraphStarted) and event.parent_activation_id for event in plan.events)
    assert plan.tasks == ()
    assert plan.terminal == "failed"
    return plan.events


def test_fatal_barrier_holds_while_sibling_attempt_is_running() -> None:
    compiled, schemas = _compiled()
    events, task_id, activation_id = _bootstrap_to_running_sibling(compiled, schemas)
    projection = _projection(*events)

    first = plan_next(compiled, projection, schemas=schemas)
    second = plan_next(compiled, projection, schemas=schemas)
    running = plan_running_tasks(compiled, projection, schemas=schemas)

    _assert_no_terminal_progress(first)
    _assert_no_terminal_progress(second)
    assert first.events == second.events
    assert [task.activation_id for task in running] == [activation_id]
    assert [task.task_id for task in running] == [task_id]
    assert [task.attempt for task in running] == [1]


@pytest.mark.parametrize("outcome", ("success", "failure"))
def test_fatal_barrier_emits_canonical_invalid_input_after_sibling_settles(outcome: str) -> None:
    compiled, schemas = _compiled()
    events, task_id, activation_id = _bootstrap_to_running_sibling(compiled, schemas)
    held = plan_next(compiled, _projection(*events), schemas=schemas)
    _assert_no_terminal_progress(held)

    if outcome == "success":
        events.extend(_sibling_success(task_id, activation_id))
    else:
        events.extend(_sibling_failure(activation_id))
    settled = plan_next(compiled, _projection(*events), schemas=schemas)
    chain = _invalid_input_chain(settled)
    again = plan_next(compiled, _projection(*events, *chain), schemas=schemas)
    assert again.events == ()
    assert again.terminal == "failed"

    history = (*events, *chain)
    envelopes = _envelopes(*history)
    validate_event_history(compiled, envelopes, fold_events(envelopes), schemas=schemas)


def _compiled_invalid_output() -> tuple[CompiledWorkflow, SchemaRegistry]:
    bundle = _registries()
    compiled = compile_workflow(
        parse_workflow(
            f"""
name: fatal-barrier-output
entrypoints: {{main: parent}}
schemas: [{_INPUT_SCHEMA_ID}, {_OUTPUT_SCHEMA_ID}]
retry: {{policy: {{max_attempts: 1, retry_on: []}}}}
timeout: {{short: {{run_seconds: 5}}}}
graphs:
  parent:
    max_activations: 8
    start: fork
    nodes:
      fork:
        kind: gate
        expression: 'true'
      sibling:
        kind: task
        capability: test.tasks.run
        retry: policy
        timeout: short
      child_call:
        kind: subgraph
        graph: child
        input_projection:
          type: object
          fields:
            change_id: {{type: root_pointer, pointer: /change_id}}
        input_schema: {_INPUT_SCHEMA_ID}
        output_schema: {_OUTPUT_SCHEMA_ID}
        output_projection:
          type: object
          fields:
            status: {{type: child_output_pointer, pointer: /status}}
      done: {{kind: end}}
    edges:
      - {{from: fork, to: sibling}}
      - {{from: fork, to: child_call}}
      - {{from: sibling, to: done}}
      - {{from: child_call, to: done}}
  child:
    max_activations: 2
    start: done
    nodes:
      done: {{kind: end}}
    edges: []
"""
        ),
        bundle,
    )
    return compiled, bundle.schemas


def _output_root() -> GraphStarted:
    return GraphStarted(graph_instance_id="parent", graph_id="parent", input={"change_id": "C-1"})


def _output_start_token(compiled: CompiledWorkflow) -> TokenOffered:
    graph = compiled.graphs["parent"]
    return TokenOffered(
        token_id=canonical_digest(
            {
                "graph_instance_id": "parent",
                "kind": "graph_start",
                "target": graph.start,
            }
        ),
        graph_instance_id="parent",
        source=None,
        target=graph.start,
        payload={"change_id": "C-1"},
    )


def _bootstrap_invalid_output_with_running_sibling(
    compiled: CompiledWorkflow, schemas: SchemaRegistry
) -> tuple[list[object], str, str]:
    events: list[object] = [
        _invocation(),
        _output_root(),
        _output_start_token(compiled),
    ]
    planned = plan_next(compiled, _projection(*events), schemas=schemas)
    assert planned.terminal is None
    events.extend(planned.events)
    if not planned.tasks:
        planned = plan_next(compiled, _projection(*events), schemas=schemas)
        events.extend(planned.events)
    assert [task.node_id for task in planned.tasks] == ["sibling"]
    sibling = planned.tasks[0]
    events.extend(_start_sibling(sibling.task_id, sibling.activation_id))
    return events, sibling.task_id, sibling.activation_id


def _invalid_output_chain(plan: object) -> tuple[object, ...]:
    kinds = [event.kind for event in plan.events]
    assert "node_failed" in kinds
    failed = next(event for event in plan.events if isinstance(event, NodeFailed))
    assert failed.failure.kind == "invalid_output"
    assert failed.failure.retryable is False
    assert not any(isinstance(event, TokenOffered) and event.source == "child_call" for event in plan.events)
    assert plan.tasks == ()
    assert plan.terminal == "failed"
    return plan.events


def test_fatal_barrier_holds_invalid_output_while_sibling_attempt_is_running() -> None:
    compiled, schemas = _compiled_invalid_output()
    events, task_id, activation_id = _bootstrap_invalid_output_with_running_sibling(compiled, schemas)
    projection = _projection(*events)

    first = plan_next(compiled, projection, schemas=schemas)
    second = plan_next(compiled, projection, schemas=schemas)
    running = plan_running_tasks(compiled, projection, schemas=schemas)

    _assert_no_terminal_progress(first)
    _assert_no_terminal_progress(second)
    assert first.events == second.events
    assert [task.activation_id for task in running] == [activation_id]
    assert [task.task_id for task in running] == [task_id]
    assert [task.attempt for task in running] == [1]


@pytest.mark.parametrize("outcome", ("success", "failure"))
def test_fatal_barrier_emits_canonical_invalid_output_after_sibling_settles(outcome: str) -> None:
    compiled, schemas = _compiled_invalid_output()
    events, task_id, activation_id = _bootstrap_invalid_output_with_running_sibling(compiled, schemas)
    held = plan_next(compiled, _projection(*events), schemas=schemas)
    _assert_no_terminal_progress(held)

    if outcome == "success":
        events.extend(_sibling_success(task_id, activation_id))
    else:
        events.extend(_sibling_failure(activation_id))
    settled = plan_next(compiled, _projection(*events), schemas=schemas)
    chain = _invalid_output_chain(settled)
    again = plan_next(compiled, _projection(*events, *chain), schemas=schemas)
    assert again.events == ()
    assert again.terminal == "failed"

    history = (*events, *chain)
    envelopes = _envelopes(*history)
    validate_event_history(compiled, envelopes, fold_events(envelopes), schemas=schemas)


def test_fatal_barrier_rejects_forged_invalid_output_chain_before_sibling_settlement() -> None:
    compiled, schemas = _compiled_invalid_output()
    events, task_id, activation_id = _bootstrap_invalid_output_with_running_sibling(compiled, schemas)
    held = plan_next(compiled, _projection(*events), schemas=schemas)
    _assert_no_terminal_progress(held)

    canonical = plan_next(
        compiled,
        _projection(*events, *_sibling_success(task_id, activation_id)),
        schemas=schemas,
    )
    chain = _invalid_output_chain(canonical)
    start = 0
    while start < len(chain) and not isinstance(chain[start], NodeFailed):
        start += 1
    forged = _envelopes(*events, *chain[start:])
    with pytest.raises((PlanningError, ProjectionError)):
        folded = fold_events(forged)
        validate_event_history(compiled, forged, folded, schemas=schemas)


def test_fatal_barrier_rejects_forged_chain_before_sibling_settlement() -> None:
    compiled, schemas = _compiled()
    events, task_id, activation_id = _bootstrap_to_running_sibling(compiled, schemas)
    held = plan_next(compiled, _projection(*events), schemas=schemas)
    _assert_no_terminal_progress(held)

    canonical = plan_next(
        compiled,
        _projection(*events, *_sibling_success(task_id, activation_id)),
        schemas=schemas,
    )
    chain = _invalid_input_chain(canonical)
    activated = next(
        index
        for index, event in enumerate(chain)
        if isinstance(event, NodeActivated) and event.node_id == "child_call"
    )
    start = activated
    while start > 0 and isinstance(chain[start - 1], TokenConsumed):
        start -= 1
    forged = _envelopes(*events, *chain[start:])
    with pytest.raises((PlanningError, ProjectionError)):
        folded = fold_events(forged)
        validate_event_history(compiled, forged, folded, schemas=schemas)


def test_fatal_barrier_holds_route_overlap_while_sibling_attempt_is_running() -> None:
    from test_planner_routing import (
        _assert_no_terminal_progress as _routing_no_progress,
        _bootstrap_route_failure_with_running_sibling,
        _parallel_route_failure_workflow,
    )

    compiled = _parallel_route_failure_workflow(mode="overlap")
    events, task_id, activation_id = _bootstrap_route_failure_with_running_sibling(compiled)
    projection = _projection(*events)
    first = plan_next(compiled, projection)
    second = plan_next(compiled, projection)
    running = plan_running_tasks(compiled, projection)
    _routing_no_progress(first)
    _routing_no_progress(second)
    assert first.events == second.events
    assert [task.activation_id for task in running] == [activation_id]
    assert [task.task_id for task in running] == [task_id]


def test_fatal_barrier_holds_insufficient_route_matches_while_sibling_attempt_is_running() -> None:
    from test_planner_routing import (
        _assert_no_terminal_progress as _routing_no_progress,
        _bootstrap_route_failure_with_running_sibling,
        _parallel_route_failure_workflow,
    )

    compiled = _parallel_route_failure_workflow(mode="insufficient")
    events, task_id, activation_id = _bootstrap_route_failure_with_running_sibling(compiled)
    first = plan_next(compiled, _projection(*events))
    _routing_no_progress(first)
    running = plan_running_tasks(compiled, _projection(*events))
    assert [task.activation_id for task in running] == [activation_id]
    assert [task.task_id for task in running] == [task_id]
