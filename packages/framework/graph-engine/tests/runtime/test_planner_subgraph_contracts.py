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
from graph_engine.plugin_api import PluginContribution, PluginDescriptor
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphStarted,
    InvocationStarted,
    NodeFailed,
    TaskAttemptStarted,
    TaskAttemptSucceeded,
    TaskCommitPrepared,
    TaskLeaseAcquired,
    TaskPromotionCompleted,
    TokenOffered,
)
from graph_engine.runtime.frozen_json import thaw_json
from graph_engine.runtime.models import InvocationProjection, fold_events
from graph_engine.runtime.planner import PlanningError, plan_next, validate_event_history
from graph_engine.plugin_api import StagedWriteSet, TaskWorkspaceIdentity


_INPUT_SCHEMA_ID = "toy.feature.workflow.run.input.v1"
_OUTPUT_SCHEMA_ID = "toy.feature.workflow.run.output.v1"
_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["change_id", "evidence", "policy"],
    "properties": {
        "change_id": {"type": "string"},
        "evidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status"],
            "properties": {"status": {"type": "string"}},
        },
        "policy": {"type": "string"},
    },
}
_PROJECTED_FIELDS = """
          type: object
          fields:
            change_id: {type: root_pointer, pointer: /change_id}
            evidence: {type: predecessor, predecessor: seed}
            policy: {type: config_pointer, pointer: /policy}
"""
_OUTPUT_PROJECTION = """
          type: object
          fields:
            status: {type: child_output_pointer, pointer: /status}
"""


class _PlaceholderHandler:
    async def execute(self, _request: object, _context: object) -> object:
        raise AssertionError("subgraph contract tests never execute task handlers")


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


def _schema_entry(schema_id: str, document: object | None = None) -> SchemaEntry:
    payload = document if document is not None else {"type": "object", "additionalProperties": False}
    return SchemaEntry.from_content(
        schema_id=schema_id,
        owner_id="toy.feature",
        media_type="application/schema+json",
        content=json.dumps(payload, separators=(",", ":")).encode(),
    )


@dataclass(frozen=True)
class _Registries:
    capabilities: CapabilityRegistry
    schemas: SchemaRegistry
    resources: ResourceRegistry
    effects: EffectRegistry


def _registries(*, input_schema: object | None = None) -> _Registries:
    return _Registries(
        capabilities=_capability_registry(),
        schemas=SchemaRegistry(
            {
                _INPUT_SCHEMA_ID: _schema_entry(_INPUT_SCHEMA_ID, input_schema or _INPUT_SCHEMA),
                _OUTPUT_SCHEMA_ID: _schema_entry(_OUTPUT_SCHEMA_ID),
            }
        ),
        resources=ResourceRegistry({}),
        effects=EffectRegistry({}),
    )


def _contracted_workflow_text(*, projection_fields: str = _PROJECTED_FIELDS) -> str:
    return f"""
name: subgraph-contracts
entrypoints: {{main: parent}}
schemas: [{_INPUT_SCHEMA_ID}, {_OUTPUT_SCHEMA_ID}]
retry: {{policy: {{max_attempts: 1, retry_on: []}}}}
timeout: {{short: {{run_seconds: 5}}}}
graphs:
  parent:
    max_activations: 8
    start: seed
    nodes:
      seed:
        kind: task
        capability: test.tasks.run
        retry: policy
        timeout: short
      child_call:
        kind: subgraph
        graph: child
        input: {{policy: strict}}
        input_projection:
{projection_fields}
        input_schema: {_INPUT_SCHEMA_ID}
        output_schema: {_OUTPUT_SCHEMA_ID}
        output_projection:
{_OUTPUT_PROJECTION}
      done: {{kind: end}}
    edges:
      - {{from: seed, to: child_call}}
      - {{from: child_call, to: done}}
  child:
    max_activations: 4
    start: done
    nodes:
      done: {{kind: end}}
    edges: []
"""


def _compiled(
    workflow_text: str,
    registries: _Registries | None = None,
) -> tuple[CompiledWorkflow, SchemaRegistry]:
    bundle = registries or _registries()
    return compile_workflow(parse_workflow(workflow_text), bundle), bundle.schemas


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


def _root(*, input_payload: object = None) -> GraphStarted:
    return GraphStarted(graph_instance_id="parent", graph_id="parent", input=input_payload)


def _canonical_start_token(compiled: CompiledWorkflow, *, payload: object = None) -> TokenOffered:
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
        payload=payload,
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


def _seed_success_events(task_id: str, activation_id: str, output: object) -> tuple[object, ...]:
    identity = _workspace_identity(task_id)
    staged = _staged_write_set(identity)
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
        TaskCommitPrepared(
            task_id=task_id,
            activation_id=activation_id,
            attempt=1,
            output=output,  # type: ignore[arg-type]
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
            output=output,  # type: ignore[arg-type]
            staged_write_set_digest=staged.staged_digest,
            promotion_receipt_digest="c" * 64,
        ),
    )


def _after_seed(
    compiled: CompiledWorkflow,
    schemas: SchemaRegistry,
    *,
    root_input: object,
    seed_output: object = None,
) -> tuple[tuple[object, ...], InvocationProjection]:
    events: list[object] = [
        _invocation(),
        _root(input_payload=root_input),
        _canonical_start_token(compiled, payload=root_input),
    ]
    planned = plan_next(compiled, _projection(*events), schemas=schemas)
    assert len(planned.tasks) == 1
    events.extend(planned.events)
    events.extend(
        _seed_success_events(
            planned.tasks[0].task_id,
            planned.tasks[0].activation_id,
            seed_output if seed_output is not None else {"status": "passed"},
        )
    )
    return tuple(events), _projection(*events)


def test_projected_subgraph_input_reaches_graph_started_and_start_token() -> None:
    compiled, schemas = _compiled(_contracted_workflow_text())
    events, projection = _after_seed(compiled, schemas, root_input={"change_id": "C-1"})

    plan = plan_next(compiled, projection, schemas=schemas)
    graph_started = next(
        event
        for event in plan.events
        if isinstance(event, GraphStarted) and event.parent_activation_id is not None
    )
    start_token = next(
        event
        for event in plan.events
        if isinstance(event, TokenOffered)
        and event.graph_instance_id == graph_started.graph_instance_id
        and event.source is None
    )

    assert graph_started.input == {
        "change_id": "C-1",
        "evidence": {"status": "passed"},
        "policy": "strict",
    }
    assert start_token.payload == graph_started.input
    assert canonical_digest(thaw_json(start_token.payload)) == canonical_digest(
        thaw_json(graph_started.input)
    )
    folded = fold_events(_envelopes(*events, *plan.events))
    validate_event_history(compiled, _envelopes(*events, *plan.events), folded, schemas=schemas)


@pytest.mark.parametrize(
    "case",
    ("missing_field", "extra_field", "wrong_type"),
)
def test_invalid_subgraph_input_emits_node_failed_without_child_start(case: str) -> None:
    if case == "extra_field":
        projection_fields = """
          type: object
          fields:
            change_id: {type: root_pointer, pointer: /change_id}
            evidence: {type: predecessor, predecessor: seed}
            policy: {type: config_pointer, pointer: /policy}
            extra: {type: literal, value: leftover}
"""
        compiled, schemas = _compiled(_contracted_workflow_text(projection_fields=projection_fields))
        root_input: object = {"change_id": "C-1"}
    elif case == "wrong_type":
        compiled, schemas = _compiled(_contracted_workflow_text())
        root_input = {"change_id": 1}
    else:
        compiled, schemas = _compiled(_contracted_workflow_text())
        root_input = {"present": True}

    _events, projection = _after_seed(compiled, schemas, root_input=root_input)
    plan = plan_next(compiled, projection, schemas=schemas)

    failed = [event for event in plan.events if isinstance(event, NodeFailed)]
    assert failed
    assert failed[0].failure.kind == "invalid_input"
    assert not any(
        isinstance(event, GraphStarted) and event.parent_activation_id is not None for event in plan.events
    )
    assert plan.tasks == ()
    assert plan.terminal == "failed"
    assert [event.kind for event in plan.events].count("graph_failed") >= 1
    assert [event.kind for event in plan.events].count("invocation_finished") == 1


def test_contracted_workflow_rejects_schemas_none() -> None:
    compiled, _schemas = _compiled(_contracted_workflow_text())
    projection = _projection(
        _invocation(),
        _root(input_payload={"change_id": "C-1"}),
        _canonical_start_token(compiled, payload={"change_id": "C-1"}),
    )

    with pytest.raises(PlanningError, match="schema"):
        plan_next(compiled, projection)


def test_legacy_subgraph_without_projection_uses_static_input() -> None:
    compiled, _schemas = _compiled(
        """
name: legacy-subgraph
entrypoints: {main: parent}
retry: {}
timeout: {}
graphs:
  parent:
    max_activations: 4
    start: child_call
    nodes:
      child_call: {kind: subgraph, graph: child, input: {mode: static-only}}
      done: {kind: end}
    edges:
      - {from: child_call, to: done}
  child:
    max_activations: 2
    start: done
    nodes:
      done: {kind: end}
    edges: []
"""
    )
    projection = _projection(_invocation())

    plan = plan_next(compiled, projection)
    graph_started = next(
        event
        for event in plan.events
        if isinstance(event, GraphStarted) and event.parent_activation_id is not None
    )
    start_token = next(
        event
        for event in plan.events
        if isinstance(event, TokenOffered)
        and event.graph_instance_id == graph_started.graph_instance_id
        and event.source is None
    )

    assert graph_started.input == {"mode": "static-only"}
    assert start_token.payload == graph_started.input


def test_forged_projected_child_input_fails_replay() -> None:
    compiled, schemas = _compiled(_contracted_workflow_text())
    events, projection = _after_seed(compiled, schemas, root_input={"change_id": "C-1"})
    plan = plan_next(compiled, projection, schemas=schemas)
    history = [*events, *plan.events]
    forged = []
    for event in history:
        if isinstance(event, GraphStarted) and event.parent_activation_id is not None:
            forged.append(event.model_copy(update={"input": {"forged": True}}))
            continue
        if isinstance(event, TokenOffered) and event.source is None and event.graph_instance_id != "parent":
            forged.append(event.model_copy(update={"payload": {"forged": True}}))
            continue
        forged.append(event)
    envelopes = _envelopes(*forged)
    folded = fold_events(envelopes)

    with pytest.raises(PlanningError):
        validate_event_history(compiled, envelopes, folded, schemas=schemas)
